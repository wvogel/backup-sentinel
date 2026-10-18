"""Running backup progress parsing for Proxmox VE."""

from __future__ import annotations

import contextvars
import logging
import math
import re
import threading
import time
from collections.abc import Callable
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib import parse
from zoneinfo import ZoneInfo

from app.config import DEFAULT_TIMEZONE
from app.i18n import t
from app.services.human_format import short_number
from app.services.proxmox import (
    ProxmoxSyncError,
    _pve_get_dict,
    _pve_get_json,
    _pve_get_payload,
    build_token_header,
)
from app.services.vzdump_log import PBS_ARCHIVE_RE, START_RE, STARTED_AT_RE, parse_size

logger = logging.getLogger(__name__)

# Same zone as the web UI's datetime_local filter, independent of the container's TZ.
APP_TIMEZONE = ZoneInfo(DEFAULT_TIMEZONE)

_PROGRESS_RE = re.compile(
    r"(?P<pct>\d+)%\s+\((?P<written>[^)]+?)\s+of\s+(?P<total>[^)]+?)\)\s+in\s+(?P<elapsed>[\ddhms\s]+),",
    re.IGNORECASE,
)
_RATE_RE = re.compile(
    r"read:\s*(?P<read>[\d.]+\s*[KMGTPE]?i?B/s),\s*write:\s*(?P<write>[\d.]+\s*[KMGTPE]?i?B/s)", re.IGNORECASE
)
_DIRTY_RE = re.compile(
    r"using fast incremental mode .*?,\s*(?P<dirty>[\d.]+\s+[KMGTPE]?i?B)\s+dirty\s+of\s+(?P<full>[\d.]+\s+[KMGTPE]?i?B)\s+total",
    re.IGNORECASE,
)
_VMID_RE = re.compile(r"\b(?:VM|CT)\s+(?P<vmid>\d+)\b|\b(?:qemu|lxc)[/-](?P<vmid2>\d+)\b", re.IGNORECASE)


def _round_rate(label: str) -> str:
    """Throughput without noise: "187.7 MiB/s" → "188 MiB/s" (one decimal
    stays below 10, e.g. "1.6 GiB/s")."""
    match = re.match(r"\s*(\d+(?:\.\d+)?)\s*(\S+)", label)
    if not match:
        return label
    # No thousands separator: the label is cached for every UI language.
    return f"{short_number(float(match.group(1)), grouping=False)} {match.group(2)}"


def _parse_elapsed_to_seconds(value: str | None) -> int | None:
    if not value:
        return None
    total = 0
    for amount, unit in re.findall(r"(\d+)\s*([dhms])", value.lower()):
        n = int(amount)
        if unit == "d":
            total += n * 86400
        elif unit == "h":
            total += n * 3600
        elif unit == "m":
            total += n * 60
        else:
            total += n
    return total or None


def _extract_log_lines(data: Any) -> list[str]:
    if not isinstance(data, list):
        return []
    lines: list[str] = []
    for row in data:
        if isinstance(row, dict):
            text = row.get("t") or row.get("msg") or row.get("text")
            if text:
                lines.append(str(text))
        elif isinstance(row, str):
            lines.append(row)
    return lines


def _current_vm_log(log_lines: list[str]) -> list[str]:
    """Return the log section of the VM currently being backed up.

    A scheduled vzdump job backs up several VMs one after another in a single
    task; progress lines of VMs that already finished must not be attributed
    to the running one. Logs without a "Starting Backup" line are returned as is.
    """
    for i in range(len(log_lines) - 1, -1, -1):
        if START_RE.search(log_lines[i]):
            return log_lines[i:]
    return log_lines


def _extract_vmid_from_task(task: dict[str, Any], log_lines: list[str]) -> int | None:
    # The last "Starting Backup" line names the VM that is running right now —
    # earlier ones belong to VMs of the same job that are already done.
    for line in reversed(log_lines):
        match = START_RE.search(line)
        if match:
            return int(match.group("vmid"))
    for key in ("id", "upid", "worker_id"):
        value = str(task.get(key) or "")
        match = _VMID_RE.search(value)
        if match:
            return int(match.group("vmid") or match.group("vmid2"))
    return None


def _build_progress_title(progress: dict[str, Any]) -> str:
    lines = []
    if progress.get("started_at_label"):
        lines.append(t("progress.start", time=progress["started_at_label"]))
    if progress.get("eta_label"):
        lines.append(t("progress.eta", time=progress["eta_label"]))
    if progress.get("written_label") and progress.get("target_label"):
        lines.append(t("progress.written_of", written=progress["written_label"], total=progress["target_label"]))
    elif progress.get("written_label"):
        lines.append(t("progress.written_so_far", written=progress["written_label"]))
    if progress.get("delta_label"):
        lines.append(t("progress.delta", size=progress["delta_label"]))
    if progress.get("full_label"):
        lines.append(t("progress.vm_total", size=progress["full_label"]))
    return "\n".join(lines)


