"""Match a VM's backups to a local datastore on the scratch host.

The scratch host (PVE-Restore) only ever has *local* PBS datastores, one per
source ``(pbs, datastore, namespace)``. A VM is auto-restorable iff one of its
known backup locations matches such a local datastore **exactly** on all three
of ``(pbs name, datastore, namespace)`` — never fuzzy (spec §7). From the
matched datastore we restore the latest *complete* (not necessarily verified)
snapshot.

These are pure functions; the DB plumbing that produces a VM's backup tuples
and the scratch host's local datastores lives in the worker / server client.
"""

from __future__ import annotations

from typing import Any, NamedTuple


class PbsTuple(NamedTuple):
    pbs: str
    datastore: str
    namespace: str


def _norm_ns(namespace: str | None) -> str:
    """PBS namespace path without surrounding or doubled slashes ("/a//b/"->"a/b")."""
    return "/".join(part for part in (namespace or "").strip().split("/") if part)


def normalize(pbs: str, datastore: str, namespace: str | None) -> PbsTuple:
    return PbsTuple(pbs.strip().lower(), datastore.strip(), _norm_ns(namespace))


def match_source(vm_locations: list[PbsTuple], scratch_storages: list[dict[str, Any]]) -> dict[str, Any] | None:
    """Return the scratch-host local datastore whose (pbs, datastore, namespace)
    exactly matches one of the VM's backup locations, or None.

    Each entry of ``scratch_storages`` is a dict carrying at least
    ``pbs``/``datastore``/``namespace`` (its identity) plus whatever the caller
    needs to drive the restore (e.g. ``storage`` id on the restore host).
    """
    wanted = set(vm_locations)
    for store in scratch_storages:
        key = normalize(store["pbs"], store["datastore"], store.get("namespace"))
        if key in wanted:
            return store
    return None


def is_auto_restorable(vm_locations: list[PbsTuple], scratch_storages: list[dict[str, Any]]) -> bool:
    return match_source(vm_locations, scratch_storages) is not None


def pick_latest_complete(snapshots: list[dict[str, Any]]) -> dict[str, Any] | None:
    """Newest snapshot that is complete (``complete`` truthy). Completeness, not
    PBS verification state (spec §3). ``backup_time`` is a unix timestamp."""
    complete = [s for s in snapshots if s.get("complete")]
    if not complete:
        return None
    return max(complete, key=lambda s: s.get("backup_time", 0))
