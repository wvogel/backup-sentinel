from __future__ import annotations

import json
import logging
from datetime import UTC, datetime
from typing import Any
from urllib import parse

from app.i18n import ts
from app.services.api_client import APIError, api_get_json, api_test_connection
from app.services.backup_location import connection_location
from app.services.vzdump_log import guest_name_from_notes

logger = logging.getLogger(__name__)


class PBSSyncError(APIError):
    pass


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
    modes = {str(f.get("crypt-mode") or "none") for f in data_files}
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


def fetch_pbs_namespaces(conn: dict[str, Any], datastore: str) -> list[str]:
    """Namespaces of a datastore this connection covers.

    Without a configured namespace only the root is read, as other namespaces
    may hold other clusters' guests with the same VMIDs. With one (e.g. the
    target of a pull sync) it and its child namespaces are read, if present.
    """
    scope = str(conn.get("namespace") or "")
    if not scope:
        return [""]
    rows = _pbs_get_json(
        conn["api_url"].rstrip("/"),
        f"/api2/json/admin/datastore/{parse.quote(datastore, safe='')}/namespace",
        conn["api_token_id"],
        conn["api_token_secret"],
    )
    namespaces = {str(row.get("ns") or "") for row in rows}
    return sorted(ns for ns in namespaces if ns == scope or ns.startswith(f"{scope}/"))


def fetch_pbs_backups(
    conn: dict[str, Any],
    vmids: set[int],
    cutoff: datetime | None = None,
    datastore: str = "",
) -> list[dict[str, Any]]:
    """Query PBS directly for backup snapshots in a specific datastore."""
    base_url = conn["api_url"].rstrip("/")
    token_id = conn["api_token_id"]
    token_secret = conn["api_token_secret"]
    conn_label = f"{base_url}/{datastore}"

    snapshots: list[tuple[str, dict[str, Any]]] = []
    try:
        for namespace in fetch_pbs_namespaces(conn, datastore):
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
) -> tuple[list[dict[str, Any]], list[str], bool]:
    """Fetch backups from all datastores on this PBS.

    Returns (events, log_lines, complete); `complete` is False when the
    datastore list or any datastore could not be read.
    """
    log_lines: list[str] = []

    datastores, debug_info, complete = fetch_pbs_datastores(conn)
    log_lines.append(f"DEBUG: {debug_info}")
    if not datastores:
        log_lines.append(ts("synclog.pbs_no_datastores"))
        return [], log_lines, complete

    log_lines.append(ts("synclog.pbs_datastores", names=", ".join(datastores)))
    all_events: list[dict[str, Any]] = []

    for ds in datastores:
        try:
            events = fetch_pbs_backups(conn, vmids, cutoff, datastore=ds)
            all_events.extend(events)
            log_lines.append(ts("synclog.pbs_datastore_done", ds=ds, count=len(events)))
        except PBSSyncError as exc:
            complete = False
            log_lines.append(ts("synclog.pbs_datastore_failed", ds=ds, error=exc))
            logger.error("PBS datastore %s/%s failed: %s", conn["api_url"], ds, exc)

    pbs_vmids = {e["vmid"] for e in all_events}
    log_lines.append(ts("synclog.pbs_total", events=len(all_events), vms=len(pbs_vmids)))
    return all_events, log_lines, complete


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
