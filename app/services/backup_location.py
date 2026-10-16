"""Physical location of a backup, to tell real second copies from aliases.

A PBS is identified by its certificate fingerprint (falls back to the host):
the same machine may be configured by IP in PVE and by name as a direct PBS
connection, and must still count as one server. Other PVE storages (NFS,
local, …) are their own location.
"""

from __future__ import annotations

import hashlib
import ipaddress
import re
import socket
import ssl
from collections.abc import Callable, Iterable, Mapping
from typing import Any
from urllib.parse import urlsplit


def pbs_location(fingerprint: str, host: str, datastore: str, namespace: str = "") -> str:
    server = normalize_fingerprint(fingerprint) or (host or "").strip().lower()
    location = f"pbs:{server}/{datastore}"
    return f"{location}/{namespace}" if namespace else location


def pve_location(storage_id: str) -> str:
    return f"pve:{storage_id}"


def server_of(location: str) -> str:
    """The machine part of a location: copies on two servers survive one failing."""
    return location.split("/", 1)[0]


def server_name(server: str) -> str:
    """Display name of a server key: short host name, IPs unchanged
    ("pbs:pbs-aboe-muc.int.example.de" → "pbs-aboe-muc")."""
    name = server.split(":", 1)[-1]
    return name if _is_ip(name) else name.split(".")[0]


def normalize_fingerprint(fingerprint: str | None) -> str:
    return re.sub(r"[^0-9a-f]", "", (fingerprint or "").lower())


def connection_location(conn: Mapping[str, Any], datastore: str, namespace: str = "") -> str:
    """Location of a backup on a registered PBS connection, keyed by its name."""
    location = f"pbs:{_connection_name(conn)}/{datastore}"
    return f"{location}/{namespace}" if namespace else location


def _connection_name(conn: Mapping[str, Any]) -> str:
    return str(conn.get("name") or _connection_host(conn)).strip().lower()


def _connection_host(conn: Mapping[str, Any]) -> str:
    url = str(conn.get("api_url") or "")
    return (urlsplit(url).hostname if "://" in url else url.split(":")[0]).lower()


def _is_ip(host: str) -> bool:
    try:
        ipaddress.ip_address(host)
    except ValueError:
        return False
    return True


def _names(host: str) -> set[str]:
    host = host.strip().lower()
    if not host:
        return set()
    return {host} if _is_ip(host) else {host, host.split(".")[0]}


def _dns(host: str) -> set[str]:
    try:
        return {info[4][0] for info in socket.getaddrinfo(host, None)}
    except (OSError, UnicodeError):
        return set()


def match_pbs_connection(
    fingerprint: str,
    host: str,
    connections: Iterable[Mapping[str, Any]],
    resolve: Callable[[str], set[str]] = _dns,
) -> Mapping[str, Any] | None:
    """The registered PBS a PVE storage points to: same certificate, same
    (short) host name, or the same IP address."""
    connections = list(connections)
    fp = normalize_fingerprint(fingerprint)
    if fp:
        for conn in connections:
            # The fingerprint from the bootstrap or the ones the PBS reports
            # for its own certificates (fetched on every sync).
            known = [conn.get("fingerprint"), *(conn.get("cert_fingerprints") or [])]
            if fp in {normalize_fingerprint(f) for f in known}:
                return conn
    names = _names(host)
    for conn in connections:
        if names & _connection_names(conn):
            return conn
    addresses = resolve(host) if host else set()
    if addresses:
        for conn in connections:
            hosts = [_connection_host(conn), *(conn.get("aliases") or [])]
            if any(addresses & resolve(h) for h in hosts):
                return conn
    return None


def _connection_names(conn: Mapping[str, Any]) -> set[str]:
    """Names and addresses a registered PBS is known by, incl. its aliases."""
    names = _names(_connection_host(conn)) | {_connection_name(conn)}
    for alias in conn.get("aliases") or []:
        names |= _names(str(alias))
    return names


