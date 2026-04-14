"""Running backup progress parsing for Proxmox VE."""

from __future__ import annotations

import logging
import math
import re
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib import parse

from app.services.proxmox import ProxmoxSyncError, _pve_get, _pve_get_dict, _pve_get_json, build_token_header

logger = logging.getLogger(__name__)

_PROGRESS_RE = re.compile(
    r"(?P<pct>\d+)%\s+\((?P<written>[^)]+?)\s+of\s+(?P<total>[^)]+?)\)\s+in\s+(?P<elapsed>[\ddhms\s]+),",
    re.IGNORECASE,
)
_DIRTY_RE = re.compile(
    r"using fast incremental mode .*?,\s*(?P<dirty>[\d.]+\s+[KMGTPE]?i?B)\s+dirty\s+of\s+(?P<full>[\d.]+\s+[KMGTPE]?i?B)\s+total",
    re.IGNORECASE,
)
_START_RE = re.compile(r"Starting Backup of (?:VM|CT)\s+(?P<vmid>\d+)", re.IGNORECASE)
_VMID_RE = re.compile(r"\b(?:VM|CT)\s+(?P<vmid>\d+)\b|\b(?:qemu|lxc)[/-](?P<vmid2>\d+)\b", re.IGNORECASE)


def _parse_size_to_bytes(value: str | None) -> int | None:
    if not value:
        return None
    match = re.search(r"(?P<num>[\d.]+)\s*(?P<unit>[KMGTPE]?i?B|B)", value.strip(), re.IGNORECASE)
    if not match:
        return None
    number = float(match.group("num"))
    unit = match.group("unit").upper()
    factors = {
        "B": 1,
        "KIB": 1024,
        "MIB": 1024**2,
        "GIB": 1024**3,
        "TIB": 1024**4,
        "PIB": 1024**5,
        "EIB": 1024**6,
        "KB": 1000,
        "MB": 1000**2,
        "GB": 1000**3,
        "TB": 1000**4,
        "PB": 1000**5,
        "EB": 1000**6,
    }
    return int(number * factors[unit]) if unit in factors else None


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


def _extract_vmid_from_task(task: dict[str, Any], log_lines: list[str]) -> int | None:
    for key in ("id", "upid", "worker_id"):
        value = str(task.get(key) or "")
        match = _VMID_RE.search(value)
        if match:
            return int(match.group("vmid") or match.group("vmid2"))
    for line in log_lines:
        match = _START_RE.search(line)
        if match:
            return int(match.group("vmid"))
    return None


def _build_progress_title(progress: dict[str, Any]) -> str:
    lines = []
    if progress.get("started_at_label"):
        lines.append(f"Start: {progress['started_at_label']}")
    if progress.get("eta_label"):
        lines.append(f"ETA: {progress['eta_label']}")
    if progress.get("written_label") and progress.get("target_label"):
        lines.append(f"{progress['written_label']} von {progress['target_label']}")
    elif progress.get("written_label"):
        lines.append(f"Bisher: {progress['written_label']}")
    if progress.get("delta_label"):
        lines.append(f"Delta: {progress['delta_label']}")
    if progress.get("full_label"):
        lines.append(f"VM gesamt: {progress['full_label']}")
    return "\n".join(lines)


def _parse_running_backup_progress(task: dict[str, Any], log_lines: list[str]) -> dict[str, Any]:
    progress: dict[str, Any] = {}

    start_raw = task.get("starttime") or task.get("start-time")
    started_at: datetime | None = None
    if start_raw not in (None, ""):
        try:
            started_at = datetime.fromtimestamp(int(start_raw), UTC)
        except (TypeError, ValueError, OSError):
            started_at = None
    if started_at is not None:
        progress["started_at"] = started_at
        progress["started_at_label"] = started_at.astimezone().strftime("%d.%m.%Y %H:%M")

    for line in log_lines:
        dirty_match = _DIRTY_RE.search(line)
        if dirty_match:
            progress["delta_bytes"] = _parse_size_to_bytes(dirty_match.group("dirty"))
            progress["full_bytes"] = _parse_size_to_bytes(dirty_match.group("full"))
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
            progress["written_bytes"] = _parse_size_to_bytes(written_label)
            progress["target_bytes"] = _parse_size_to_bytes(target_label)
            progress["written_label"] = written_label
            progress["target_label"] = target_label
            if percent > 0 and started_at is not None and elapsed_seconds is not None:
                total_seconds = math.ceil(elapsed_seconds * (100 / percent))
                eta = started_at + timedelta(seconds=total_seconds)
                progress["eta"] = eta
                progress["eta_label"] = eta.astimezone().strftime("%d.%m.%Y %H:%M")
            break

    progress["label"] = f"Backup {progress['percent']}%" if progress.get("percent") is not None else "Backup läuft"
    progress["title"] = _build_progress_title(progress)
    return progress


def fetch_running_backup_progress(
    cluster: dict[str, Any], nodes: list[str], timeout: float = 5.0
) -> dict[int, dict[str, Any]]:
    if not cluster.get("api_user") or not cluster.get("token_id") or not cluster.get("token_secret"):
        return {}

    base_url = cluster["api_url"].rstrip("/")
    auth_header = build_token_header(cluster["api_user"], cluster["token_id"], cluster["token_secret"])
    result: dict[int, dict[str, Any]] = {}

    for node_name in nodes:
        try:
            tasks = _pve_get_json(
                base_url,
                f"/api2/json/nodes/{parse.quote(node_name, safe='')}/tasks",
                auth_header,
                timeout=timeout,
                query={"source": "active", "typefilter": "vzdump"},
            )
        except ProxmoxSyncError as exc:
            logger.warning("Cannot list running backup tasks on node %s: %s", node_name, exc)
            continue

        for task in tasks:
            upid = str(task.get("upid") or "")
            if not upid:
                continue
            try:
                status = _pve_get_dict(
                    base_url,
                    f"/api2/json/nodes/{parse.quote(node_name, safe='')}/tasks/{parse.quote(upid, safe='')}/status",
                    auth_header,
                    timeout=timeout,
                )
                if status.get("status") == "stopped":
                    continue
                log_data = _pve_get(
                    base_url,
                    f"/api2/json/nodes/{parse.quote(node_name, safe='')}/tasks/{parse.quote(upid, safe='')}/log",
                    auth_header,
                    timeout=max(timeout, 10.0),
                    query={"start": "0", "limit": "5000"},
                )
            except ProxmoxSyncError as exc:
                logger.warning("Cannot inspect running backup task %s on %s: %s", upid, node_name, exc)
                continue

            log_lines = _extract_log_lines(log_data)
            vmid = _extract_vmid_from_task({**task, **status}, log_lines)
            if vmid is None:
                continue
            progress = _parse_running_backup_progress({**task, **status}, log_lines)
            if progress:
                result[vmid] = progress

    return result
