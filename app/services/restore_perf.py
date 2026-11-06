"""Operator-triggered write-performance test for the restore host's local
storages, and storage of its result.

The measured sustained random-write rate (bytes/s) of a storage becomes the
100% (``disk_max``) of the Disk-I/O graph. The test runs fio on the host in a
background thread (~5 min); results, live progress (percent / current rate /
ETA / a short rate history for the sparkline) and the "currently running" and
"cancel requested" markers live in ``app_settings`` so every worker sees them
and they survive a restart.
"""

from __future__ import annotations

import json
import logging
import threading
from datetime import UTC, datetime
from typing import Any

from app.db_settings import get_settings_dict, save_settings

logger = logging.getLogger(__name__)

_PREFIX = "restore_server.perf."  # + <storage> → {"bps": int, "at": iso, "error"?: str}
_RUNNING_KEY = "restore_server.perf_running"  # value = storage being benchmarked, else ""
_PROGRESS_KEY = "restore_server.perf_progress"  # live {storage, pct, rate_bps, eta, series, …}
_CANCEL_KEY = "restore_server.perf_cancel"  # value = storage whose test should abort, else ""

_RUNTIME = 300  # fio time-based duration (s); also drives the UI's time estimate
_SERIES_CAP = 150  # most rate samples kept for the sparkline


def get_perf() -> dict[str, dict[str, Any]]:
    """``{storage: {"bps", "at", "error"?}}`` for every storage ever tested."""
    out: dict[str, dict[str, Any]] = {}
    for key, val in get_settings_dict(_PREFIX).items():
        name = key[len(_PREFIX) :]
        if not name:
            continue
        try:
            out[name] = json.loads(val)
        except (ValueError, TypeError):
            continue
    return out


def running_storage() -> str:
    """The storage whose benchmark is running right now, or '' when none is."""
    return get_settings_dict(_RUNNING_KEY).get(_RUNNING_KEY, "") or ""


def get_progress() -> dict[str, Any]:
    """Live progress of the running test: ``{storage, pct, rate_bps, eta,
    started_at, runtime, series:[bps,…]}`` — or ``{}`` when nothing runs."""
    raw = get_settings_dict(_PROGRESS_KEY).get(_PROGRESS_KEY, "") or ""
    try:
        return json.loads(raw) if raw else {}
    except (ValueError, TypeError):
        return {}


def _cancel_requested() -> str:
    return get_settings_dict(_CANCEL_KEY).get(_CANCEL_KEY, "") or ""


def _set_running(storage: str) -> None:
    save_settings({_RUNNING_KEY: storage})


def _store_result(storage: str, *, bps: int | None = None, error: str | None = None) -> None:
    rec: dict[str, Any] = {"at": datetime.now(UTC).isoformat()}
    if bps is not None:
        rec["bps"] = int(bps)
    if error is not None:
        rec["error"] = error[:200]
    save_settings({_PREFIX + storage: json.dumps(rec)})


def _store_progress(storage: str, sample: dict[str, Any]) -> None:
    """Fold one fio ETA sample into the live progress record, appending its rate
    to the series that feeds the sparkline."""
    prog = get_progress()
    if prog.get("storage") != storage:  # stale (a new run started) — ignore
        return
    prog["pct"] = sample.get("pct", prog.get("pct", 0))
    prog["at"] = datetime.now(UTC).isoformat()
    if "eta" in sample:
        prog["eta"] = sample["eta"]
    rate = sample.get("rate_bps")
    if rate is not None:
        prog["rate_bps"] = int(rate)
        series = list(prog.get("series") or [])
        series.append(int(rate))
        prog["series"] = series[-_SERIES_CAP:]
    save_settings({_PROGRESS_KEY: json.dumps(prog)})


def _reset_live() -> None:
    save_settings({_RUNNING_KEY: "", _PROGRESS_KEY: "", _CANCEL_KEY: ""})


def involved_storages() -> set[str]:
    """Storages the currently running restore writes to (its target storages) —
    their test buttons are disabled so a benchmark never fights a live restore.
    The host restores one VM at a time, so only that run's targets are busy."""
    from app import db_restore

    for run in db_restore.list_runs(limit=1, status="running"):
        return set(run.get("target_storages") or [])
    return set()


def start(storage: str) -> tuple[bool, str]:
    """Kick a background benchmark for ``storage``. Returns ``(started, reason)``;
    refuses when another benchmark runs or the storage is in a live restore."""
    if running_storage():
        return False, "another performance test is already running"
    if storage in involved_storages():
        return False, "storage is in use by a running restore"
    _set_running(storage)
    save_settings(
        {
            _CANCEL_KEY: "",
            _PROGRESS_KEY: json.dumps(
                {
                    "storage": storage,
                    "pct": 0,
                    "started_at": datetime.now(UTC).isoformat(),
                    "runtime": _RUNTIME,
                    "series": [],
                }
            ),
        }
    )
    threading.Thread(target=_run, args=(storage,), name=f"perf-{storage}", daemon=True).start()
    return True, ""


def cancel(storage: str) -> tuple[bool, str]:
    """Ask the running test for ``storage`` to abort. The worker thread stops at
    its next poll (~5 s), then frees the scratch volume."""
    if running_storage() != storage:
        return False, "no performance test is running for this storage"
    save_settings({_CANCEL_KEY: storage})
    return True, ""


def _run(storage: str) -> None:
    from app.services import restore_config, restore_orchestrator
    from app.services.restore_server import BenchmarkCancelled

    try:
        config = restore_config.load()
        if not config:
            raise RuntimeError("restore server not configured")
        config = restore_orchestrator.ensure_host_key_pinned(config)
        server = restore_orchestrator.build_server(config)
        bps = server.benchmark_storage_write(
            storage,
            runtime=_RUNTIME,
            progress_cb=lambda sample: _store_progress(storage, sample),
            cancel_cb=lambda: _cancel_requested() == storage,
        )
        _store_result(storage, bps=bps)
        logger.info("perf benchmark %s: %d bytes/s", storage, bps)
    except BenchmarkCancelled:
        logger.info("perf benchmark %s cancelled by operator", storage)  # no result stored
    except Exception as exc:
        logger.warning("perf benchmark %s failed: %s", storage, exc)
        _store_result(storage, error=str(exc))
    finally:
        _reset_live()
