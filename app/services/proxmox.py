from __future__ import annotations

import logging
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any
from urllib import parse

from app.config import API_TIMEOUT
from app.i18n import ts
from app.services.api_client import APIError, api_get, api_get_dict, api_get_json, api_test_connection
from app.services.backup_location import pbs_location, pve_location
from app.services.vzdump_log import VmBackupRun, guest_name_from_notes, parse_vzdump_log

logger = logging.getLogger(__name__)


class ProxmoxSyncError(APIError):
    pass


@dataclass(slots=True)
class ProxmoxInventory:
    nodes: list[dict[str, str]]
    vms: list[dict[str, Any]]
    backup_events: list[dict[str, Any]]
    # Storages/nodes that could not be queried — backup data is incomplete.
    fetch_errors: list[str] = field(default_factory=list)
    # PBS storages of the cluster (where PVE backs up to); None when the
    # storage config could not be read.
    pbs_targets: list[dict[str, str]] | None = None
    # Quorum device: {} without one, None when it could not be asked.
    qdevice: dict[str, str] | None = None
    # HA-managed guests by VMID; None when the HA config could not be read.
    ha: dict[int, dict[str, Any]] | None = None


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


def fetch_cluster_inventory(
    cluster: dict[str, Any], timeout: float = API_TIMEOUT, known_vmids: set[int] | None = None
) -> ProxmoxInventory:
    """Fetch nodes, VMs and storage backups.

    `known_vmids` are VMIDs seen in earlier syncs; backups of those are still
    collected even if the VM itself no longer exists in PVE.
    """
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
                    "guest_type": str(row_type),
                    "pool": row.get("pool"),
                    "is_template": bool(int(row.get("template") or 0)),
                    "backup_kind": "daily",
                    "last_backup_at": None,
                }
            )

    logger.info("Found %d nodes, %d VMs", len(nodes), len(vms))

    backup_kind_by_vmid = infer_backup_kind_by_vmid(vms, backup_jobs)
    logger.info("Backup kind by VMID: %s", backup_kind_by_vmid)

    storage_config = fetch_storage_config(base_url, auth_header, timeout=timeout)
    latest_backup_by_vmid, backup_events, fetch_errors = fetch_backup_details(
        base_url,
        auth_header,
        nodes,
        vms,
        timeout=timeout,
        extra_vmids=known_vmids,
        storage_config=storage_config if storage_config is not None else [],
    )
    logger.info("Latest backups found for VMIDs: %s", sorted(latest_backup_by_vmid.keys()))

    storages_by_vmid = job_storages_by_vmid(vms, backup_jobs)
    for vm in vms:
        vm["backup_storages"] = storages_by_vmid.get(vm["vmid"], [])
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
        fetch_errors=fetch_errors,
        pbs_targets=pbs_storage_targets(storage_config) if storage_config is not None else None,
        qdevice=fetch_qdevice(base_url, auth_header, timeout=timeout),
        ha=fetch_ha(base_url, auth_header, timeout=timeout),
    )


def _ha_nodes(nodes: str) -> list[str]:
    """ "pve1:2,pve2" → ["pve1 (2)", "pve2"] (priority only when set)."""
    result = []
    for entry in str(nodes or "").split(","):
        name, _, prio = entry.strip().partition(":")
        if name:
            result.append(f"{name} ({prio})" if prio and prio != "0" else name)
    return result


def fetch_ha(base_url: str, auth_header: str, timeout: float = 10.0) -> dict[int, dict[str, Any]] | None:
    """HA-managed guests by VMID: {"state", "placement", "nodes"}, None when
    the HA config cannot be read.

    placement: "only" (may run only on `nodes`), "prefer", "never", "avoid"
    or None (any node). From node-affinity rules (PVE 9) or, on older PVE,
    from the guest's HA group ("restricted" = only)."""
    try:
        resources = _pve_get_json(base_url, "/api2/json/cluster/ha/resources", auth_header, timeout=timeout)
    except ProxmoxSyncError as exc:
        logger.warning("Cannot read HA resources: %s", exc)
        return None
    try:
        rules = _pve_get_json(base_url, "/api2/json/cluster/ha/rules", auth_header, timeout=timeout)
    except ProxmoxSyncError:
        rules = []  # PVE before 9: no rules
    try:
        groups = {
            g.get("group"): g
            for g in _pve_get_json(base_url, "/api2/json/cluster/ha/groups", auth_header, timeout=timeout)
        }
    except ProxmoxSyncError:
        groups = {}  # PVE 9: groups became rules

    placement_by_sid: dict[str, dict[str, Any]] = {}
    for rule in rules:
        if rule.get("type") != "node-affinity" or rule.get("disable"):
            continue
        negative = rule.get("affinity") == "negative"
        strict = bool(int(rule.get("strict") or 0))
        kind = ("never" if strict else "avoid") if negative else ("only" if strict else "prefer")
        for sid in str(rule.get("resources") or "").split(","):
            sid = sid.strip()
            # A strict rule says more than a preference; keep the first strict one.
            if sid and (
                sid not in placement_by_sid or (strict and placement_by_sid[sid]["placement"] in ("prefer", "avoid"))
            ):
                placement_by_sid[sid] = {"placement": kind, "nodes": _ha_nodes(rule.get("nodes"))}

    result: dict[int, dict[str, Any]] = {}
    for res in resources:
        sid = str(res.get("sid") or "")
        kind_, _, vmid = sid.partition(":")
        if kind_ not in ("vm", "ct") or not vmid.isdigit():
            continue
        info: dict[str, Any] = {"state": str(res.get("state") or ""), "placement": None, "nodes": []}
        if sid in placement_by_sid:
            info.update(placement_by_sid[sid])
        elif res.get("group") in groups:
            group = groups[res["group"]]
            info["placement"] = "only" if int(group.get("restricted") or 0) else "prefer"
            info["nodes"] = _ha_nodes(group.get("nodes"))
        result[int(vmid)] = info
    return result


