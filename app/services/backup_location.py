"""Physical location of a backup, to tell real second copies from aliases.

A PBS is identified by its certificate fingerprint (falls back to the host):
the same machine may be configured by IP in PVE and by name as a direct PBS
connection, and must still count as one server. Other PVE storages (NFS,
local, …) are their own location.
"""

from __future__ import annotations

import ipaddress
import re
import socket
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
            if fp == normalize_fingerprint(conn.get("fingerprint")):
                return conn
    names = _names(host)
    for conn in connections:
        if names & (_names(_connection_host(conn)) | {_connection_name(conn)}):
            return conn
    addresses = resolve(host) if host else set()
    if addresses:
        for conn in connections:
            if addresses & resolve(_connection_host(conn)):
                return conn
    return None


def assign_pbs_servers(
    events: list[dict[str, Any]],
    connections: Iterable[Mapping[str, Any]],
    resolve: Callable[[str], set[str]] = _dns,
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
