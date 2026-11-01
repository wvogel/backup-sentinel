"""Glue between config, DB, matching, allocation, and the worker.

Builds a `RestoreServer` from the stored config, derives each VM's backup
locations, builds a `RunPlan`, enqueues due runs, and drains the queue one
active run at a time. Called from `auto_sync_loop`; every entry point is
defensive so a restore problem never breaks monitoring.
"""

from __future__ import annotations

import logging
from typing import Any

from app import db, db_restore
from app.db_core import connect
from app.services import restore_alloc, restore_config, restore_ip_collect, restore_match, restore_worker
from app.services.restore_server import ParamikoSsh, PveApi, PveSsh, RestoreServer
from app.services.restore_worker import RunPlan

logger = logging.getLogger(__name__)


def build_server(config: dict[str, Any]) -> RestoreServer:
    # Hybrid transport: the fast API token for reads + token-safe writes (status/
    # log polling, node/storage, NICs, start/stop/destroy, helper), and pvesh over
    # root SSH only for what a token may not do (the restore itself = qmrestore
    # with hostpci, and deleting hostpci). SSH also drives shell actions (bridge).
    ssh = ParamikoSsh(
        config["ssh_host"],
        config.get("ssh_port", 22),
        config.get("ssh_user", "root"),
        config.get("ssh_private_key", ""),
        config.get("ssh_host_key", ""),
    )
    pve = PveApi(
        config.get("api_url", ""),
        config.get("api_user", "root@pam"),
        config.get("token_id", ""),
        config.get("token_secret", ""),
    )
    return RestoreServer(config, pve, ssh, pve_root=PveSsh(ssh))


def ensure_host_key_pinned(config: dict[str, Any]) -> dict[str, Any]:
    """Trust-on-first-use: if no SSH host key is pinned yet, scan the scratch
    host once and persist it, so every later connection verifies against it
    (RejectPolicy). Returns the config with ``ssh_host_key`` populated. On a scan
    failure the config is returned unchanged (the SSH check then reports it)."""
    if config.get("ssh_host_key", "").strip():
        return config
    try:
        from app.services.restore_server import scan_host_key

        line = scan_host_key(config["ssh_host"], int(config.get("ssh_port", 22)))
    except Exception as exc:
        logger.warning("SSH host-key scan failed for %s: %s", config.get("ssh_host"), exc)
        return config
    updated = dict(config)
    updated["ssh_host_key"] = line
    try:
        restore_config.set_host_key(line)
        logger.info("Pinned SSH host key for %s", config.get("ssh_host"))
    except Exception as exc:
        logger.warning("could not persist pinned host key: %s", exc)
    return updated


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


def vm_backup_locations(vm_id: int) -> set[str]:
    """The canonical PBS backup locations the VM actually has — the exact same
    keys the protection/offsite column uses (``backup_events.locations``, one
    ``pbs:<server>/<datastore>/<namespace>`` per physical copy, incl. offsite).
    Not the cluster's configured targets: a VM is restorable from wherever its
    backups really live."""
    with connect() as conn, conn.cursor() as cur:
        cur.execute(
            """
            SELECT DISTINCT loc
            FROM backup_events c, unnest(c.locations) AS loc
            WHERE c.vm_id = %s AND c.status = 'ok' AND c.removed_at IS NULL
              AND COALESCE(c.size_bytes, 0) > 1 AND loc LIKE 'pbs:%%'
            """,
            (vm_id,),
        )
        return {r["loc"] for r in cur.fetchall()}


def restore_host_pbs_entries(server: RestoreServer) -> list[dict[str, Any]]:
    """The restore host's PBS datastores, each resolved to its canonical backup
    location — the same identity (certificate fingerprint → registered
    connection) the protection column uses — so a VM's backup location matches
    1:1 with no heuristics."""
    from app.services import backup_location as bl

    conns = list(db.list_pbs_connections())
    entries: list[dict[str, Any]] = []
    for s in server.list_pbs_storages():
        host = (s.get("server") or "").strip().lower()
        ds = s.get("datastore", "")
        ns = s.get("namespace", "")
        conn = bl.match_pbs_connection(host, conns)
        if conn is not None:
            location = bl.connection_location(conn, ds, ns)
        else:
            location = f"pbs:{host}/{ds}" + (f"/{ns}" if ns else "")
        entries.append(
            {
                "location": location,
                "storage": s["storage"],
                "pbs": host,
                "datastore": ds,
                "namespace": ns,
                "resolved": conn is not None,  # matched a registered PBS connection
            }
        )
    return entries


