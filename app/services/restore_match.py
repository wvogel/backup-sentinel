"""Match a VM's backups to a local datastore on the scratch host.

Identity is the **canonical PBS backup location** — the exact same key the
protection/offsite column uses (``app.services.backup_location``): a PBS is
identified by its certificate fingerprint resolved to the registered connection
(``pbs:<name>/<datastore>/<namespace>``), never by a raw host string. A VM is
auto-restorable iff one of its known backup locations equals one of the restore
host's PBS datastores' locations — exact string match, 1:1, no heuristics. From
the matched datastore we restore the latest *complete* snapshot.

These are pure functions; the DB plumbing that produces a VM's backup locations
and resolves the restore host's PBS datastores lives in the orchestrator.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any


def match_source(vm_locations: Iterable[str], host_entries: list[dict[str, Any]]) -> dict[str, Any] | None:
    """Return the restore-host PBS datastore whose canonical ``location`` equals
    one of the VM's backup locations, or None. Each host entry carries at least
    ``location`` (the canonical key) plus what the restore needs (``storage`` id,
    ``pbs``/``datastore``/``namespace``)."""
    wanted = set(vm_locations)
    for entry in host_entries:
        if entry.get("location") in wanted:
            return entry
    return None


def is_auto_restorable(vm_locations: Iterable[str], host_entries: list[dict[str, Any]]) -> bool:
    return match_source(vm_locations, host_entries) is not None


def pick_latest_complete(snapshots: list[dict[str, Any]]) -> dict[str, Any] | None:
    """Newest snapshot that is complete (``complete`` truthy). Completeness, not
    PBS verification state (spec §3). ``backup_time`` is a unix timestamp."""
    complete = [s for s in snapshots if s.get("complete")]
    if not complete:
        return None
    return max(complete, key=lambda s: s.get("backup_time", 0))
