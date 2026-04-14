from __future__ import annotations

import logging
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from urllib import parse

from app.config import API_TIMEOUT
from app.services.api_client import APIError, api_get, api_get_dict, api_get_json, api_test_connection

logger = logging.getLogger(__name__)


class ProxmoxSyncError(APIError):
    pass


@dataclass(slots=True)
class ProxmoxInventory:
    nodes: list[dict[str, str]]
    vms: list[dict[str, Any]]
    backup_events: list[dict[str, Any]]


def build_token_header(api_user: str, token_id: str, token_secret: str) -> str:
    return f"PVEAPIToken={api_user}!{token_id}={token_secret}"


def _pve_get(base_url: str, path: str, auth_header: str, **kwargs: Any) -> Any:
    try:
        return api_get(base_url, path, auth_header, label="PVE", **kwargs)
    except APIError as exc:
        raise ProxmoxSyncError(str(exc)) from exc


def _pve_get_json(base_url: str, path: str, auth_header: str, **kwargs: Any) -> list[dict[str, Any]]:
    try:
        return api_get_json(base_url, path, auth_header, label="PVE", **kwargs)
    except APIError as exc:
        raise ProxmoxSyncError(str(exc)) from exc


def _pve_get_dict(base_url: str, path: str, auth_header: str, **kwargs: Any) -> dict[str, Any]:
    try:
        return api_get_dict(base_url, path, auth_header, label="PVE", **kwargs)
    except APIError as exc:
        raise ProxmoxSyncError(str(exc)) from exc


# Keep old names for backward compatibility in imports
proxmox_get_data = _pve_get
proxmox_get_json = _pve_get_json
proxmox_get_dict = _pve_get_dict


def fetch_cluster_inventory(cluster: dict[str, Any], timeout: float = API_TIMEOUT) -> ProxmoxInventory:
    base_url = cluster["api_url"].rstrip("/")
    auth_header = build_token_header(cluster["api_user"], cluster["token_id"], cluster["token_secret"])

    logger.info("Syncing cluster %s (%s)", cluster.get("name"), base_url)

    resources = _pve_get_json(base_url, "/api2/json/cluster/resources", auth_header, timeout=timeout)
    backup_jobs = _pve_get_json(base_url, "/api2/json/cluster/backup", auth_header, timeout=timeout)

    logger.info("Cluster resources: %d items, backup jobs: %d", len(resources), len(backup_jobs))
    for job in backup_jobs:
        logger.debug(
            "Backup job: id=%s schedule=%r vmid=%r enabled=%s",
            job.get("id"),
            job.get("schedule"),
            job.get("vmid"),
            job.get("enabled", 1),
        )

    nodes: list[dict[str, str]] = []
    vms: list[dict[str, Any]] = []

    for row in resources:
        row_type = row.get("type")
        if row_type == "node":
            node_name = row.get("node") or row.get("name")
            if node_name:
                nodes.append(
                    {
                        "name": str(node_name),
                        "status": "online" if row.get("status") == "online" else str(row.get("status") or "unknown"),
                    }
                )
        elif row_type in {"qemu", "lxc"}:
            vmid = row.get("vmid")
            node_name = row.get("node")
            if vmid is None or not node_name:
                continue
            vms.append(
                {
                    "node_name": str(node_name),
                    "vmid": int(vmid),
                    "name": str(row.get("name") or f"{row_type}-{vmid}"),
                    "backup_kind": "daily",
                    "last_backup_at": None,
                }
            )

    logger.info("Found %d nodes, %d VMs", len(nodes), len(vms))

    backup_kind_by_vmid = infer_backup_kind_by_vmid(vms, backup_jobs)
    logger.info("Backup kind by VMID: %s", backup_kind_by_vmid)

    latest_backup_by_vmid, backup_events = fetch_backup_details(base_url, auth_header, nodes, vms, timeout=timeout)
    logger.info("Latest backups found for VMIDs: %s", sorted(latest_backup_by_vmid.keys()))

    for vm in vms:
        vm["backup_kind"] = backup_kind_by_vmid.get(vm["vmid"], "none")
        vm["last_backup_at"] = latest_backup_by_vmid.get(vm["vmid"])
        logger.debug(
            "VM %s (vmid=%d): backup_kind=%s last_backup_at=%s",
            vm["name"],
            vm["vmid"],
            vm["backup_kind"],
            vm["last_backup_at"],
        )

    return ProxmoxInventory(
        nodes=dedupe_nodes(nodes),
        vms=sorted(vms, key=lambda item: (item["node_name"], item["vmid"])),
        backup_events=backup_events,
    )