def all_vm_backup_locations() -> list[dict[str, Any]]:
    """Every distinct canonical PBS backup location recorded across all VMs, with
    a sample VM for each — shown in the diagnostic beside the restore host's
    datastores so the exact string difference (datastore, namespace, server name)
    is visible when nothing matches."""
    with connect() as conn, conn.cursor() as cur:
        cur.execute(
            """
            SELECT loc,
                   COUNT(DISTINCT be.vm_id) AS vms,
                   MIN(cl.name || ':' || v.name) AS sample
            FROM backup_events be
            JOIN vms v ON v.id = be.vm_id
            JOIN clusters cl ON cl.id = v.cluster_id
            CROSS JOIN unnest(be.locations) AS loc
            WHERE be.status = 'ok' AND be.removed_at IS NULL
              AND COALESCE(be.size_bytes, 0) > 1 AND loc LIKE 'pbs:%%'
            GROUP BY loc
            ORDER BY loc
            """
        )
        return [{"location": r["loc"], "vms": r["vms"], "sample": r["sample"]} for r in cur.fetchall()]


def matched_vms_by_location(locations: list[str]) -> dict[str, list[str]]:
    """For each restore-host location, the VMs whose real backups live there,
    as ``cluster:vm`` — so the diagnostic shows exactly what can be restored
    from where."""
    if not locations:
        return {}
    out: dict[str, list[str]] = {}
    with connect() as conn, conn.cursor() as cur:
        cur.execute(
            """
            SELECT DISTINCT loc, cl.name AS cluster, v.name AS vm
            FROM backup_events be
            JOIN vms v ON v.id = be.vm_id
            JOIN clusters cl ON cl.id = v.cluster_id
            CROSS JOIN unnest(be.locations) AS loc
            WHERE be.status = 'ok' AND be.removed_at IS NULL AND COALESCE(be.size_bytes, 0) > 1
              AND loc = ANY(%s)
            ORDER BY loc, cl.name, v.name
            """,
            (locations,),
        )
        for r in cur.fetchall():
            out.setdefault(r["loc"], []).append(f"{r['cluster']}:{r['vm']}")
    return out


def _sandbox_alloc(
    override: str | None,
    mgmt_ip: str | None,
    config: dict[str, Any],
) -> tuple[str | None, str | None, int]:
    """Decide how the helper is provisioned: ``(guest_ip, helper_sandbox_ip, prefix)``.

    - With an operator **override** (``probe_primary_ip`` as ``ip[/prefix]``): the
      helper is pinned to that subnet — dnsmasq hands that IP and the helper takes
      the first free host in its net. A bare IP means /24. The prober then uses
      exactly this IP (``stur``), ignoring the guest agent.
    - Without an override: the helper starts on the **management LAN** and runs
      DHCP from the management range (dnsmasq hands ``.200``, the helper takes
      ``.254``). The prober then adapts at runtime to whatever IP(s) the guest
      agent reports, reaching each over a host it adds in that IP's own subnet.

    ``(None, None, mgmt_prefix)`` only when there is no management IP."""
    from app.services.restore_server import _prefix_from_netmask, parse_cidr, sandbox_addrs, sandbox_host_ip

    mgmt_prefix = _prefix_from_netmask(config.get("mgmt_netmask", "255.255.255.0"))
    parsed = parse_cidr(override) if override else None
    if parsed:
        guest_ip, prefix = parsed
        return guest_ip, sandbox_host_ip(guest_ip, prefix), prefix
    if mgmt_ip:
        helper_ip, guest_ip, _ = sandbox_addrs(mgmt_ip, mgmt_prefix)
        return guest_ip, helper_ip, mgmt_prefix
    return None, None, mgmt_prefix


class NotRunnable(RuntimeError):
    """A queued run cannot run and should be dequeued as an error (vs a resource
    wait, which returns None from build_plan)."""