def _fmt_local(value: datetime) -> str:
    return value.astimezone(APP_TIMEZONE).strftime("%d.%m.%Y %H:%M")


def _fmt_short(value: datetime, now: datetime | None = None) -> str:
    """Time only for today, otherwise day and time — for compact labels."""
    local = value.astimezone(APP_TIMEZONE)
    today = (now or datetime.now(UTC)).astimezone(APP_TIMEZONE).date()
    return local.strftime("%H:%M") if local.date() == today else local.strftime("%d.%m. %H:%M")


def _vm_started_at(log_lines: list[str]) -> datetime | None:
    """Start of the current VM's backup: the PBS archive name (UTC), else
    "Backup started at" (node local time, assumed to be the app zone)."""
    for line in log_lines:
        match = PBS_ARCHIVE_RE.search(line)
        if match:
            return datetime.fromisoformat(match.group("ts")).replace(tzinfo=UTC)
    for line in log_lines:
        match = STARTED_AT_RE.search(line)
        if match:
            return datetime.fromisoformat(match.group("ts")).replace(tzinfo=APP_TIMEZONE)
    return None


def _parse_running_backup_progress(task: dict[str, Any], log_lines: list[str]) -> dict[str, Any]:
    return _localize(_parse_progress_data(task, log_lines))


def _localize(progress: dict[str, Any]) -> dict[str, Any]:
    """Progress with the UI language's label and tooltip (a copy: the parsed
    data is cached once for all languages)."""
    localized = dict(progress)
    localized["label"] = (
        f"Backup {progress['percent']}%" if progress.get("percent") is not None else t("progress.running")
    )
    localized["title"] = _build_progress_title(progress)
    return localized


def _parse_progress_data(task: dict[str, Any], log_lines: list[str]) -> dict[str, Any]:
    """Progress of the running VM from its log section, without translated text."""
    progress: dict[str, Any] = {}

    start_raw = task.get("starttime") or task.get("start-time")
    started_at: datetime | None = None
    if start_raw not in (None, ""):
        try:
            started_at = datetime.fromtimestamp(int(start_raw), UTC)
        except (TypeError, ValueError, OSError):
            started_at = None
    # The task start is the start of the whole job; in a multi-VM job other
    # VMs ran before. Prefer the current VM's own start from its log section.
    started_at = _vm_started_at(log_lines) or started_at
    if started_at is not None:
        progress["started_at"] = started_at
        progress["started_at_label"] = _fmt_local(started_at)

    for line in log_lines:
        dirty_match = _DIRTY_RE.search(line)
        if dirty_match:
            progress["delta_bytes"] = parse_size(dirty_match.group("dirty"))
            progress["full_bytes"] = parse_size(dirty_match.group("full"))
            progress["delta_label"] = dirty_match.group("dirty")
            progress["full_label"] = dirty_match.group("full")

    for line in reversed(log_lines):
        progress_match = _PROGRESS_RE.search(line)
        if progress_match:
            percent = int(progress_match.group("pct"))
            written_label = progress_match.group("written")
            target_label = progress_match.group("total")
            elapsed_seconds = _parse_elapsed_to_seconds(progress_match.group("elapsed"))
            progress["percent"] = percent
            # Average since the previous progress line, not an instant value.
            rate_match = _RATE_RE.search(line)
            if rate_match:
                progress["read_label"] = _round_rate(rate_match.group("read"))
                progress["write_label"] = _round_rate(rate_match.group("write"))
            progress["written_bytes"] = parse_size(written_label)
            progress["target_bytes"] = parse_size(target_label)
            progress["written_label"] = written_label
            progress["target_label"] = target_label
            # "elapsed" counts from the VM's start, not from when the line was
            # written — large VMs log progress only every ~30 min.
            if percent > 0 and elapsed_seconds is not None and started_at is not None:
                total_seconds = math.ceil(elapsed_seconds * 100 / percent)
                eta = started_at + timedelta(seconds=total_seconds)
                progress["eta"] = eta
                progress["eta_label"] = _fmt_local(eta)
                progress["eta_short"] = _fmt_short(eta)
            break

    return progress


# ── Reading the task logs ────────────────────────────────────────────────────
# A scheduled job backs up many VMs in one task, and its log grows to many
# thousands of lines. Each refresh reads only the lines added since the last
# one; per task the section of the VM running right now is kept.
_LOG_PAGE = 1000


@dataclass(slots=True)
class _TaskLog:
    read: int = 0  # log lines read so far
    section: list[str] = field(default_factory=list)  # from the last "Starting Backup" on