def probe_tls_fingerprint(host: str, port: int = 8007, timeout: float = 3.0) -> str:
    """SHA-256 fingerprint of the certificate a PBS serves, or "" if
    unreachable. Identifies a PBS known under different names or IPs."""
    try:
        context = ssl.create_default_context()
        context.check_hostname = False
        context.verify_mode = ssl.CERT_NONE
        with (
            socket.create_connection((host, port), timeout=timeout) as sock,
            context.wrap_socket(sock, server_hostname=host) as tls,
        ):
            der = tls.getpeercert(binary_form=True)
    except (OSError, ValueError):
        return ""
    return hashlib.sha256(der).hexdigest() if der else ""


def resolve_pbs_connection(
    fingerprint: str,
    host: str,
    connections: Iterable[Mapping[str, Any]],
    resolve: Callable[[str], set[str]] = _dns,
    probe: Callable[[str, int], str] = probe_tls_fingerprint,
    port: int = 8007,
) -> Mapping[str, Any] | None:
    """Like match_pbs_connection, but asks the server for its certificate
    when nothing matches and no fingerprint is known (e.g. an ACME
    certificate, or the PBS registered under its management name)."""
    connections = list(connections)
    conn = match_pbs_connection(fingerprint, host, connections, resolve)
    if conn or normalize_fingerprint(fingerprint) or not host:
        return conn
    probed = probe(host, port)
    return match_pbs_connection(probed, "", connections, resolve) if probed else None


def assign_pbs_servers(
    events: list[dict[str, Any]],
    connections: Iterable[Mapping[str, Any]],
    resolve: Callable[[str], set[str]] = _dns,
    probe: Callable[[str, int], str] | None = None,
) -> None:
    """Give PVE events on a PBS storage the location of the matching registered
    PBS connection, so the same server counts once whichever way it was seen.

    Unregistered PBS keep a host-based name; storages sharing a certificate
    fingerprint are merged under one host (a name rather than an IP).
    """
    connections = list(connections)
    cache: dict[str, set[str]] = {}

    def cached(host: str) -> set[str]:
        if host not in cache:
            cache[host] = resolve(host)
        return cache[host]

    targets = [e for e in events if e.get("pbs_target")]
    unmatched_hosts: dict[str, str] = {}
    pending: list[dict[str, Any]] = []
    for event in targets:
        t = event["pbs_target"]
        if probe:
            conn = resolve_pbs_connection(t["fingerprint"], t["server"], connections, cached, probe)
        else:
            conn = match_pbs_connection(t["fingerprint"], t["server"], connections, cached)
        if conn:
            event["location"] = connection_location(conn, t["datastore"], t["namespace"])
            continue
        fp = normalize_fingerprint(t["fingerprint"])
        host = str(t["server"]).strip().lower()
        if fp and host:
            # Prefer a host name over an IP address as the display name.
            unmatched_hosts[fp] = min(unmatched_hosts.get(fp, host), host, key=lambda h: (_is_ip(h), h))
        pending.append(event)
    for event in pending:
        t = event["pbs_target"]
        host = unmatched_hosts.get(normalize_fingerprint(t["fingerprint"])) or str(t["server"]).strip().lower()
        location = f"pbs:{host}/{t['datastore']}"
        event["location"] = f"{location}/{t['namespace']}" if t["namespace"] else location


class CachedLookups:
    """DNS and certificate lookups, each host asked once per sync."""

    def __init__(
        self,
        resolve: Callable[[str], set[str]] = _dns,
        probe: Callable[[str, int], str] = probe_tls_fingerprint,
    ):
        self._resolve, self._probe = resolve, probe
        self._dns: dict[str, set[str]] = {}
        self._certs: dict[tuple[str, int], str] = {}

    def resolve(self, host: str) -> set[str]:
        if host not in self._dns:
            self._dns[host] = self._resolve(host)
        return self._dns[host]

    def probe(self, host: str, port: int) -> str:
        if (host, port) not in self._certs:
            self._certs[(host, port)] = self._probe(host, port)
        return self._certs[(host, port)]

    def connection(
        self, fingerprint: str, host: str, connections: Iterable[Mapping[str, Any]]
    ) -> Mapping[str, Any] | None:
        return resolve_pbs_connection(fingerprint, host, connections, self.resolve, self.probe)