def build_plan(candidate: dict[str, Any], server: RestoreServer, config: dict[str, Any]) -> RunPlan | None:
    """Resolve source/snapshot/NICs and allocate resources. Returns None only for
    a resource wait (no free management IP); raises NotRunnable when the VM is
    not auto-restorable or has no snapshot (those become an error, not a wait)."""
    vm_id = candidate["vm_id"]
    match = restore_match.match_source(vm_backup_locations(vm_id), restore_host_pbs_entries(server))
    if match is None:
        raise NotRunnable("not auto-restorable (no matching local datastore)")
    snaps = server.list_snapshots(match["storage"], candidate["vmid"])
    snap = restore_match.pick_latest_complete(snaps)
    if snap is None:
        raise NotRunnable("no complete snapshot available")

    ports = list(candidate.get("probe_tcp_ports") or [])

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

    # How the helper is provisioned: pinned to an operator override's subnet, else
    # the management-LAN + DHCP default (the prober adapts to the live guest-agent
    # IPs at runtime). Never derived from the restored, running VM.
    guest_ip, sandbox_ip, sandbox_prefix = _sandbox_alloc(candidate.get("probe_primary_ip"), alloc.mgmt_ip, config)
    primary = guest_ip
    targets = [{"ip": guest_ip, "ports": ports, "prefix": sandbox_prefix}] if guest_ip else []
    from app.services.restore_server import sandbox_name

    label = sandbox_name(candidate.get("cluster_name") or "", candidate.get("vm_name") or "")

    targets_sorted = sorted(
        (s for s in config.get("target_storages", []) if s.get("use", True)), key=lambda s: s.get("order", 0)
    )
    if not targets_sorted:
        # No ticked target storage: PVE would reject the restore with a cryptic
        # "storage ID '' ..." 400. Fail with an actionable message instead.
        raise NotRunnable("no target storage selected — tick one in Settings → Restore")
    target_storage = targets_sorted[0]["storage"]

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
        reservations=[],
        probe_targets=targets,
        primary_ip=primary,
        sandbox_ip=sandbox_ip,
        sandbox_prefix=sandbox_prefix,
        guest_vlan=candidate.get("probe_vlan"),
        probe_override=candidate.get("probe_primary_ip") or None,
        label=label,
        require_icmp=candidate.get("probe_require_icmp", True),
        tcp_ports=ports,
        hold_on_failure=candidate.get("hold_on_failure", False),
        retry_days=candidate.get("restore_retry_days", 7),
        first_delay=candidate.get("probe_first_delay_seconds") or config.get("probe_first_delay_seconds", 30),
        interval=candidate.get("probe_interval_seconds") or config.get("probe_interval_seconds", 5),
        window=candidate.get("probe_window_seconds") or config.get("probe_window_seconds", 300),
        needed_bytes=snap.get("size"),
        needed_ram=_vm_memory_bytes(vm_id),
    )


def _vm_memory_bytes(vm_id: int) -> int | None:
    """The guest's configured RAM (maxmem), captured during PVE sync."""
    with connect() as conn, conn.cursor() as cur:
        cur.execute("SELECT memory_bytes FROM vms WHERE id = %s", (vm_id,))
        row = cur.fetchone()
    return row["memory_bytes"] if row else None


def enqueue_due(server: RestoreServer, config: dict[str, Any]) -> int:
    """Create queued runs for due, auto-restorable VMs. Returns count enqueued."""
    storages = restore_host_pbs_entries(server)
    count = 0
    for cand in db_restore.due_auto_restore_candidates():
        if restore_match.is_auto_restorable(vm_backup_locations(cand["vm_id"]), storages):
            db_restore.create_run(cand["vm_id"], trigger="scheduled")
            count += 1
    return count


def _fits_disk(server: RestoreServer, plan: RunPlan) -> bool:
    """Whether this run's estimated footprint fits the target storage's free
    space right now (held environments already consume it, so live free avail
    reflects them). Unknown sizes optimistically fit (spec §15)."""
    try:
        stores = server.free_resources().get("stores", [])
        free = next((s.get("avail") for s in stores if s.get("storage") == plan.target_storage), None)
    except Exception as exc:
        logger.debug("free_resources check failed: %s", exc)
        return True
    return restore_worker.fits_disk(free, plan.needed_bytes)


def _fits_ram(server: RestoreServer, plan: RunPlan) -> bool:
    """Whether the guest's RAM fits the host's free RAM, leaving the host its
    reserve (10%, min 10 GiB). Unknown free RAM optimistically fits."""
    try:
        res = server.free_resources()
    except Exception as exc:
        logger.debug("free_resources (ram) check failed: %s", exc)
        return True
    return restore_worker.fits_ram(res.get("ram_free"), res.get("ram_total"), plan.needed_ram)