# Fields of PVE's QDevice status ("status verbose" of corosync-qdevice) we keep.
_QDEVICE_FIELDS = {"State": "state", "QNetd host": "host", "Model": "model", "Algorithm": "algorithm"}


def fetch_qdevice(base_url: str, auth_header: str, timeout: float = 10.0) -> dict[str, str] | None:
    """The cluster's QDevice (corosync quorum device): {} without one, None
    when it cannot be asked. PVE answers from the node handling the request."""
    try:
        status = _pve_get_dict(base_url, "/api2/json/cluster/config/qdevice", auth_header, timeout=timeout)
    except ProxmoxSyncError as exc:
        logger.warning("Cannot read QDevice status: %s", exc)
        return None
    return {key: str(status[raw]) for raw, key in _QDEVICE_FIELDS.items() if status.get(raw)}


def fetch_storage_config(base_url: str, auth_header: str, timeout: float = 10.0) -> list[dict[str, Any]] | None:
    """Cluster storage config (/storage), or None when it cannot be read."""
    try:
        return _pve_get_json(base_url, "/api2/json/storage", auth_header, timeout=timeout)
    except ProxmoxSyncError as exc:
        logger.warning("Cannot read storage config: %s", exc)
        return None


def pbs_storage_targets(storage_config: list[dict[str, Any]]) -> list[dict[str, str]]:
    """Enabled PBS storages: the datastores PVE backs up to directly."""
    return [
        {
            "storage_id": str(s.get("storage") or ""),
            "server": str(s.get("server") or ""),
            "fingerprint": str(s.get("fingerprint") or ""),
            "datastore": str(s.get("datastore") or ""),
            "namespace": str(s.get("namespace") or ""),
        }
        for s in storage_config
        if s.get("type") == "pbs" and s.get("storage") and s.get("datastore") and not s.get("disable")
    ]


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


