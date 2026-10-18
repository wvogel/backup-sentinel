from __future__ import annotations

import json
import logging
import re
from collections.abc import Iterable, Mapping
from datetime import UTC, datetime
from typing import Any
from urllib import parse

from app.i18n import ts
from app.services.api_client import APIError, api_get_json, api_get_payload, api_test_connection
from app.services.backup_location import connection_location
from app.services.vzdump_log import guest_name_from_notes

logger = logging.getLogger(__name__)


class PBSSyncError(APIError):
    pass


def _pbs_get_payload(conn: dict[str, Any], path: str, **kwargs: Any) -> dict[str, Any]:
    auth_header = f"PBSAPIToken={conn['api_token_id']}:{conn['api_token_secret']}"
    try:
        return api_get_payload(conn["api_url"].rstrip("/"), path, auth_header, label="PBS", **kwargs)
    except APIError as exc:
        raise PBSSyncError(str(exc)) from exc


def _pbs_get_json(
    base_url: str,
    path: str,
    token_id: str,
    token_secret: str,
    **kwargs: Any,
) -> list[dict[str, Any]]:
    auth_header = f"PBSAPIToken={token_id}:{token_secret}"
    try:
        return api_get_json(base_url, path, auth_header, label="PBS", **kwargs)
    except APIError as exc:
        raise PBSSyncError(str(exc)) from exc


def _is_encrypted(files: list[dict[str, Any]] | None) -> bool | None:
    """Determine encryption state from PBS snapshot file list."""
    if not files:
        return None
    data_files = [
        f
        for f in files
        if isinstance(f, dict) and not str(f.get("filename", "")).endswith((".log.blob", ".catalog.pcat1.didx"))
    ]
    if not data_files:
        return None
    # A running backup has no manifest yet: PBS lists its files without
    # crypt-mode. That is "not known yet", not "unencrypted".
    modes = {str(f.get("crypt-mode") or "") for f in data_files}
    if "encrypt" in modes:
        return True
    if modes <= {"none", "sign-only"}:
        return False
    return None


def test_pbs_connection(conn: dict[str, Any], timeout: float = 5.0) -> None:
    """Test PBS connectivity. Raises PBSSyncError on failure."""
    base_url = conn["api_url"].rstrip("/")
    token_id = conn["api_token_id"]
    token_secret = conn["api_token_secret"]
    auth_header = f"PBSAPIToken={token_id}:{token_secret}"
    try:
        api_test_connection(
            f"{base_url}/api2/json/status/datastore-usage",
            auth_header,
            timeout=timeout,
            label="PBS",
        )
    except APIError as exc:
        raise PBSSyncError(str(exc)) from exc


def fetch_pbs_datastores(conn: dict[str, Any]) -> tuple[list[str], str, bool]:
    """Fetch the list of datastore names from PBS.

    Tries three API endpoints in order and returns the first that yields results.
    Returns (names, debug_info, ok) so callers can include raw response in sync
    logs; `ok` is False when no names were found and an endpoint errored.
    """
    base_url = conn["api_url"].rstrip("/")
    token_id = conn["api_token_id"]
    token_secret = conn["api_token_secret"]

    endpoints = [
        ("/api2/json/admin/datastore", "name"),
        ("/api2/json/config/datastore", "name"),
        ("/api2/json/status/datastore-usage", "store"),
    ]

    debug_lines: list[str] = []
    had_error = False
    for path, key in endpoints:
        try:
            data = _pbs_get_json(base_url, path, token_id, token_secret)
            raw = json.dumps(data, default=str)[:500]
            debug_lines.append(f"{path}: {len(data)} entries, raw={raw}")
            logger.info("PBS %s %s: %d entries", base_url, path, len(data))
            if data:
                names: list[str] = []
                for entry in data:
                    name = entry.get(key) or entry.get("store") or entry.get("name")
                    if name:
                        names.append(str(name))
                if names:
                    return names, " | ".join(debug_lines), True
        except PBSSyncError as exc:
            had_error = True
            debug_lines.append(f"{path}: ERROR {exc}")
            logger.warning("PBS %s %s failed: %s", base_url, path, exc)

    return [], " | ".join(debug_lines), not had_error