def drain(config: dict[str, Any], server: RestoreServer) -> None:
    """Drain the queue, one active run at a time, until nothing runnable is left.
    A run too big for the target storage *right now* is skipped so smaller due
    runs proceed (it stays queued and is retried later); management-IP
    exhaustion makes the whole queue wait (spec §15). The caller guarantees a
    single drainer."""
    if restore_config.queue_paused():
        return
    prod = production_endpoints()
    while True:
        if db_restore.has_active_run():
            return
        queued = db_restore.list_queued_runs()
        if not queued:
            return
        executed = False
        for run in queued:
            cand = _candidate_for_vm(run["vm_id"])
            if cand is None:
                db_restore.record_run_result(run["id"], run["vm_id"], "error", duration_seconds=0, status="error")
                continue  # dequeued (no longer queued), try the next
            try:
                plan = build_plan(cand, server, config)
            except NotRunnable as exc:
                logger.info("restore run %s dequeued: %s", run["id"], exc)
                db_restore.record_run_result(
                    run["id"], run["vm_id"], "error", duration_seconds=0, checks={"error": str(exc)}, status="error"
                )
                continue  # dequeued, try the next
            if plan is None:
                # No free management IP: nothing can start — wait (spec §15).
                logger.info("restore run %s waiting for a free management IP", run["id"])
                return
            if not _fits_disk(server, plan):
                # Too big for the target storage right now: skip, leave queued,
                # try the next (smaller) due run. Retried on a later pass.
                logger.info("restore run %s skipped: does not fit target storage yet", run["id"])
                continue
            if not _fits_ram(server, plan):
                # Guest RAM would eat into the host's reserve: skip, try the next
                # (smaller) run. Left queued; retried on a later pass (no spin —
                # the pass returns below once nothing executes).
                logger.info("restore run %s skipped: does not fit free RAM yet", run["id"])
                continue
            result = restore_worker.execute(run["id"], plan, server, production_hosts=prod)
            _notify_result(run["vm_id"], result)
            executed = True
            break  # free space changed; re-evaluate the queue from the top
        if not executed:
            return  # nothing runnable fits right now


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
            SELECT v.id AS vm_id, v.vmid, v.node_name, v.guest_type, v.name AS vm_name,
                   c.name AS cluster_name,
                   p.hold_on_failure, p.probe_primary_ip, p.probe_vlan, p.probe_tcp_ports, p.probe_require_icmp,
                   p.restore_retry_days, p.probe_window_seconds, p.probe_first_delay_seconds,
                   p.probe_interval_seconds
            FROM vms v LEFT JOIN clusters c ON c.id = v.cluster_id
                 LEFT JOIN vm_policies p ON p.vm_id = v.id WHERE v.id = %s
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


def test_connection() -> dict[str, Any]:
    """Read-only credential check for the Settings 'test' button: API token
    (list nodes), SSH key (echo), and the host-identity interlock. Returns a
    structured result; never raises."""
    config = restore_config.load()
    if config is None:
        return {"configured": False}
    config = ensure_host_key_pinned(config)  # trust-on-first-use, then verify
    checks: dict[str, Any] = {}
    try:
        server = build_server(config)
    except Exception as exc:
        return {"configured": True, "ok": False, "error": str(exc)[:200]}

    try:
        server.verify_host_identity(production_endpoints())
        checks["host"] = {"ok": True, "msg": "kein Produktions-Host / not a production host"}
    except Exception as exc:
        checks["host"] = {"ok": False, "msg": str(exc)[:200]}
    try:
        nodes = server.pve.get("/nodes") or []
        checks["api"] = {"ok": True, "msg": f"pvesh über root-SSH · {len(nodes)} Node(s)"}
    except Exception as exc:
        checks["api"] = {"ok": False, "msg": f"pvesh · {str(exc)[:160]}"}
    try:
        out = server.ssh.run("echo bsentinel-ok").strip()
        ok = "bsentinel-ok" in out
        checks["ssh"] = {"ok": ok, "msg": "SSH ok" if ok else f"unexpected: {out[:80]}"}
    except Exception as exc:
        checks["ssh"] = {"ok": False, "msg": str(exc)[:200]}

    checks["configured"] = True
    checks["ok"] = all(c.get("ok") for c in checks.values() if isinstance(c, dict))
    return checks


