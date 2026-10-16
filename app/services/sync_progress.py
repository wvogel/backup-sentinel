"""Live progress of running replication syncs (PBS sync jobs).

The precomputed diagram knows which syncs ran at the last background sync;
how far they are is asked live from the PBS task log, cached briefly. Used by
the cluster page (initial render and its 30 s poll) and the overview.
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Any

from app import db
from app.services import topology_store
from app.services.pbs import sync_task_state

logger = logging.getLogger(__name__)

# Ask a PBS about a task at most this often.
_TTL = 30.0
# How old a live value may be to still be shown when a page loads.
_SHOW_AGE = 120.0

_cache: dict[str, tuple[float, tuple[float | None, bool]]] = {}
_lock = threading.Lock()


def clear() -> None:
    with _lock:
        _cache.clear()


def _cached(upid: str, max_age: float) -> tuple[float | None, bool] | None:
    with _lock:
        hit = _cache.get(upid)
    if hit is None or time.monotonic() - hit[0] >= max_age:
        return None
    return hit[1]


def finished(upid: str | None) -> bool:
    """A live look already saw this sync task end (before the next sync)."""
    with _lock:
        hit = _cache.get(upid or "")
    return bool(hit and hit[1][1])


def _running_edges(topology: dict[str, Any]) -> list[dict[str, Any]]:
    """Running sync edges, in the edge list and in the datastores' copies."""
    incoming = [e for column in topology["columns"] for g in column for s in g["stores"] for e in s["incoming"]]
    return [e for e in [*topology["edges"], *incoming] if e.get("kind") == "sync" and e.get("state") == "running"]


def with_live_progress(topology: dict[str, Any]) -> dict[str, Any]:
    """Running syncs show only live progress: a fresh cached value, or none
    (the ring turns until the page's first poll answers)."""
    for edge in _running_edges(topology):
        state = _cached(edge.get("running_upid") or "", _SHOW_AGE)
        edge["running_percent"] = state[0] if state else None
    return topology


def live_progress(cluster_id: int) -> dict[str, dict[str, Any]]:
    """{"pbs_id:job_id": {"percent", "finished"}} for the cluster's running
    syncs, asking each PBS at most every 30 s."""
    conns = {c["id"]: c for c in db.list_pbs_connections()}
    result: dict[str, dict[str, Any]] = {}
    for edge in topology_store.load(cluster_id)["edges"]:
        upid, conn = edge.get("running_upid"), conns.get(edge.get("pbs_id"))
        if edge.get("kind") != "sync" or edge.get("state") != "running" or not upid or not conn:
            continue
        state = _cached(upid, _TTL)
        if state is None:
            try:
                state = sync_task_state(dict(conn), upid)
            except Exception as exc:  # PBS unreachable: the page keeps what it shows
                logger.warning("Sync progress of %s unavailable: %s", edge.get("job_id"), exc)
                continue
            with _lock:
                _cache[upid] = (time.monotonic(), state)
        result[f"{edge['pbs_id']}:{edge['job_id']}"] = {"percent": state[0], "finished": state[1]}
    return result


def running_syncs(cluster_id: int) -> list[str]:
    """ "pbs-pano/nas-pbs-pano → nas-offsite-fh-muc" for each running sync
    job; ones a live look has already seen finish are left out."""
    topology = topology_store.load(cluster_id)
    stores = {s["id"]: s["store"] for column in topology["columns"] for g in column for s in g["stores"]}
    return [
        f"{edge['source_label']} → {stores.get(edge['to'], '')}"
        for edge in topology["edges"]
        if edge.get("kind") == "sync" and edge.get("state") == "running" and not finished(edge.get("running_upid"))
    ]