def fetch_pbs_namespaces(conn: dict[str, Any], datastore: str, extra: Iterable[str] = ()) -> list[str]:
    """Namespaces of a datastore this connection covers.

    Without a configured namespace the root is read, as other namespaces may
    hold other clusters' guests with the same VMIDs; with one (e.g. the target
    of a pull sync) it and its children. `extra` adds namespaces known to hold
    this cluster's copies — sync jobs writing there from its own datastores —
    with their children, if present on this datastore."""
    scope = str(conn.get("namespace") or "")
    extra = sorted({ns for ns in extra if ns})
    if not scope and not extra:
        return [""]
    rows = _pbs_get_json(
        conn["api_url"].rstrip("/"),
        f"/api2/json/admin/datastore/{parse.quote(datastore, safe='')}/namespace",
        conn["api_token_id"],
        conn["api_token_secret"],
    )
    available = {str(row.get("ns") or "") for row in rows}

    def covered(ns: str, root: str) -> bool:
        return ns == root or ns.startswith(f"{root}/")

    wanted = {ns for ns in available if any(covered(ns, root) for root in [scope] if scope)}
    if not scope:
        wanted.add("")
    wanted |= {ns for ns in available if any(covered(ns, root) for root in extra)}
    return sorted(wanted)


def fetch_pbs_backups(
    conn: dict[str, Any],
    vmids: set[int],
    cutoff: datetime | None = None,
    datastore: str = "",
    extra_namespaces: Iterable[str] = (),
) -> list[dict[str, Any]]:
    """Query PBS directly for backup snapshots in a specific datastore."""
    base_url = conn["api_url"].rstrip("/")
    token_id = conn["api_token_id"]
    token_secret = conn["api_token_secret"]
    conn_label = f"{base_url}/{datastore}"

    snapshots: list[tuple[str, dict[str, Any]]] = []
    try:
        for namespace in fetch_pbs_namespaces(conn, datastore, extra_namespaces):
            rows = _pbs_get_json(
                base_url,
                f"/api2/json/admin/datastore/{parse.quote(datastore, safe='')}/snapshots",
                token_id,
                token_secret,
                query={"ns": namespace} if namespace else None,
            )
            snapshots.extend((namespace, snap) for snap in rows)
    except PBSSyncError as exc:
        logger.error("PBS fetch failed for %s: %s", conn_label, exc)
        raise

    logger.info("PBS %s: %d total snapshots", conn_label, len(snapshots))

    events: list[dict[str, Any]] = []
    skipped_vmid = skipped_old = 0

    for namespace, snap in snapshots:
        backup_type = str(snap.get("backup-type") or "")
        if backup_type not in ("vm", "ct"):
            continue

        try:
            vmid = int(snap["backup-id"])
        except (KeyError, TypeError, ValueError):
            continue

        if vmids and vmid not in vmids:
            skipped_vmid += 1
            continue

        ts_raw = snap.get("backup-time")
        if ts_raw is None:
            continue
        try:
            started_at = datetime.fromtimestamp(int(ts_raw), UTC).replace(second=0, microsecond=0)
        except (TypeError, ValueError, OSError):
            continue

        if cutoff is not None and started_at < cutoff:
            skipped_old += 1
            continue

        size_bytes: int | None = None
        try:
            if snap.get("size") is not None:
                size_bytes = int(snap["size"])
        except (TypeError, ValueError):
            pass

        encrypted = _is_encrypted(snap.get("files"))

        verify_state: str | None = None
        verification = snap.get("verification")
        if isinstance(verification, dict):
            vs = str(verification.get("state") or "")
            verify_state = vs if vs else None

        events.append(
            {
                "vmid": vmid,
                "upid": "",
                "started_at": started_at,
                "finished_at": None,
                "duration_seconds": None,
                "size_bytes": size_bytes,
                "encrypted": encrypted,
                "verify_state": verify_state,
                "status": "ok",
                "storage": "/".join(filter(None, (conn.get("name") or base_url, datastore, namespace))),
                "guest_name": guest_name_from_notes(snap.get("comment")),
                "location": connection_location(conn, datastore, namespace),
            }
        )

    logger.info(
        "PBS %s: %d events for %d VMs (skipped: %d not-in-cluster, %d too-old)",
        conn_label,
        len(events),
        len({e["vmid"] for e in events}),
        skipped_vmid,
        skipped_old,
    )
    return events


