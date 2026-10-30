"""Glue between config, DB, matching, allocation, and the worker.

Builds a `RestoreServer` from the stored config, derives each VM's backup
locations, builds a `RunPlan`, enqueues due runs, and drains the queue one
active run at a time. Called from `auto_sync_loop`; every entry point is
defensive so a restore problem never breaks monitoring.
"""

from __future__ import annotations

import logging
from typing import Any

from app import db_restore
from app.db_core import connect
from app.services import restore_alloc, restore_config, restore_ip_collect, restore_match, restore_worker
from app.services.restore_match import PbsTuple
from app.services.restore_server import ParamikoSsh, PveApi, RestoreServer
from app.services.restore_worker import RunPlan

logger = logging.getLogger(__name__)


def build_server(config: dict[str, Any]) -> RestoreServer:
    pve = PveApi(config["api_url"], config["api_user"], config["token_id"], config["token_secret"])
    ssh = ParamikoSsh(
        config["ssh_host"],
        config.get("ssh_port", 22),
        config.get("ssh_user", "root"),
        config.get("ssh_private_key", ""),
    )
    return RestoreServer(config, pve, ssh)


def production_endpoints() -> set[str]:
    """Hosts of all known production PVE clusters and PBS servers — the restore
    host must not be one of them (spec §6 interlock)."""
    import re

    hosts: set[str] = set()
    with connect() as conn, conn.cursor() as cur:
        cur.execute("SELECT api_url FROM clusters")
        urls = [r["api_url"] for r in cur.fetchall()]
        cur.execute("SELECT api_url FROM pbs_connections")
        urls += [r["api_url"] for r in cur.fetchall()]
    for url in urls:
        m = re.search(r"https?://([^:/]+)", url or "")
        if m:
            hosts.add(m.group(1).lower())
    return hosts


def vm_backup_locations(vm_id: int) -> list[PbsTuple]:
    """The (pbs, datastore, namespace) tuples a VM's cluster backs up to, from
    pve_pbs_targets (spec §7)."""
    with connect() as conn, conn.cursor() as cur:
        cur.execute(
            """
            SELECT t.server, t.datastore, t.namespace
            FROM pve_pbs_targets t
            JOIN vms v ON v.cluster_id = t.cluster_id
            WHERE v.id = %s
            """,
            (vm_id,),
        )
        return [restore_match.normalize(r["server"], r["datastore"], r["namespace"]) for r in cur.fetchall()]


def _reservations(nics: list[dict[str, Any]]) -> list[dict[str, Any]]:
    res = []
    for nic in nics:
        if nic["ip_addresses"]:
            res.append({"mac": nic["mac"], "ip": nic["ip_addresses"][0], "prefix": nic.get("prefix") or 24})
    return res


class NotRunnable(RuntimeError):
    """A queued run cannot run and should be dequeued as an error (vs a resource
    wait, which returns None from build_plan)."""