def _with_lines(state: _TaskLog, lines: list[str], read: int) -> _TaskLog:
    for i in range(len(lines) - 1, -1, -1):
        if START_RE.search(lines[i]):
            return _TaskLog(read, lines[i:])
    return _TaskLog(read, state.section + lines)


def _read_task_log(get_page: Callable[[int, int], tuple[list[str], int | None]], state: _TaskLog) -> _TaskLog:
    """Bring `state` up to date. `get_page(start, limit)` returns the log lines
    after the first `start` ones and the log's total line count (PVE: "total")."""
    lines, total = get_page(state.read, _LOG_PAGE)
    if total is not None and total < state.read:  # not the log we read before
        state = _TaskLog()
        lines, total = get_page(0, _LOG_PAGE)
    if total is None:  # no line count reported: read on page by page
        page = lines
        while len(page) == _LOG_PAGE:
            page, _ = get_page(state.read + len(lines), _LOG_PAGE)
            lines += page
        return _with_lines(state, lines, state.read + len(lines))
    if total == 1 and lines == ["no content"]:  # PVE's answer for an empty log
        return state
    floor = state.read + len(lines)
    if total <= floor:
        return _with_lines(state, lines, floor)
    # More new lines than one page: the running VM's section is at the end.
    # Read back from there to its "Starting Backup" line; the lines skipped
    # belong to VMs that are already done.
    tail: list[str] = []
    end = total
    while end > floor:
        start = max(floor, end - _LOG_PAGE)
        page, _ = get_page(start, end - start)
        tail = page + tail
        end = start
        if any(START_RE.search(line) for line in page):
            return _with_lines(state, tail, total)
    return _with_lines(state, lines + tail, total)


# Per (cluster, node, UPID) of a running task; dropped once it no longer runs.
_task_logs: dict[tuple[Any, str, str], _TaskLog] = {}
_task_logs_lock = threading.Lock()
# Nodes and tasks are asked in parallel. Long-lived threads keep their PVE
# connection open (api_client keeps one per server and thread).
_pve_pool = ThreadPoolExecutor(max_workers=8, thread_name_prefix="backup-progress-pve")


def _fetch_running_backup_progress(
    cluster: dict[str, Any], nodes: list[str], timeout: float = 5.0
) -> tuple[dict[int, dict[str, Any]], bool]:
    """Running backups of a cluster (progress by VMID, not translated) and
    whether every node's task list could be read."""
    if not cluster.get("api_user") or not cluster.get("token_id") or not cluster.get("token_secret"):
        return {}, False

    base_url = cluster["api_url"].rstrip("/")
    auth_header = build_token_header(cluster["api_user"], cluster["token_id"], cluster["token_secret"])
    cluster_key = cluster.get("id")

    def node_path(node_name: str) -> str:
        return f"/api2/json/nodes/{parse.quote(node_name, safe='')}"

    def list_tasks(node_name: str) -> list[dict[str, Any]] | None:
        # Live checks are asked once (no retries): the next poll asks again.
        try:
            return _pve_get_json(
                base_url,
                f"{node_path(node_name)}/tasks",
                auth_header,
                timeout=timeout,
                query={"source": "active", "typefilter": "vzdump"},
                retries=0,
            )
        except ProxmoxSyncError as exc:
            logger.warning("Cannot list running backup tasks on node %s: %s", node_name, exc)
            return None

    def inspect(node_name: str, task: dict[str, Any]) -> tuple[int, dict[str, Any]] | None:
        upid = str(task["upid"])
        task_path = f"{node_path(node_name)}/tasks/{parse.quote(upid, safe='')}"
        key = (cluster_key, node_name, upid)

        def get_page(start: int, limit: int) -> tuple[list[str], int | None]:
            payload = _pve_get_payload(
                base_url,
                f"{task_path}/log",
                auth_header,
                timeout=max(timeout, 10.0),
                query={"start": str(start), "limit": str(limit)},
                retries=0,
            )
            total = payload.get("total")
            return _extract_log_lines(payload.get("data")), total if isinstance(total, int) else None

        try:
            status = _pve_get_dict(base_url, f"{task_path}/status", auth_header, timeout=timeout, retries=0)
            if status.get("status") == "stopped":
                return None
            with _task_logs_lock:
                state = _task_logs.get(key) or _TaskLog()
            state = _read_task_log(get_page, state)
        except ProxmoxSyncError as exc:
            logger.warning("Cannot inspect running backup task %s on %s: %s", upid, node_name, exc)
            return None
        with _task_logs_lock:
            _task_logs[key] = state

        vmid = _extract_vmid_from_task({**task, **status}, state.section)
        if vmid is None:
            return None
        return vmid, _parse_progress_data({**task, **status}, _current_vm_log(state.section))

    task_lists = dict(zip(nodes, _pve_pool.map(list_tasks, nodes), strict=True))
    complete = all(tasks is not None for tasks in task_lists.values())
    running = [
        (node_name, task)
        for node_name, tasks in task_lists.items()
        for task in tasks or []
        if str(task.get("upid") or "")
    ]
    result: dict[int, dict[str, Any]] = {}
    for item in _pve_pool.map(lambda pair: inspect(*pair), running):
        if item is not None:
            result[item[0]] = item[1]

    # Forget tasks that ended (only on nodes that answered).
    active = {(cluster_key, node_name, str(task["upid"])) for node_name, task in running}
    with _task_logs_lock:
        for key in list(_task_logs):
            if key[0] == cluster_key and task_lists.get(key[1]) is not None and key not in active:
                del _task_logs[key]
    return result, complete


