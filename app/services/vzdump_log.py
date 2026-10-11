"""Parse finished vzdump task logs into per-VM backup details.

A vzdump task backs up one or more VMs in sequence; each VM's section starts
with "Starting Backup of VM <id>". From each section we extract what the
storage listings don't tell us: runtime, data transferred, average throughput,
how much was reused from the previous backup and whether it was a full run.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import UTC, datetime, tzinfo

_START_RE = re.compile(r"Starting Backup of (?:VM|CT) (?P<vmid>\d+)", re.IGNORECASE)
_PBS_ARCHIVE_RE = re.compile(r"archive '(?:vm|ct)/\d+/(?P<ts>\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2})Z'")
_STARTED_AT_RE = re.compile(r"Backup started at (?P<ts>\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})")
_TRANSFERRED_RE = re.compile(
    r"transferred (?P<size>[\d.]+ [KMGTPE]?i?B) in (?P<secs>\d+) seconds(?: \((?P<rate>[\d.]+ [KMGTPE]?i?B/s)\))?",
    re.IGNORECASE,
)
_REUSED_RE = re.compile(r"backup was done incrementally, reused [\d.]+ [KMGTPE]?i?B \((?P<pct>\d+)%\)", re.IGNORECASE)
_FULL_RE = re.compile(r"dirty-bitmap status: (?:existing bitmap was invalid|created new)", re.IGNORECASE)
_FINISHED_RE = re.compile(r"Finished Backup of (?:VM|CT) \d+ \((?P<h>\d+):(?P<m>\d\d):(?P<s>\d\d)\)", re.IGNORECASE)
_FAILED_RE = re.compile(r"ERROR: Backup of (?:VM|CT) \d+ failed", re.IGNORECASE)

_UNITS = {
    "B": 1,
    "KIB": 1024,
    "MIB": 1024**2,
    "GIB": 1024**3,
    "TIB": 1024**4,
    "PIB": 1024**5,
    "KB": 1000,
    "MB": 1000**2,
    "GB": 1000**3,
    "TB": 1000**4,
    "PB": 1000**5,
}


@dataclass(slots=True)
class VmBackupRun:
    vmid: int
    started_at: datetime | None = None
    duration_seconds: int | None = None
    transferred_bytes: int | None = None
    throughput_bps: int | None = None
    reused_pct: int | None = None
    full_backup: bool | None = None
    failed: bool = False


def _to_bytes(value: str) -> int | None:
    number, _, unit = value.strip().partition(" ")
    unit = unit.upper().removesuffix("/S")
    factor = _UNITS.get(unit)
    try:
        return int(float(number) * factor) if factor else None
    except ValueError:
        return None


def parse_vzdump_log(lines: list[str], node_tz: tzinfo) -> list[VmBackupRun]:
    """Split a vzdump task log into one result per backed-up VM.

    `node_tz` is used for "Backup started at" (node local time); the PBS
    archive name, when present, is UTC and takes precedence.
    """
    runs: list[VmBackupRun] = []
    current: VmBackupRun | None = None
    seen_incremental = False
    seen_full = False

    def close() -> None:
        if current is None:
            return
        if seen_full:
            current.full_backup = True
        elif seen_incremental:
            current.full_backup = False
        runs.append(current)

    for line in lines:
        start = _START_RE.search(line)
        if start:
            close()
            current = VmBackupRun(vmid=int(start.group("vmid")))
            seen_incremental = seen_full = False
            continue
        if current is None:
            continue
        if match := _PBS_ARCHIVE_RE.search(line):
            current.started_at = datetime.fromisoformat(match.group("ts")).replace(tzinfo=UTC)
        elif (match := _STARTED_AT_RE.search(line)) and current.started_at is None:
            current.started_at = datetime.fromisoformat(match.group("ts")).replace(tzinfo=node_tz).astimezone(UTC)
        elif match := _TRANSFERRED_RE.search(line):
            current.transferred_bytes = _to_bytes(match.group("size"))
            if match.group("rate"):
                current.throughput_bps = _to_bytes(match.group("rate"))
            elif current.transferred_bytes and int(match.group("secs")):
                current.throughput_bps = current.transferred_bytes // int(match.group("secs"))
        elif match := _REUSED_RE.search(line):
            current.reused_pct = int(match.group("pct"))
            seen_incremental = True
        elif _FULL_RE.search(line):
            seen_full = True
        elif match := _FINISHED_RE.search(line):
            current.duration_seconds = int(match.group("h")) * 3600 + int(match.group("m")) * 60 + int(match.group("s"))
        elif _FAILED_RE.search(line):
            current.failed = True
    close()
    return runs
