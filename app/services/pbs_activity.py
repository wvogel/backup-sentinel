"""Garbage collections and verifications running on the PBS right now, on the
datastores that hold the cluster's backups (the ones in its diagram). What a
PBS does on a datastore the cluster has nothing on is none of its business:
such tasks are neither shown nor are their logs read.

Nothing asks a PBS on its own: only an open page's poll does (`refresh`), each
PBS at most every 30 s however many pages are open. Pages render from what
that left behind (`tasks`), never waiting for a PBS; with no page open, no
request is made at all.
"""

from __future__ import annotations

import logging
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Any

from app.services import topology_store
from app.services.pbs import fetch_running_maintenance, last_run_seconds, maintenance_progress

logger = logging.getLogger(__name__)

# Ask a PBS at most this often.
_TTL = 30.0
# How old a state may be to still be shown when a page loads.
_SHOW_AGE = 120.0
# After a failed look (PBS down, rights missing): leave it alone this long.
_BACKOFF = 300.0
# A PBS verifying many single snapshots at once: read the logs of this many.
_MAX_TASKS = 12
# The last run's duration of a job hardly changes: ask once in this time.
_DURATION_TTL = 6 * 3600.0
_WORKERS = 8

_cache: dict[int, tuple[float, list[dict[str, Any]]]] = {}
_next_look: dict[int, float] = {}
_durations: dict[tuple[int, str, str], tuple[float, int | None]] = {}
_lock = threading.Lock()


def clear() -> None:
    with _lock:
        _cache.clear()
        _next_look.clear()
        _durations.clear()


def tasks(pbs_id: int, max_age: float = _SHOW_AGE) -> list[dict[str, Any]]:
    """What the last look found running on this PBS; nothing once it is stale."""
    with _lock:
        hit = _cache.get(pbs_id)
    if hit is None or time.monotonic() - hit[0] >= max_age:
        return []
    return hit[1]


def _last_duration(conn: dict[str, Any], task: dict[str, Any]) -> int | None:
    # A single group or snapshot has no earlier run to compare with.
    if task["worker_type"] not in ("garbage_collection", "verificationjob", "verify"):
        return None
    key = (conn["id"], task["worker_type"], task["worker_id"])
    now = time.monotonic()
    with _lock:
        hit = _durations.get(key)
    if hit and now - hit[0] < _DURATION_TTL:
        return hit[1]
    try:
        seconds = last_run_seconds(conn, task["worker_type"], task["worker_id"])
    except Exception as exc:
        logger.debug("Last run of %s on PBS %s unavailable: %s", task["worker_id"], conn.get("name"), exc)
        seconds = None
    with _lock:
        _durations[key] = (now, seconds)
    return seconds


def relevant_stores(conn: dict[str, Any]) -> set[str]:
    """This PBS's datastores in its cluster's diagram: the ones PVE backs up
    to and the ones holding synced copies of those backups."""
    topology = topology_store.load(conn["cluster_id"])
    return {
        store["store"]
        for column in topology["columns"]
        for group in column
        if group.get("conn_id") == conn["id"]
        for store in group["stores"]
    }


def _look(conn: dict[str, Any]) -> None:
    try:
        found = fetch_running_maintenance(conn)
        if found:
            stores = relevant_stores(conn)
            found = [task for task in found if task["store"] in stores]
    except Exception as exc:
        logger.warning("Running tasks of PBS %s unavailable: %s", conn.get("name"), exc)
        with _lock:
            _next_look[conn["id"]] = time.monotonic() + _BACKOFF
        return
    result = []
    for task in found[:_MAX_TASKS]:
        try:
            progress = maintenance_progress(conn, task["upid"], task["kind"])
        except Exception as exc:  # the task still shows as running, without figures
            logger.debug("Progress of %s unavailable: %s", task["upid"], exc)
            progress = {}
        if progress.get("finished"):
            continue
        result.append(task | {"progress": progress, "last_seconds": _last_duration(conn, task)})
    with _lock:
        _cache[conn["id"]] = (time.monotonic(), result)


def refresh(conns: list[dict[str, Any]]) -> None:
    """Look at the PBS that are due, side by side; the others keep their state."""
    now = time.monotonic()
    with _lock:
        due = [dict(c) for c in conns if now >= _next_look.get(c["id"], 0.0)]
        for conn in due:
            _next_look[conn["id"]] = now + _TTL
    if not due:
        return
    if len(due) == 1:
        _look(due[0])
        return
    with ThreadPoolExecutor(max_workers=min(_WORKERS, len(due))) as pool:
        list(pool.map(_look, due))
