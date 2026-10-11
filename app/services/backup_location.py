"""Physical location of a backup, to tell real second copies from aliases.

A PBS is identified by its certificate fingerprint (falls back to the host):
the same machine may be configured by IP in PVE and by name as a direct PBS
connection, and must still count as one server. Other PVE storages (NFS,
local, …) are their own location.
"""

from __future__ import annotations


def pbs_location(fingerprint: str, host: str, datastore: str, namespace: str = "") -> str:
    server = (fingerprint or "").strip().lower() or (host or "").strip().lower()
    location = f"pbs:{server}/{datastore}"
    return f"{location}/{namespace}" if namespace else location


def pve_location(storage_id: str) -> str:
    return f"pve:{storage_id}"


def server_of(location: str) -> str:
    """The machine part of a location: copies on two servers survive one failing."""
    return location.split("/", 1)[0]