def test_proxmox_connection(cluster: dict[str, Any], timeout: float = 5.0) -> None:
    """Test PVE API connectivity. Raises ProxmoxSyncError on failure."""
    base_url = cluster["api_url"].rstrip("/")
    auth_header = build_token_header(cluster["api_user"], cluster["token_id"], cluster["token_secret"])
    try:
        api_test_connection(f"{base_url}/api2/json/version", auth_header, timeout=timeout, label="PVE")
    except APIError as exc:
        raise ProxmoxSyncError(str(exc)) from exc


def dedupe_nodes(nodes: list[dict[str, str]]) -> list[dict[str, str]]:
    by_name: dict[str, dict[str, str]] = {}
    for node in nodes:
        by_name[node["name"]] = node
    return sorted(by_name.values(), key=lambda item: item["name"])


# ── Backup schedule classification ──────────────────────────────────────────


def infer_backup_kind_by_vmid(vms: list[dict[str, Any]], jobs: list[dict[str, Any]]) -> dict[int, str]:
    """Classify each VMID as 'daily', 'weekly', or leave unset (→ 'none').

    Resolution rules:
    1. A job with explicit VMIDs (specific) overrides any catch-all job (vmid="" / "all").
    2. Within the same specificity tier: daily beats weekly.
    This handles the common setup where a daily catch-all job coexists with
    specific weekly jobs for a subset of VMs.
    """
    all_vmids = {vm["vmid"] for vm in vms}
    result: dict[int, str] = {}
    specific: set[int] = set()

    for job in jobs:
        if not isinstance(job, dict):
            continue
        if str(job.get("enabled", 1)) in {"0", "false", "False"}:
            logger.debug("Skipping disabled backup job: %s", job.get("id"))
            continue
        schedule = job.get("schedule", "")
        backup_kind = classify_schedule(schedule)
        raw_vmid = str(job.get("vmid") or "").strip().lower()
        is_specific = bool(raw_vmid) and raw_vmid != "all"
        vmids = expand_job_vmids(raw_vmid, all_vmids)
        exclude_spec = str(job.get("exclude") or "").strip()
        if exclude_spec:
            vmids -= _parse_vmid_set(exclude_spec)
        logger.debug(
            "Job %s: schedule=%r → %s, specific=%s, covers %d VMIDs",
            job.get("id"),
            schedule,
            backup_kind,
            is_specific,
            len(vmids),
        )
        for vmid in vmids:
            if is_specific:
                if vmid in specific:
                    if backup_kind == "daily":
                        result[vmid] = "daily"
                else:
                    result[vmid] = backup_kind
                    specific.add(vmid)
            else:
                if vmid in specific:
                    continue
                if backup_kind == "daily" or vmid not in result:
                    result[vmid] = backup_kind

    return result


def _parse_vmid_set(vmid_spec: str) -> set[int]:
    """Parse a comma/range VMID spec into a set."""
    result: set[int] = set()
    for part in vmid_spec.strip().lower().split(","):
        item = part.strip()
        if not item:
            continue
        if "-" in item:
            start_raw, end_raw = item.split("-", 1)
            if start_raw.isdigit() and end_raw.isdigit():
                result.update(range(int(start_raw), int(end_raw) + 1))
        elif item.isdigit():
            result.add(int(item))
    return result


def classify_schedule(schedule: str) -> str:
    """Classify a Proxmox/systemd calendar schedule as 'daily' or 'weekly'."""
    lowered = schedule.lower().strip()
    if not lowered:
        return "weekly"

    if "daily" in lowered:
        return "daily"
    if "hourly" in lowered or "*/" in lowered:
        return "daily"
    if "weekly" in lowered or "monthly" in lowered or "yearly" in lowered or "annually" in lowered:
        return "weekly"

    weekdays = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]
    day_hits = sum(token in lowered for token in weekdays)

    if day_hits >= 2:
        return "daily"
    if day_hits == 1:
        return "weekly"

    if re.search(r"\*-\*-\*", lowered) or re.search(r"\d{4}-\*-\*", lowered):
        return "daily"

    if re.match(r"^\d{1,2}:\d{2}(:\d{2})?$", lowered):
        return "daily"

    logger.warning("Unrecognised schedule format %r, defaulting to weekly", schedule)
    return "weekly"