# ── Cached access (dashboard, cluster page, status endpoint, sync) ───────────
# One entry per cluster for all UI languages; labels are added per request.
_CACHE_MAX_AGE = 20.0
_cache: dict[Any, tuple[float, dict[int, dict[str, Any]], bool]] = {}
_cache_lock = threading.Lock()
_executor = ThreadPoolExecutor(max_workers=8, thread_name_prefix="backup-progress")
# Fetch running per cluster: everyone who needs fresh data waits for it.
_inflight: dict[Any, Future[tuple[dict[int, dict[str, Any]], bool]]] = {}
# How old a cached result may be and still be shown while a refresh runs.
_PEEK_MAX_AGE = 300.0


def _localize_all(progress: dict[int, dict[str, Any]]) -> dict[int, dict[str, Any]]:
    return {vmid: _localize(item) for vmid, item in progress.items()}


def _refresh_future(cluster: dict[str, Any], nodes: list[str]) -> Future[tuple[dict[int, dict[str, Any]], bool]]:
    """The running fetch of the cluster, or a new one. Call with `_cache_lock` held."""
    key = cluster["id"]
    future = _inflight.get(key)
    if future is None:
        future = _executor.submit(contextvars.copy_context().run, _refresh, key, cluster, nodes)
        _inflight[key] = future
    return future


def _refresh(key: Any, cluster: dict[str, Any], nodes: list[str]) -> tuple[dict[int, dict[str, Any]], bool]:
    try:
        progress, complete = _fetch_running_backup_progress(cluster, nodes)
        with _cache_lock:
            _cache[key] = (time.monotonic(), progress, complete)
        return progress, complete
    except Exception:
        logger.exception("Refreshing running backups of cluster %s failed", cluster.get("name"))
        raise
    finally:
        with _cache_lock:
            _inflight.pop(key, None)


def running_backup_progress_cached(cluster: dict[str, Any], nodes: list[str]) -> tuple[dict[int, dict[str, Any]], bool]:
    """Running backups of a cluster, at most `_CACHE_MAX_AGE` seconds old.

    Returns (progress by VMID, complete); `complete` is False when a node's
    task list could not be read, so the result may be missing backups.
    Callers that find the cache stale at the same time share one fetch.
    """
    with _cache_lock:
        hit = _cache.get(cluster["id"])
        if hit and time.monotonic() - hit[0] < _CACHE_MAX_AGE:
            return _localize_all(hit[1]), hit[2]
        future = _refresh_future(cluster, nodes)
    progress, complete = future.result()
    return _localize_all(progress), complete


def running_backup_progress_peek(
    cluster: dict[str, Any], nodes: list[str]
) -> tuple[dict[int, dict[str, Any]] | None, bool]:
    """Cached running backups without waiting for PVE (for page loads).

    Returns (progress, complete) from the cache, or (None, False) when nothing
    recent is cached. A stale or missing entry is refreshed in the background,
    so the next look (or the page's poll) gets live data.
    """
    with _cache_lock:
        hit = _cache.get(cluster["id"])
        age = time.monotonic() - hit[0] if hit else None
        if age is None or age >= _CACHE_MAX_AGE:
            _refresh_future(cluster, nodes)
    if hit and age is not None and age < _PEEK_MAX_AGE:
        return _localize_all(hit[1]), hit[2]
    return None, False


def cached_running_backups(
    clusters: list[tuple[dict[str, Any], list[str]]],
) -> dict[int, dict[int, dict[str, Any]] | None]:
    """Running backups of several clusters from the cache, without waiting;
    None for a cluster with no recent or no complete data."""
    result: dict[int, dict[int, dict[str, Any]] | None] = {}
    for cluster, nodes in clusters:
        progress, complete = running_backup_progress_peek(cluster, nodes)
        result[cluster["id"]] = progress if progress is not None and complete else None
    return result
