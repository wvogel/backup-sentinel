"""Physical location of a backup, to tell real second copies from aliases.

A PBS is identified by its host name (short name, aliases, or resolved IP):
the same machine may be configured by IP in PVE and by name as a direct PBS
connection, and must still count as one server. Certificate fingerprints are
deliberately NOT used — with ACME/Let's Encrypt they rotate every few months,
so a fingerprint match silently breaks and the same server stops counting as
one. Other PVE storages (NFS, local, …) are their own location.
"""

from __future__ import annotations

import ipaddress
import socket
from collections.abc import Callable, Iterable, Mapping
from typing import Any
from urllib.parse import urlsplit


def pbs_location(host: str, datastore: str, namespace: str = "") -> str:
    server = (host or "").strip().lower()
    location = f"pbs:{server}/{datastore}"
    return f"{location}/{namespace}" if namespace else location


def pve_location(storage_id: str) -> str:
    return f"pve:{storage_id}"


def server_name(server: str) -> str:
    """Display name of a server key: short host name, IPs unchanged
    ("pbs:pbs-aboe-muc.int.example.de" → "pbs-aboe-muc")."""
    name = server.split(":", 1)[-1]
    return name if _is_ip(name) else name.split(".")[0]


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
    host: str,
    connections: Iterable[Mapping[str, Any]],
    resolve: Callable[[str], set[str]] = _dns,
) -> Mapping[str, Any] | None:
    """The registered PBS a PVE storage points to, matched by host identity:
    same (short) host name or alias, or the same resolved IP address. Never by
    certificate fingerprint — ACME certs rotate and would break the match."""
    connections = list(connections)
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


def assign_pbs_servers(
    events: list[dict[str, Any]],
    connections: Iterable[Mapping[str, Any]],
    resolve: Callable[[str], set[str]] = _dns,
) -> None:
    """Give PVE events on a PBS storage the location of the matching registered
    PBS connection (keyed by its name), so the same server counts once whichever
    way it was seen. Unregistered PBS keep a host-based name; storages on the
    same host name are merged under it (a name rather than an IP)."""
    connections = list(connections)
    cache: dict[str, set[str]] = {}

    def cached(host: str) -> set[str]:
        if host not in cache:
            cache[host] = resolve(host)
        return cache[host]

    for event in [e for e in events if e.get("pbs_target")]:
        t = event["pbs_target"]
        conn = match_pbs_connection(t["server"], connections, cached)
        if conn:
            event["location"] = connection_location(conn, t["datastore"], t["namespace"])
        else:
            event["location"] = pbs_location(str(t["server"]), t["datastore"], t["namespace"])


class CachedLookups:
    """DNS lookups, each host asked once per sync."""

    def __init__(self, resolve: Callable[[str], set[str]] = _dns):
        self._resolve = resolve
        self._dns: dict[str, set[str]] = {}

    def resolve(self, host: str) -> set[str]:
        if host not in self._dns:
            self._dns[host] = self._resolve(host)
        return self._dns[host]

    def connection(self, host: str, connections: Iterable[Mapping[str, Any]]) -> Mapping[str, Any] | None:
        return match_pbs_connection(host, connections, self.resolve)