def expand_job_vmids(vmid_spec: str, all_vmids: set[int]) -> set[int]:
    cleaned = vmid_spec.strip().lower()
    if not cleaned or cleaned == "all":
        return set(all_vmids)
    result: set[int] = set()
    for part in cleaned.split(","):
        item = part.strip()
        if not item:
            continue
        if "-" in item:
            start_raw, end_raw = item.split("-", 1)
            if start_raw.isdigit() and end_raw.isdigit():
                result.update(range(int(start_raw), int(end_raw) + 1))
        elif item.isdigit():
            result.add(int(item))
        else:
            logger.warning("Unrecognised VMID spec token %r in job, skipping", item)
    return result


# ── Backup detail fetching (bulk per storage) ───────────────────────────────


def fetch_backup_details(
    base_url: str,
    auth_header: str,
    nodes: list[dict[str, str]],
    vms: list[dict[str, Any]],
    timeout: float = 10.0,
) -> tuple[dict[int, datetime], list[dict[str, Any]]]:
    """Fetch backup inventory using BULK queries per storage (not per VM)."""
    events: list[dict[str, Any]] = []
    seen_volids: set[str] = set()
    latest: dict[int, datetime] = {}
    all_vmids = {vm["vmid"] for vm in vms}

    node_storages: dict[str, list[dict[str, Any]]] = {}
    all_node_names = [n["name"] for n in nodes]

    def _fetch_node_storages(node_name: str) -> tuple[str, list[dict[str, Any]]]:
        try:
            raw = _pve_get_json(
                base_url,
                f"/api2/json/nodes/{parse.quote(node_name, safe='')}/storage",
                auth_header,
                timeout=timeout,
            )
        except ProxmoxSyncError as exc:
            logger.warning("Cannot list storages for node %s: %s", node_name, exc)
            raw = []
        storages = [s for s in raw if "backup" in str(s.get("content", "")) and s.get("storage")]
        logger.info("Node %s: backup storages: %s", node_name, [s["storage"] for s in storages])
        return node_name, storages

    with ThreadPoolExecutor(max_workers=len(nodes) or 1) as pool:
        for node_name, storages in pool.map(_fetch_node_storages, all_node_names):
            node_storages[node_name] = storages

    storage_query_node: dict[str, str] = {}
    storage_types: dict[str, str] = {}
    for node_name in all_node_names:
        for s in node_storages.get(node_name, []):
            sid = str(s["storage"])
            if sid not in storage_query_node:
                storage_query_node[sid] = node_name
                storage_types[sid] = str(s.get("type", "")).lower()

    logger.info("Unique backup storages: %s", list(storage_query_node.keys()))

    bulk_items_by_vmid: dict[int, list[dict[str, Any]]] = {}

    def _bulk_fetch_storage(storage_id: str, primary_node: str) -> tuple[str, list[dict[str, Any]]]:
        for query_node in dict.fromkeys([primary_node, *all_node_names]):
            path = (
                f"/api2/json/nodes/{parse.quote(query_node, safe='')}"
                f"/storage/{parse.quote(storage_id, safe='')}/content"
            )
            try:
                items = _pve_get_json(
                    base_url,
                    path,
                    auth_header,
                    timeout=max(timeout, 60.0),
                    query={"content": "backup"},
                )
            except ProxmoxSyncError as exc:
                logger.warning("Bulk fetch %s via %s failed: %s", storage_id, query_node, exc)
                continue
            logger.info("Bulk fetch %s via %s: %d items", storage_id, query_node, len(items))
            return storage_id, items
        logger.warning("Could not fetch backup list from storage %s on any node", storage_id)
        return storage_id, []

    with ThreadPoolExecutor(max_workers=len(storage_query_node) or 1) as pool:
        futures = [pool.submit(_bulk_fetch_storage, sid, node) for sid, node in storage_query_node.items()]
        for future in as_completed(futures):
            storage_id, items = future.result()
            for item in items:
                vmid = item.get("vmid")
                if vmid is None:
                    vmid = extract_vmid(item)
                if vmid is None:
                    continue
                vmid = int(vmid)
                if vmid not in all_vmids:
                    continue
                item["_storage_id"] = storage_id
                bulk_items_by_vmid.setdefault(vmid, []).append(item)

    def _parse_item(item: dict[str, Any], vmid: int) -> dict[str, Any] | None:
        volid = str(item.get("volid") or "")
        if volid:
            if volid in seen_volids:
                return None
            seen_volids.add(volid)

        ts_raw = item.get("backup-time") or item.get("ctime")
        if not ts_raw:
            return None
        try:
            started_at = datetime.fromtimestamp(int(ts_raw), UTC).replace(second=0, microsecond=0)
        except (TypeError, ValueError, OSError):
            return None

        size_bytes: int | None = None
        try:
            if item.get("size") is not None:
                size_bytes = int(item["size"])
        except (TypeError, ValueError):
            pass

        encrypted: bool | None = None
        enc_raw = item.get("encrypted")
        if enc_raw is not None:
            if isinstance(enc_raw, bool):
                encrypted = enc_raw
            elif str(enc_raw) in ("0", "false", "False", ""):
                encrypted = False
            else:
                encrypted = True

        verify_state: str | None = None
        verification = item.get("verification")
        if isinstance(verification, dict):
            vs = str(verification.get("state") or "")
            verify_state = vs if vs else None

        return {
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

    for vm in vms:
        vmid = vm["vmid"]
        vm_items = bulk_items_by_vmid.get(vmid, [])
        vm_count = 0
        for item in vm_items:
            parsed = _parse_item(item, vmid)
            if parsed is None:
                continue
            parsed["storage_type"] = storage_types.get(item.get("_storage_id", ""), "")
            events.append(parsed)
            vm_count += 1
            ts = parsed["started_at"]
            if vmid not in latest or ts > latest[vmid]:
                latest[vmid] = ts
        logger.debug("VM %s (vmid=%d): %d backups found", vm["name"], vmid, vm_count)

    logger.info(
        "Backup scan complete: %d backups across %d VMs",
        len(events),
        len({e["vmid"] for e in events}),
    )
    return latest, events


# ── VMID / datetime extraction helpers ──────────────────────────────────────

_VMID_RE = re.compile(r"\b(?:VM|CT)\s+(?P<vmid>\d+)\b|\b(?:qemu|lxc)[/-](?P<vmid2>\d+)\b", re.IGNORECASE)


def extract_vmid(item: dict[str, Any]) -> int | None:
    if item.get("vmid") is not None:
        return int(item["vmid"])
    raw_values = [
        item.get("volid"),
        item.get("volid-path"),
        item.get("text"),
        item.get("notes"),
    ]
    for raw_value in raw_values:
        value = str(raw_value or "")
        if not value:
            continue
        for pattern in (
            r"vzdump-(?:qemu|lxc)-(\d+)-",
            r"(?:^|[:/])(?:backup/)?(?:vm|ct)/(\d+)(?:/|$)",
            r"(?:^|[:/])(?:qemu|lxc)-(\d+)(?:-|/|$)",
        ):
            match = re.search(pattern, value)
            if match:
                return int(match.group(1))
    return None


def extract_backup_time(item: dict[str, Any]) -> datetime | None:
    for key in ("ctime", "backup-time", "starttime", "time"):
        raw_value = item.get(key)
        if raw_value in (None, ""):
            continue
        try:
            return datetime.fromtimestamp(int(raw_value), UTC)
        except (TypeError, ValueError, OSError):
            parsed = parse_datetime_string(str(raw_value))
            if parsed is not None:
                return parsed
    for key in ("volid", "volid-path", "text", "notes"):
        parsed = parse_datetime_string(str(item.get(key) or ""))
        if parsed is not None:
            return parsed
    return None


def parse_datetime_string(raw_value: str) -> datetime | None:
    value = raw_value.strip()
    if not value:
        return None

    iso_match = re.search(r"(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2})Z", value)
    if iso_match:
        try:
            return datetime.fromisoformat(f"{iso_match.group(1)}+00:00").astimezone(UTC)
        except ValueError:
            pass

    vzdump_match = re.search(r"(\d{4})_(\d{2})_(\d{2})[-T_](\d{2})_(\d{2})_(\d{2})", value)
    if vzdump_match:
        try:
            return datetime(
                int(vzdump_match.group(1)),
                int(vzdump_match.group(2)),
                int(vzdump_match.group(3)),
                int(vzdump_match.group(4)),
                int(vzdump_match.group(5)),
                int(vzdump_match.group(6)),
                tzinfo=UTC,
            )
        except ValueError:
            pass

    return None
