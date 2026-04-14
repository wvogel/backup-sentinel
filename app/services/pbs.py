from __future__ import annotations

import json
import logging
from datetime import UTC, datetime
from typing import Any
from urllib import parse

from app.services.api_client import APIError, api_get_json, api_test_connection

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


def fetch_pbs_datastores(conn: dict[str, Any]) -> tuple[list[str], str]:
    """Fetch the list of datastore names from PBS.

    Tries three API endpoints in order and returns the first that yields results.
    Returns (names, debug_info) so callers can include raw response in sync logs.
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
                    return names, " | ".join(debug_lines)
        except PBSSyncError as exc:
            debug_lines.append(f"{path}: ERROR {exc}")
            logger.warning("PBS %s %s failed: %s", base_url, path, exc)

    return [], " | ".join(debug_lines)


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

    try:
        snapshots = _pbs_get_json(
            base_url,
            f"/api2/json/admin/datastore/{parse.quote(datastore, safe='')}/snapshots",
            token_id,
            token_secret,
        )
    except PBSSyncError as exc:
        logger.error("PBS fetch failed for %s: %s", conn_label, exc)
        raise

    logger.info("PBS %s: %d total snapshots", conn_label, len(snapshots))

    events: list[dict[str, Any]] = []
    skipped_vmid = skipped_old = 0

    for snap in snapshots:
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
) -> tuple[list[dict[str, Any]], list[str]]:
    """Fetch backups from all datastores on this PBS."""
    log_lines: list[str] = []

    datastores, debug_info = fetch_pbs_datastores(conn)
    log_lines.append(f"DEBUG: {debug_info}")
    if not datastores:
        log_lines.append("Keine Datastores auf dem PBS gefunden.")
        return [], log_lines

    log_lines.append(f"Datastores: {', '.join(datastores)}")
    all_events: list[dict[str, Any]] = []

    for ds in datastores:
        try:
            events = fetch_pbs_backups(conn, vmids, cutoff, datastore=ds)
            all_events.extend(events)
            log_lines.append(f"  {ds}: {len(events)} Snapshots verarbeitet")
        except PBSSyncError as exc:
            log_lines.append(f"  {ds}: Fehler – {exc}")
            logger.error("PBS datastore %s/%s failed: %s", conn["api_url"], ds, exc)

    pbs_vmids = {e["vmid"] for e in all_events}
    log_lines.append(f"Gesamt: {len(all_events)} Events für {len(pbs_vmids)} VMs")
    return all_events, log_lines