def job_storages_by_vmid(vms: list[dict[str, Any]], jobs: list[dict[str, Any]]) -> dict[int, list[str]]:
    """Storages each guest is backed up to directly by an enabled PVE backup
    job — as opposed to copies a PBS sync job pulls into a datastore, which
    PVE also lists as that storage's content.

    A job selects guests by `vmid` (list/ranges), `all` (minus `exclude`) or
    `pool`, optionally limited to one `node`."""
    result: dict[int, set[str]] = {}
    all_vmids = {vm["vmid"] for vm in vms}
    for job in jobs:
        if not isinstance(job, dict) or not job.get("storage"):
            continue
        if str(job.get("enabled", 1)) in {"0", "false", "False"}:
            continue
        if job.get("pool"):
            vmids = {vm["vmid"] for vm in vms if vm.get("pool") == job["pool"]}
        else:
            vmids = expand_job_vmids(str(job.get("vmid") or ""), all_vmids)
        if job.get("exclude"):
            vmids -= _parse_vmid_set(str(job["exclude"]))
        if job.get("node"):
            vmids &= {vm["vmid"] for vm in vms if vm.get("node_name") == job["node"]}
        for vmid in vmids & all_vmids:
            result.setdefault(vmid, set()).add(str(job["storage"]))
    return {vmid: sorted(storages) for vmid, storages in result.items()}


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
    extra_vmids: set[int] | None = None,
    storage_config: list[dict[str, Any]] | None = None,
) -> tuple[dict[int, datetime], list[dict[str, Any]], list[str]]:
    """Fetch backup inventory using BULK queries per storage (not per VM).

    Returns (latest backup per VMID, events, errors); `errors` lists nodes or
    storages that could not be queried, i.e. the event list is incomplete.
    """
    events: list[dict[str, Any]] = []
    errors: list[str] = []
    seen_volids: set[str] = set()
    latest: dict[int, datetime] = {}
    all_vmids = {vm["vmid"] for vm in vms} | (extra_vmids or set())

    node_storages: dict[str, list[dict[str, Any]]] = {}
    all_node_names = [n["name"] for n in nodes]

    # Cluster storage config: identifies the PBS behind a "pbs" storage
    # (server + fingerprint) so a second copy can be told from an alias.
    if storage_config is None:
        storage_config = fetch_storage_config(base_url, auth_header, timeout=timeout) or []
    storage_by_id = {str(s.get("storage")): s for s in storage_config}

    def _location(storage_id: str) -> str | None:
        cfg = storage_by_id.get(storage_id)
        if storage_types.get(storage_id) == "pbs" or (cfg and cfg.get("type") == "pbs"):
            if not cfg:
                return None  # unknown PBS — never count it as an extra copy
            return pbs_location(
                str(cfg.get("fingerprint") or ""),
                str(cfg.get("server") or ""),
                str(cfg.get("datastore") or ""),
                str(cfg.get("namespace") or ""),
            )
        return pve_location(storage_id)

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
            errors.append(ts("synclog.node_storages_failed", node=node_name, error=exc))
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
        errors.append(ts("synclog.storage_unreachable", storage=storage_id))
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
            "guest_name": guest_name_from_notes(item.get("notes")),
        }

    for vmid in sorted(all_vmids):
        vm_items = bulk_items_by_vmid.get(vmid, [])
        vm_count = 0
        for item in vm_items:
            parsed = _parse_item(item, vmid)
            if parsed is None:
                continue
            parsed["storage"] = item.get("_storage_id", "")
            parsed["storage_type"] = storage_types.get(parsed["storage"], "")
            parsed["location"] = _location(parsed["storage"])
            cfg = storage_by_id.get(parsed["storage"])
            if parsed["location"] and parsed["location"].startswith("pbs:") and cfg:
                parsed["pbs_target"] = {
                    "fingerprint": str(cfg.get("fingerprint") or ""),
                    "server": str(cfg.get("server") or ""),
                    "datastore": str(cfg.get("datastore") or ""),
                    "namespace": str(cfg.get("namespace") or ""),
                }
            events.append(parsed)
            vm_count += 1
            started_at = parsed["started_at"]
            if vmid not in latest or started_at > latest[vmid]:
                latest[vmid] = started_at
        logger.debug("VMID %d: %d backups found", vmid, vm_count)

    logger.info(
        "Backup scan complete: %d backups across %d VMs",
        len(events),
        len({e["vmid"] for e in events}),
    )
    return latest, events, errors


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


@dataclass(slots=True)
class FinishedBackupTask:
    upid: str
    ended_at: datetime
    runs: list[VmBackupRun]


def fetch_finished_backup_tasks(
    cluster: dict[str, Any],
    nodes: list[str],
    skip_upids: set[str],
    node_tz: Any,
    lookback_days: int = 31,
    timeout: float = API_TIMEOUT,
) -> list[FinishedBackupTask]:
    """Read the logs of finished vzdump tasks not processed yet.

    The task list covers `lookback_days` — the 30-day detail charts — so older
    backups get their details too and jobs running for days are still picked up
    once they end; only unseen UPIDs get their (large) log fetched.
    Node errors are logged and skipped — these details are best effort.
    """
    base_url = cluster["api_url"].rstrip("/")
    auth_header = build_token_header(cluster["api_user"], cluster["token_id"], cluster["token_secret"])
    since = int(datetime.now(UTC).timestamp()) - lookback_days * 86400
    result: list[FinishedBackupTask] = []
    for node_name in nodes:
        node_path = f"/api2/json/nodes/{parse.quote(node_name, safe='')}"
        try:
            tasks = _pve_get_json(
                base_url,
                f"{node_path}/tasks",
                auth_header,
                timeout=timeout,
                query={"typefilter": "vzdump", "since": str(since), "limit": "500", "source": "archive"},
            )
        except ProxmoxSyncError as exc:
            logger.warning("Cannot list finished backup tasks on node %s: %s", node_name, exc)
            continue
        for task in tasks:
            upid = str(task.get("upid") or "")
            if not upid or upid in skip_upids or not task.get("endtime"):
                continue
            try:
                log = _pve_get(
                    base_url,
                    f"{node_path}/tasks/{parse.quote(upid, safe='')}/log",
                    auth_header,
                    timeout=max(timeout, 30.0),
                    query={"start": "0", "limit": "50000"},
                )
            except ProxmoxSyncError as exc:
                logger.warning("Cannot read backup task log %s: %s", upid, exc)
                continue
            lines = [str(row.get("t", "")) for row in log if isinstance(row, dict)] if isinstance(log, list) else []
            result.append(
                FinishedBackupTask(
                    upid=upid,
                    ended_at=datetime.fromtimestamp(int(task["endtime"]), UTC),
                    runs=parse_vzdump_log(lines, node_tz),
                )
            )
    return result