def fetch_all_pbs_backups(
    conn: dict[str, Any],
    vmids: set[int],
    cutoff: datetime | None = None,
    extra_namespaces: Mapping[str, Iterable[str]] | None = None,
) -> tuple[list[dict[str, Any]], list[str], bool]:
    """Fetch backups from all datastores on this PBS.

    Returns (events, log_lines, complete); `complete` is False when the
    datastore list or any datastore could not be read.
    """
    log_lines: list[str] = []

    datastores, _debug_info, complete = fetch_pbs_datastores(conn)
    if not datastores:
        log_lines.append(ts("synclog.pbs_no_datastores"))
        return [], log_lines, complete

    log_lines.append(ts("synclog.pbs_datastores", names=", ".join(datastores)))
    all_events: list[dict[str, Any]] = []

    for ds in datastores:
        try:
            events = fetch_pbs_backups(
                conn, vmids, cutoff, datastore=ds, extra_namespaces=(extra_namespaces or {}).get(ds, ())
            )
            all_events.extend(events)
            log_lines.append(ts("synclog.pbs_datastore_done", ds=ds, count=len(events)))
        except PBSSyncError as exc:
            complete = False
            log_lines.append(ts("synclog.pbs_datastore_failed", ds=ds, error=exc))
            logger.error("PBS datastore %s/%s failed: %s", conn["api_url"], ds, exc)

    pbs_vmids = {e["vmid"] for e in all_events}
    log_lines.append(ts("synclog.pbs_total", events=len(all_events), vms=len(pbs_vmids)))
    return all_events, log_lines, complete


def _timestamp(value: Any) -> datetime | None:
    try:
        return datetime.fromtimestamp(int(value), UTC) if value else None
    except (TypeError, ValueError, OSError):
        return None


def _upid_start(upid: Any) -> datetime | None:
    """Start time of a PBS task from its UPID
    (UPID:node:pid:pstart:task_id:starttime:type:id:user:, hex fields)."""
    parts = str(upid or "").split(":")
    try:
        return datetime.fromtimestamp(int(parts[5], 16), UTC) if len(parts) > 5 and parts[0] == "UPID" else None
    except (ValueError, OSError):
        return None


_SYNC_PERCENT_RE = re.compile(r"percentage done:\s*([\d.]+)\s*%")


# Enough log lines to hold the latest "percentage done" and the end marker.
_SYNC_LOG_TAIL = 200


def sync_task_state(conn: dict[str, Any], upid: str) -> tuple[float | None, bool]:
    """(percent done, finished) of a sync task, from the end of its task log
    ("percentage done: 45.31% (9/20 groups, …)"). Reads only the last lines:
    a long sync's log has many thousands.

    PBS's task log API answers with the line count ("total", lines numbered
    from 1, `start` skips lines below it) and, with test-status, whether the
    task is still "active"; the node in the path is not used (the UPID names it).
    """
    path = f"/api2/json/nodes/localhost/tasks/{parse.quote(upid, safe='')}/log"
    head = _pbs_get_payload(conn, path, query={"start": "0", "limit": "1", "test-status": "1"}, retries=0)
    total = head.get("total")
    if isinstance(total, int):
        start = max(0, total - _SYNC_LOG_TAIL)
        rows = _pbs_get_payload(conn, path, query={"start": str(start), "limit": str(_SYNC_LOG_TAIL)}, retries=0).get(
            "data"
        )
    else:  # no line count reported: read it all
        rows = _pbs_get_payload(conn, path, query={"start": "0", "limit": "100000"}, retries=0).get("data")
    rows = rows if isinstance(rows, list) else []
    active = head.get("active")
    # Older PBS without "active": a finished task's log ends with "TASK OK/ERROR".
    finished = (
        active is False
        if isinstance(active, bool)
        else (bool(rows) and str(rows[-1].get("t") or "").startswith("TASK "))
    )
    for row in reversed(rows):
        match = _SYNC_PERCENT_RE.search(str(row.get("t") or ""))
        if match:
            return round(float(match.group(1)), 2), finished
    return None, finished