def build_plan(candidate: dict[str, Any], server: RestoreServer, config: dict[str, Any]) -> RunPlan | None:
    """Resolve source/snapshot/NICs and allocate resources. Returns None only for
    a resource wait (no free management IP); raises NotRunnable when the VM is
    not auto-restorable or has no snapshot (those become an error, not a wait)."""
    vm_id = candidate["vm_id"]
    match = restore_match.match_source(vm_backup_locations(vm_id), server.list_pbs_storages())
    if match is None:
        raise NotRunnable("not auto-restorable (no matching local datastore)")
    snaps = server.list_snapshots(match["storage"], candidate["vmid"])
    snap = restore_match.pick_latest_complete(snaps)
    if snap is None:
        raise NotRunnable("no complete snapshot available")

    nics = [dict(n) for n in db_restore.get_network_interfaces(vm_id)]
    reservations = _reservations(nics)
    primary = restore_ip_collect.primary_ip(nics, candidate.get("probe_primary_ip"))
    ports = list(candidate.get("probe_tcp_ports") or [])
    targets = [{"ip": nic["ip_addresses"][0], "ports": ports} for nic in nics if nic["ip_addresses"]]

    alloc = restore_alloc.allocate(
        vmid_from=config["restore_vmid_from"],
        vmid_to=config["restore_vmid_to"],
        vlan_from=config["sandbox_vlan_from"],
        vlan_to=config["sandbox_vlan_to"],
        ip_from=config["mgmt_ip_from"],
        ip_to=config["mgmt_ip_to"],
        owned=[dict(r) for r in db_restore.active_and_held_runs()],
    )
    if alloc is None:
        return None  # management IP (or vmid/vlan) exhausted → caller waits

    targets_sorted = sorted(config.get("target_storages", []), key=lambda s: s.get("order", 0))
    target_storage = targets_sorted[0]["storage"] if targets_sorted else ""

    return RunPlan(
        vm_id=vm_id,
        restore_vmid=alloc.restore_vmid,
        helper_vmid=alloc.helper_vmid,
        vlan_tag=alloc.vlan_tag,
        mgmt_ip=alloc.mgmt_ip,
        source={
            "storage": match["storage"],
            "src_vmid": candidate["vmid"],
            "pbs": match["pbs"],
            "datastore": match["datastore"],
            "namespace": match["namespace"],
        },
        snapshot_id=snap["snapshot_id"],
        target_storage=target_storage,
        reservations=reservations,
        probe_targets=targets,
        primary_ip=primary,
        require_icmp=candidate.get("probe_require_icmp", True),
        tcp_ports=ports,
        hold_on_failure=candidate.get("hold_on_failure", False),
        retry_days=candidate.get("restore_retry_days", 7),
        first_delay=candidate.get("probe_first_delay_seconds") or config.get("probe_first_delay_seconds", 30),
        interval=candidate.get("probe_interval_seconds") or config.get("probe_interval_seconds", 30),
        window=candidate.get("probe_window_seconds") or config.get("probe_window_seconds", 300),
    )


def enqueue_due(server: RestoreServer, config: dict[str, Any]) -> int:
    """Create queued runs for due, auto-restorable VMs. Returns count enqueued."""
    storages = server.list_pbs_storages()
    count = 0
    for cand in db_restore.due_auto_restore_candidates():
        if restore_match.is_auto_restorable(vm_backup_locations(cand["vm_id"]), storages):
            db_restore.create_run(cand["vm_id"], trigger="scheduled")
            count += 1
    return count


def drain(config: dict[str, Any], server: RestoreServer) -> None:
    """Run queued runs one at a time until none is runnable (resource wait) or
    the queue is empty. The caller guarantees a single drainer (spec §15)."""
    if restore_config.queue_paused():
        return
    prod = production_endpoints()
    while True:
        if db_restore.has_active_run():
            return
        run = db_restore.next_queued_run()
        if run is None:
            return
        cand = _candidate_for_vm(run["vm_id"])
        if cand is None:
            db_restore.record_run_result(run["id"], run["vm_id"], "error", duration_seconds=0, status="error")
            continue
        try:
            plan = build_plan(cand, server, config)
        except NotRunnable as exc:
            logger.info("restore run %s dequeued: %s", run["id"], exc)
            db_restore.record_run_result(
                run["id"], run["vm_id"], "error", duration_seconds=0, checks={"error": str(exc)}, status="error"
            )
            continue
        if plan is None:
            # No free management IP: leave queued and wait (spec §15).
            logger.info("restore run %s waiting for a free management IP", run["id"])
            return
        result = restore_worker.execute(run["id"], plan, server, production_hosts=prod)
        _notify_result(run["vm_id"], result)


def _notify_result(vm_id: int, result: str) -> None:
    if result in ("passed", "limited"):
        return
    try:
        from app.services import notifications

        with connect() as conn, conn.cursor() as cur:
            cur.execute(
                "SELECT v.name AS vm_name, v.vmid, c.name AS cluster_name "
                "FROM vms v JOIN clusters c ON c.id = v.cluster_id WHERE v.id = %s",
                (vm_id,),
            )
            row = cur.fetchone()
        if not row:
            return
        if result == "held":
            notifications.notify_restore_held(row["cluster_name"], row["vm_name"], row["vmid"])
        else:
            notifications.notify_restore_failed(row["cluster_name"], row["vm_name"], row["vmid"], result)
    except Exception as exc:
        logger.debug("restore notification failed: %s", exc)