def ui_target_storages() -> dict[str, Any]:
    """Storages for the Settings UI: the scratch host's own storages enumerated
    live (name + PVE type), each merged with the operator's saved disk-class and
    selection. The operator only assigns the disk type / picks targets — never
    types storage names. Returns ``{"rows": [...], "error": str|None}``; on a
    host error the saved assignments are still returned (nothing lost) and the
    error is surfaced so the UI can say why nothing fresh was found."""
    config = restore_config.load()
    if config is None:
        return {"rows": [], "error": None, "note": None}
    saved = {s["storage"]: s for s in config.get("target_storages", [])}
    discovered: list[dict[str, Any]] = []
    error: str | None = None
    note: str | None = None
    try:
        result = build_server(config).list_target_storages()
        discovered = result["targets"]
        if not discovered and result["pbs"] > 0 and result["total"] == result["pbs"]:
            # Every storage returned is a PBS source datastore — no regular
            # (target) storage is visible. Almost always a missing Datastore.Audit
            # permission for the target storages on the API token.
            note = "only_pbs"
    except Exception as exc:
        logger.warning("target storage enumeration failed: %s", exc)
        error = str(exc)[:200]

    rows: list[dict[str, Any]] = []
    seen: set[str] = set()
    # Only image-capable storages are restore targets; others (e.g. "local") are
    # never shown or configurable here.
    for d in discovered:
        if not d.get("images"):
            continue
        name = d["storage"]
        seen.add(name)
        s = saved.get(name)
        rows.append(
            {
                "storage": name,
                "type": d.get("type", ""),
                "disk_class": (s or {}).get("disk_class", "standard-ssd"),
                "use": bool(s.get("use")) if s else False,
                "order": (s or {}).get("order", 999),
                "used": d.get("used"),
                "free": d.get("avail"),
            }
        )
    # Saved storages the host did not return (temporarily missing): keep them so
    # the assignment is not silently dropped.
    for name, s in saved.items():
        if name not in seen:
            rows.append(
                {
                    "storage": name,
                    "type": s.get("type", ""),
                    "disk_class": s.get("disk_class", "standard-ssd"),
                    "use": bool(s.get("use", True)),
                    "order": s.get("order", 999),
                    "missing": True,
                }
            )
    rows.sort(key=lambda r: (not r["use"], r["order"], r["storage"]))
    return {"rows": rows, "error": error, "note": note}


def plan_from_run(run: dict[str, Any], config: dict[str, Any]) -> RunPlan:
    """Reconstruct a RunPlan from a stored (held) run for reap/retest/promote."""
    cand = _candidate_for_vm(run["vm_id"]) or {}
    ports = list(cand.get("probe_tcp_ports") or [])
    primary, sandbox_ip, sandbox_prefix = _sandbox_alloc(cand.get("probe_primary_ip"), run.get("mgmt_ip"), config)
    targets = [{"ip": primary, "ports": ports, "prefix": sandbox_prefix}] if primary else []
    tgt = (run.get("target_storages") or [""])[0]
    from app.services.restore_server import sandbox_name

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
        reservations=[],
        probe_targets=targets,
        primary_ip=primary,
        sandbox_ip=sandbox_ip,
        sandbox_prefix=sandbox_prefix,
        guest_vlan=cand.get("probe_vlan"),
        probe_override=cand.get("probe_primary_ip") or None,
        label=sandbox_name(cand.get("cluster_name") or "", cand.get("vm_name") or ""),
        require_icmp=cand.get("probe_require_icmp", True),
        tcp_ports=ports,
        hold_on_failure=cand.get("hold_on_failure", False),
        retry_days=cand.get("restore_retry_days", 7),
        first_delay=cand.get("probe_first_delay_seconds") or config.get("probe_first_delay_seconds", 30),
        interval=cand.get("probe_interval_seconds") or config.get("probe_interval_seconds", 5),
        window=cand.get("probe_window_seconds") or config.get("probe_window_seconds", 300),
    )