def fetch_pbs_sync_jobs(conn: dict[str, Any], retries: int = 2) -> list[dict[str, Any]]:
    """Sync jobs on this PBS (pull, push and local), with the remote resolved
    to its host and fingerprint so it can be matched to a registered PBS.
    `retries=0` for the frequent live check (it is asked again soon)."""
    base_url = conn["api_url"].rstrip("/")
    token_id = conn["api_token_id"]
    token_secret = conn["api_token_secret"]
    path = "/api2/json/admin/sync"
    try:
        # Push jobs (PBS 3.3+) are only listed on request.
        jobs = _pbs_get_json(base_url, path, token_id, token_secret, query={"sync-direction": "all"}, retries=retries)
    except PBSSyncError as exc:
        if "HTTP 400" not in str(exc):
            raise
        jobs = _pbs_get_json(base_url, path, token_id, token_secret, retries=retries)
    remotes = {
        str(r.get("name")): r
        for r in _pbs_get_json(base_url, "/api2/json/config/remote", token_id, token_secret, retries=retries)
    }
    result: list[dict[str, Any]] = []
    for job in jobs:
        if not job.get("id") or not job.get("store"):
            continue
        remote_name = str(job.get("remote") or "")
        remote = remotes.get(remote_name, {})
        result.append(
            {
                "job_id": str(job["id"]),
                "direction": "push" if job.get("sync-direction") == "push" else "pull",
                "store": str(job["store"]),
                "ns": str(job.get("ns") or ""),
                "remote_name": remote_name,
                "remote_host": str(remote.get("host") or ""),
                "remote_fingerprint": str(remote.get("fingerprint") or ""),
                "remote_store": str(job.get("remote-store") or ""),
                "remote_ns": str(job.get("remote-ns") or ""),
                "schedule": str(job.get("schedule") or ""),
                "last_run_state": str(job.get("last-run-state") or ""),
                "last_run_at": _timestamp(job.get("last-run-endtime")),
                # While a job runs PBS reports only its task UPID, no end or state.
                "running_since": None if job.get("last-run-endtime") else _upid_start(job.get("last-run-upid")),
                "running_upid": None,
                "next_run_at": _timestamp(job.get("next-run")),
            }
        )
    # Progress is fetched live by the cluster page, never stored.
    for job, raw in zip(result, [j for j in jobs if j.get("id") and j.get("store")], strict=True):
        if job["running_since"]:
            job["running_upid"] = str(raw.get("last-run-upid"))
    return result


def fetch_pbs_cert_fingerprints(conn: dict[str, Any]) -> list[str]:
    """Fingerprints of the certificates this PBS serves, to recognise it when
    PVE or another PBS knows it under a different name or address."""
    rows = _pbs_get_json(
        conn["api_url"].rstrip("/"),
        "/api2/json/nodes/localhost/certificates/info",
        conn["api_token_id"],
        conn["api_token_secret"],
    )
    return [str(row["fingerprint"]) for row in rows if row.get("fingerprint")]


def fetch_datastore_usage(conn: dict[str, Any]) -> list[dict[str, Any]]:
    """Capacity of every datastore on this PBS, incl. PBS's own full-date estimate.

    Datastores reporting an error (e.g. not mounted) are skipped.
    """
    rows = _pbs_get_json(
        conn["api_url"].rstrip("/"),
        "/api2/json/status/datastore-usage",
        conn["api_token_id"],
        conn["api_token_secret"],
    )
    now = datetime.now(UTC)
    result: list[dict[str, Any]] = []
    for row in rows:
        if row.get("error") or not row.get("store") or row.get("total") is None:
            continue
        estimate = row.get("estimated-full-date")
        try:
            full_at = datetime.fromtimestamp(int(estimate), UTC) if estimate and int(estimate) > 0 else None
        except (TypeError, ValueError, OSError):
            full_at = None
        # PBS extrapolates the last month linearly; shrinking usage (prune/GC)
        # puts the 100 % crossing in the past, meaning it is not filling up.
        if full_at is not None and full_at <= now:
            full_at = None
        result.append(
            {
                "store": str(row["store"]),
                "total_bytes": int(row.get("total") or 0),
                "used_bytes": int(row.get("used") or 0),
                "avail_bytes": int(row.get("avail") or 0),
                "estimated_full_at": full_at,
            }
        )
    return result


def assess_datastore_usage(
    used_bytes: int, total_bytes: int, estimated_full_at: datetime | None, now: datetime | None = None
) -> str:
    """ok / warning (≥ 80 % or full within 60 days) / critical (≥ 90 % or within 14 days)."""
    if total_bytes <= 0:
        return "ok"
    pct = used_bytes / total_bytes * 100
    days_left = None
    if estimated_full_at is not None:
        days_left = (estimated_full_at - (now or datetime.now(UTC))).total_seconds() / 86400
        if days_left < 0:  # a past estimate means shrinking usage, see fetch_datastore_usage
            days_left = None
    if pct >= 90 or (days_left is not None and days_left <= 14):
        return "critical"
    if pct >= 80 or (days_left is not None and days_left <= 60):
        return "warning"
    return "ok"
