"""How much room each guest's backups take on a PBS datastore — an estimate.

PBS has no figure for it: chunks are shared between all backups of a
datastore. What it does log, per backup task, is how much the backup
uploaded and how well that compressed ("Upload size", "Compression",
"Duplicates"). Summed over a guest's snapshots that still exist, this is the
data its backups newly wrote — after compression, and without the chunks the
server already had. Chunks a later backup of *another* guest shares are still
counted with the one that wrote them first.

Reading it costs the PBS one short task log per backup, once: no index file
is opened and no chunk is touched, so the disks behind the datastore see
nothing of it.
"""

from __future__ import annotations

import logging
import re
from datetime import UTC, datetime
from typing import Any
from urllib import parse

from app import db
from app.services.pbs import _pbs_get_payload

logger = logging.getLogger(__name__)

# Task logs read per PBS and sync: the backlog of a newly registered PBS is
# worked off over several syncs instead of in one burst.
LOGS_PER_SYNC = 150
_TASK_PAGE = 500
_TASK_PAGES = 20
_LOG_LINES = 5000

_START_RE = re.compile(r"starting new backup on datastore '(?P<store>[^']+)'.*?: \"(?P<path>[^\"]+)\"")
_ARCHIVE_RE = re.compile(r"Upload statistics for '(?P<name>[^']+)'")
_CHUNKS_RE = re.compile(r"Chunk count: (?P<n>\d+)")
_UPLOAD_RE = re.compile(r"Upload size: (?P<bytes>\d+) \(")
_DUP_RE = re.compile(r"Duplicates: (?P<client>\d+)\+(?P<server>\d+) \(")
_COMP_RE = re.compile(r"Compression: (?P<pct>\d+)%")
_TIME_RE = re.compile(r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ$")


def parse_snapshot_path(path: str) -> dict[str, Any] | None:
    """Namespace, type, id and time of a snapshot from its directory as PBS
    logs it: ``[ns/<a>/ns/<b>/]<type>/<id>/<time>``, possibly below the
    datastore's own path."""
    parts = [p for p in path.split("/") if p]
    if len(parts) < 3 or not _TIME_RE.match(parts[-1]):
        return None
    backup_time = int(datetime.strptime(parts[-1], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC).timestamp())
    rest = parts[:-3]
    namespace: list[str] = []
    while len(rest) >= 2 and rest[-2] == "ns":
        namespace.insert(0, rest[-1])
        rest = rest[:-2]
    return {"ns": "/".join(namespace), "backup_type": parts[-3], "backup_id": parts[-2], "backup_time": backup_time}


def parse_backup_log(lines: list[str]) -> dict[str, Any] | None:
    """What one backup task wrote, from its log: the snapshot it made, the
    bytes it uploaded and — the estimate — the bytes that took on disk.
    None when the log names no snapshot."""
    snapshot: dict[str, Any] | None = None
    archives: list[dict[str, int]] = []
    current: dict[str, int] | None = None
    for line in lines:
        if snapshot is None and (match := _START_RE.search(line)):
            snapshot = parse_snapshot_path(match.group("path"))
            if snapshot is None:
                return None
            snapshot["store"] = match.group("store")
        elif _ARCHIVE_RE.search(line):
            current = {}
            archives.append(current)
        elif current is not None:
            if match := _CHUNKS_RE.search(line):
                current["chunks"] = int(match.group("n"))
            elif match := _UPLOAD_RE.search(line):
                current["upload"] = int(match.group("bytes"))
            elif match := _DUP_RE.search(line):
                current["client_dup"] = int(match.group("client"))
                current["server_dup"] = int(match.group("server"))
            elif match := _COMP_RE.search(line):
                current["comp"] = int(match.group("pct"))
    if snapshot is None:
        return None
    upload = stored = 0
    for archive in archives:
        size = archive.get("upload", 0)
        upload += size
        compressed = size * archive.get("comp", 100) / 100
        # Chunks the client sent although the server had them already: sent,
        # but not written. PBS logs their number, not their size.
        sent = archive.get("chunks", 0) - archive.get("client_dup", 0)
        if sent > 0:
            compressed *= max(0, sent - archive.get("server_dup", 0)) / sent
        stored += int(compressed)
    return snapshot | {"upload_bytes": upload, "stored_bytes": stored}


def _task_log(conn: dict[str, Any], upid: str) -> list[str]:
    path = f"/api2/json/nodes/localhost/tasks/{parse.quote(upid, safe='')}/log"
    rows = _pbs_get_payload(conn, path, query={"start": "0", "limit": str(_LOG_LINES)}, retries=0).get("data")
    return [str(row.get("t") or "") for row in rows] if isinstance(rows, list) else []


def _backup_tasks(conn: dict[str, Any], oldest: int) -> list[dict[str, Any]]:
    """Finished, successful backup tasks back to `oldest` (epoch), newest first."""
    tasks: list[dict[str, Any]] = []
    for page in range(_TASK_PAGES):
        rows = _pbs_get_payload(
            conn,
            "/api2/json/nodes/localhost/tasks",
            query={"typefilter": "backup", "start": str(page * _TASK_PAGE), "limit": str(_TASK_PAGE)},
            retries=0,
        ).get("data")
        if not isinstance(rows, list) or not rows:
            break
        for row in rows:
            status = str(row.get("status") or "")
            if row.get("worker_type") == "backup" and row.get("endtime") and (status == "OK" or "WARNINGS" in status):
                tasks.append(row)
        if len(rows) < _TASK_PAGE or int(rows[-1].get("starttime") or 0) < oldest:
            break
    return tasks


def canonical_ids(conns: list[dict[str, Any]]) -> dict[int, int]:
    """A PBS registered for several clusters is one PBS: every registration's
    id mapped to the lowest one, under which its snapshots are kept — so its
    task logs are read once, not once per cluster."""
    by_url: dict[str, int] = {}
    for conn in sorted(conns, key=lambda c: c["id"]):
        by_url.setdefault(str(conn.get("api_url") or "").rstrip("/").lower(), conn["id"])
    return {conn["id"]: by_url[str(conn.get("api_url") or "").rstrip("/").lower()] for conn in conns}


def collect(conn: dict[str, Any], listed: dict[tuple[str, str], list[dict[str, Any]]]) -> int:
    """Remember the snapshots just listed on this PBS (`listed`: per datastore
    and namespace read in full) and read the task logs of those whose upload
    is not known yet. Returns how many logs were read."""
    pbs_id = canonical_ids(db.list_pbs_connections()).get(conn["id"], conn["id"])
    db.replace_pbs_snapshots(pbs_id, listed)
    missing = db.pbs_snapshots_without_stats(pbs_id)
    if not missing:
        return 0
    # worker_id of a backup task: "<store>:<type>/<id>" — no namespace, no time.
    wanted = {f"{row['store']}:{row['backup_type']}/{row['backup_id']}" for row in missing}
    # The task starts shortly before the time its snapshot carries.
    oldest = min(row["backup_time"] for row in missing) - 3600
    read_before = db.pbs_backup_tasks_read(pbs_id)
    read = 0
    for task in _backup_tasks(conn, oldest):
        upid = str(task.get("upid") or "")
        started = int(task.get("starttime") or 0)
        if not upid or upid in read_before or started < oldest or str(task.get("worker_id") or "") not in wanted:
            continue
        if read >= LOGS_PER_SYNC:
            break
        read += 1
        db.save_pbs_backup_task(pbs_id, upid, started, parse_backup_log(_task_log(conn, upid)))
    db.forget_pbs_backup_tasks(pbs_id)
    return read


def _under(ns: str, root: str) -> str | None:
    """`ns` relative to the namespace `root`, or None when it is not below it."""
    if not root:
        return ns
    if ns == root:
        return ""
    return ns[len(root) + 1 :] if ns.startswith(root + "/") else None


def _sources(
    pbs_id: int, store: str, ns: str, jobs: list[dict[str, Any]], canon: dict[int, int]
) -> list[tuple[int, str, str]]:
    """Where a sync job copies the snapshots of this datastore namespace from:
    (PBS, datastore, namespace)."""
    found: list[tuple[int, str, str]] = []
    for job in jobs:
        local = canon.get(job["pbs_id"], job["pbs_id"])
        remote_id = job.get("remote_pbs_id")
        # No remote named: a job between two datastores of the same PBS.
        remote = canon.get(remote_id, remote_id) if remote_id else (None if job.get("remote_name") else local)
        if job.get("direction") == "push":
            target, t_store, t_ns = remote, job.get("remote_store") or "", job.get("remote_ns") or ""
            source, s_store, s_ns = local, job["store"], job.get("ns") or ""
        else:
            target, t_store, t_ns = local, job["store"], job.get("ns") or ""
            source, s_store, s_ns = remote, job.get("remote_store") or "", job.get("remote_ns") or ""
        rest = _under(ns, t_ns)
        if source is None or target != pbs_id or t_store != store or rest is None:
            continue
        found.append((source, s_store, "/".join(filter(None, (s_ns, rest)))))
    return found


def usage(pbs_id: int, store: str) -> dict[str, Any]:
    """The guests with backups on this datastore and what their snapshots
    took, largest first. A snapshot a sync job copied here has no upload of
    its own: it counts with what it took where it was backed up to."""
    canon = canonical_ids(db.list_pbs_connections())
    pbs_id = canon.get(pbs_id, pbs_id)
    jobs = db.list_pbs_sync_jobs()
    rows = db.list_pbs_snapshots(pbs_id, store)

    cache: dict[tuple[int, str, str], dict[tuple[str, str, int], int]] = {}

    def known(source: tuple[int, str, str]) -> dict[tuple[str, str, int], int]:
        if source not in cache:
            cache[source] = {
                (r["backup_type"], r["backup_id"], r["backup_time"]): r["stored_bytes"]
                for r in db.list_pbs_snapshots(*source)
                if r["stored_bytes"] is not None
            }
        return cache[source]

    def from_source(key: tuple[str, str, int], at: tuple[int, str, str], depth: int = 0) -> int | None:
        for source in _sources(*at, jobs, canon):
            value = known(source).get(key)
            if value is None and depth < 3:  # a copy of a copy
                value = from_source(key, source, depth + 1)
            if value is not None:
                return value
        return None

    groups: dict[tuple[str, str, str], dict[str, Any]] = {}
    for row in rows:
        group = groups.setdefault(
            (row["ns"], row["backup_type"], row["backup_id"]),
            {
                "ns": row["ns"],
                "backup_type": row["backup_type"],
                "backup_id": row["backup_id"],
                "name": "",
                "snapshots": 0,
                "known": 0,
                "stored_bytes": 0,
                "latest": 0,
            },
        )
        group["snapshots"] += 1
        if row["backup_time"] >= group["latest"]:
            group["latest"] = row["backup_time"]
            group["name"] = row.get("guest_name") or group["name"]
        stored = row["stored_bytes"]
        if stored is None:
            stored = from_source((row["backup_type"], row["backup_id"], row["backup_time"]), (pbs_id, store, row["ns"]))
        if stored is not None:
            group["known"] += 1
            group["stored_bytes"] += stored
    ordered = sorted(groups.values(), key=lambda g: (-g["stored_bytes"], g["ns"], g["backup_id"]))
    for group in ordered:
        del group["latest"]
    return {
        "groups": ordered,
        "stored_bytes": sum(g["stored_bytes"] for g in ordered),
        "snapshots": sum(g["snapshots"] for g in ordered),
        "known": sum(g["known"] for g in ordered),
    }