def reprovision_and_retest(run_id: int, run: dict[str, Any], config: dict[str, Any], server: RestoreServer):
    """Re-test a held environment. Its helper was discarded when it was held (to
    free the management IP), so allocate + provision a FRESH helper, re-probe,
    then tear the helper back down and free its IP again — unless it passed, in
    which case the caller promotes (which tears down VM + helper). Returns
    ``(result, plan)``; the plan carries the fresh helper for the teardown."""
    import dataclasses

    base = plan_from_run(run, config)
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
        raise NotRunnable("no free management IP to re-provision the re-test helper")
    # Recompute the sandbox addressing against the FRESH mgmt IP (only the
    # management-range fallback depends on it; a recorded/override guest IP does
    # not), so the re-test helper and its dnsmasq are set up the same way.
    cand = _candidate_for_vm(run["vm_id"]) or {}
    primary, sandbox_ip, sandbox_prefix = _sandbox_alloc(cand.get("probe_primary_ip"), alloc.mgmt_ip, config)
    targets = [{"ip": primary, "ports": list(base.tcp_ports), "prefix": sandbox_prefix}] if primary else []
    plan = dataclasses.replace(
        base,
        helper_vmid=alloc.helper_vmid,
        mgmt_ip=alloc.mgmt_ip,
        probe_targets=targets,
        primary_ip=primary,
        sandbox_ip=sandbox_ip,
        sandbox_prefix=sandbox_prefix,
    )
    db_restore.update_run(run_id, helper_vmid=alloc.helper_vmid, mgmt_ip=alloc.mgmt_ip)
    server.provision_helper(
        plan.helper_vmid,
        plan.vlan_tag,
        plan.mgmt_ip,
        plan.sandbox_ip,
        plan.primary_ip,
        plan.sandbox_prefix,
        plan.guest_vlan,
        plan.label,
    )
    result = restore_worker.retest_held(run_id, server, plan)
    if result != "passed":
        # Still not good: keep the VM held, discard the fresh helper, free its IP.
        try:
            server.destroy(plan.helper_vmid, is_lxc=True)
        except Exception as exc:
            logger.warning("retest: helper teardown failed: %s", exc)
        db_restore.update_run(run_id, helper_vmid=None, mgmt_ip=None)
    return result, plan


# A real restore has its helper and/or restore VM on the host within seconds of
# going 'running' (the helper CT is created first, the restore VM moments later).
# So a run that has been 'running' this long with NOTHING on the host is wedged.
STUCK_RUN_GRACE_SECONDS = 600


def recover_stuck_runs(server: RestoreServer, config: dict[str, Any]) -> int:
    """Self-heal a wedged queue. One run in status 'running' blocks the whole
    single-slot queue (``has_active_run``). A crash/SIGKILL mid-run, or a resume
    that raised before it could finalize, can leave a run 'running' with nothing
    on the host — and then the queue never advances. Finalize any such run: past
    the grace period, with no running PVE task and neither its restore VM nor its
    helper present on the host. Host unreachable → do nothing (don't mistake an
    outage for an empty host). Returns the count recovered."""
    import datetime as _dt

    running = db_restore.list_runs(limit=500, status="running")
    if not running:
        return 0
    try:
        present = server.present_vmids()  # ground truth; raises if the host is down
    except Exception as exc:
        logger.debug("stuck-run recovery skipped (host unreachable): %s", exc)
        return 0

    now = _dt.datetime.now(_dt.UTC)
    recovered = 0
    for run in running:
        started = run.get("started_at")
        if started is not None:
            if started.tzinfo is None:
                started = started.replace(tzinfo=_dt.UTC)
            if (now - started).total_seconds() < STUCK_RUN_GRACE_SECONDS:
                continue  # too fresh — may still be provisioning
        rv, hv, upid = run.get("restore_vmid"), run.get("helper_vmid"), run.get("task_upid")
        alive = (rv and int(rv) in present) or (hv and int(hv) in present)
        if not alive and upid:
            alive = server.task_running(upid)
        if alive:
            continue
        logger.warning("recovering stuck restore run %s: 'running' but nothing on the host", run["id"])
        try:
            restore_worker._teardown(server, plan_from_run(run, config))  # best-effort, usually a no-op
        except Exception as exc:
            logger.debug("stuck-run teardown (ignored): %s", exc)
        db_restore.record_run_result(
            run["id"],
            run["vm_id"],
            "error",
            duration_seconds=0,
            checks={"error": "recovered: run was stuck 'running' with no environment on the host"},
            status="error",
        )
        recovered += 1
    return recovered


def run_restore_queue() -> None:
    """Entry point for the auto-sync loop: recover wedged runs, enqueue due, drain."""
    try:
        config = restore_config.load()
        if config is None:
            return
        config = ensure_host_key_pinned(config)
        server = build_server(config)
        recover_stuck_runs(server, config)
        enqueue_due(server, config)
        drain(config, server)
    except Exception as exc:
        logger.error("restore queue processing failed: %s", exc)