def _candidate_for_vm(vm_id: int) -> dict[str, Any] | None:
    with connect() as conn, conn.cursor() as cur:
        cur.execute(
            """
            SELECT v.id AS vm_id, v.vmid, v.node_name, v.guest_type,
                   p.hold_on_failure, p.probe_primary_ip, p.probe_tcp_ports, p.probe_require_icmp,
                   p.restore_retry_days, p.probe_window_seconds, p.probe_first_delay_seconds,
                   p.probe_interval_seconds
            FROM vms v LEFT JOIN vm_policies p ON p.vm_id = v.id WHERE v.id = %s
            """,
            (vm_id,),
        )
        return cur.fetchone()


def collect_cluster_ips(cluster: dict[str, Any], inventory_vms: list[dict[str, Any]]) -> None:
    """Best-effort guest IP collection for this cluster's *auto-restore-enabled*
    VMs only (keeps the sync cheap). Never raises (spec §9)."""
    try:
        with connect() as conn, conn.cursor() as cur:
            cur.execute(
                """
                SELECT v.id, v.vmid, v.node_name, v.guest_type, p.probe_primary_ip
                FROM vms v JOIN vm_policies p ON p.vm_id = v.id
                WHERE v.cluster_id = %s AND p.auto_restore_enabled
                """,
                (cluster["id"],),
            )
            enabled = {r["vmid"]: r for r in cur.fetchall()}
        if not enabled:
            return
        from app.services.api_client import api_get
        from app.services.proxmox import build_token_header

        base = cluster["api_url"].rstrip("/") + "/api2/json"
        auth = build_token_header(cluster["api_user"], cluster["token_id"], cluster["token_secret"])

        def pve_get(path: str) -> Any:
            return api_get(base, path, auth, label="PVE", retries=0)

        for vm in inventory_vms:
            row = enabled.get(vm["vmid"])
            if not row:
                continue
            nics = restore_ip_collect.collect_for_vm(
                {
                    "node_name": vm.get("node_name", row["node_name"]),
                    "vmid": vm["vmid"],
                    "guest_type": vm.get("guest_type", row["guest_type"]),
                },
                pve_get,
            )
            nics = restore_ip_collect.mark_primary(nics, row.get("probe_primary_ip"))
            db_restore.set_network_interfaces(row["id"], nics)
    except Exception as exc:
        logger.debug("cluster IP collection skipped for %s: %s", cluster.get("name"), exc)


def plan_from_run(run: dict[str, Any], config: dict[str, Any]) -> RunPlan:
    """Reconstruct a RunPlan from a stored (held) run for reap/retest/promote."""
    cand = _candidate_for_vm(run["vm_id"]) or {}
    nics = [dict(n) for n in db_restore.get_network_interfaces(run["vm_id"])]
    ports = list(cand.get("probe_tcp_ports") or [])
    targets = [{"ip": nic["ip_addresses"][0], "ports": ports} for nic in nics if nic["ip_addresses"]]
    primary = restore_ip_collect.primary_ip(nics, cand.get("probe_primary_ip"))
    tgt = (run.get("target_storages") or [""])[0]
    return RunPlan(
        vm_id=run["vm_id"],
        restore_vmid=run.get("restore_vmid") or 0,
        helper_vmid=run.get("helper_vmid") or 0,
        vlan_tag=run.get("vlan_tag") or 0,
        mgmt_ip=run.get("mgmt_ip") or "",
        source={
            "storage": "",
            "src_vmid": cand.get("vmid"),
            "pbs": run.get("source_pbs"),
            "datastore": run.get("source_datastore"),
            "namespace": run.get("source_namespace"),
        },
        snapshot_id=run.get("snapshot_id") or "",
        target_storage=tgt,
        reservations=_reservations(nics),
        probe_targets=targets,
        primary_ip=primary,
        require_icmp=cand.get("probe_require_icmp", True),
        tcp_ports=ports,
        hold_on_failure=cand.get("hold_on_failure", False),
        retry_days=cand.get("restore_retry_days", 7),
        first_delay=cand.get("probe_first_delay_seconds") or config.get("probe_first_delay_seconds", 30),
        interval=cand.get("probe_interval_seconds") or config.get("probe_interval_seconds", 30),
        window=cand.get("probe_window_seconds") or config.get("probe_window_seconds", 300),
    )


def run_restore_queue() -> None:
    """Entry point for the auto-sync loop: enqueue due + drain. Defensive."""
    try:
        config = restore_config.load()
        if config is None:
            return
        server = build_server(config)
        enqueue_due(server, config)
        drain(config, server)
    except Exception as exc:
        logger.error("restore queue processing failed: %s", exc)
