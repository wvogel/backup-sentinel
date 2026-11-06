"""The single-slot restore worker: executes one run's pipeline (spec §11),
classifies the result, persists it, and handles hold / retry / promote / reap.

The heavy I/O lives in `RestoreServer` (injected), so this module is unit-tested
with a fake server. Only one *active* run executes at a time (the caller holds a
Postgres advisory lock); held environments coexist (spec §15).
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from app import db_restore
from app.services import restore_probe
from app.services.restore_server import RestoreCancelled, overall_restore_fields

logger = logging.getLogger(__name__)


@dataclass
class RunPlan:
    vm_id: int
    restore_vmid: int
    helper_vmid: int
    vlan_tag: int
    mgmt_ip: str
    source: dict[str, Any]  # {storage, src_vmid, pbs, datastore, namespace}
    snapshot_id: str
    target_storage: str
    reservations: list[dict[str, Any]]  # {mac, ip, prefix}
    probe_targets: list[dict[str, Any]]  # {ip, ports, prefix}
    primary_ip: str | None  # the guest IP we probe (recorded / override / fallback)
    sandbox_ip: str | None = None  # the helper's own sandbox-leg IP (a host in the guest subnet, or fallback)
    sandbox_prefix: int = 24  # the guest subnet's prefix (not always /24)
    guest_vlan: int | None = None  # the VID the guest tags internally (trunk the NIC, access the helper)
    probe_override: str | None = None  # operator-set probe IP (CIDR); when set, the prober uses exactly it (stur)
    label: str = ""  # cluster+VM name for the restore VM and helper, so vmids 9000/9001 are identifiable
    require_icmp: bool = True
    tcp_ports: list[int] = field(default_factory=list)
    hold_on_failure: bool = False
    retry_days: int = 7
    first_delay: int = 30
    interval: int = 30
    window: int = 300
    needed_bytes: int | None = None  # estimated restore footprint, for the disk-fit skip
    needed_ram: int | None = None  # the guest's configured RAM, for the RAM-fit skip


def owned_vmids() -> set[int]:
    out: set[int] = set()
    for r in db_restore.active_and_held_runs():
        for v in (r.get("restore_vmid"), r.get("helper_vmid")):
            if v is not None:
                out.add(v)
    return out


def _probe_loop(
    server: Any,
    plan: RunPlan,
    *,
    now: Callable[[], float],
    sleep: Callable[[float], None],
    probe_fn: Callable[[Any, str, list[dict[str, Any]]], dict[str, Any]],
    run_id: int | None = None,
    should_cancel: Callable[[], bool] | None = None,
) -> tuple[str, dict[str, Any]]:
    """Poll until success or the window elapses. Returns (result, checks).

    Targets are resolved fresh every pass:
    - **Operator override** (CIDR [+ VLAN]): the prober uses exactly that one IP,
      rigidly — it never consults the guest agent.
    - **Otherwise**: before every test it checks what IPv4(s) the guest agent
      reports. None yet → it tries the IP dnsmasq hands out from the management
      range (a DHCP guest answers there). As soon as the agent reports IP(s), it
      probes every IPv4 on the guest's **real NICs** (MAC in the VM config — not
      Docker bridges, wg0, …) — the helper reaches each over the first free host
      it adds in that IP's own subnet — and a check counts green if it passed on
      any of them, until the first pass where every check is green. If our DHCP
      handed out its IP (the agent reports it), that IP is probed **first**.

    Cadence is fixed: a check starts every ``interval`` seconds measured from the
    START of the previous one, and nothing in a pass is allowed to block it — the
    agent lookup uses a short timeout and the helper probes every IP/port in
    parallel (each capped ~2s). Once the guest agent has reported its live IP(s)
    we remember them and keep probing them, so a later agent query that times out
    doesn't drop us back to the DHCP fallback. A failed pass counts as not-green
    and the next follows; only a failure on the last pass before the window closes
    is raised."""
    sleep(plan.first_delay)
    deadline = now() + plan.window
    checks: dict[str, Any] = {}
    result = "limited"
    discovered: list[tuple[str, int]] = []  # live agent IP(s), remembered once seen
    while True:
        pass_start = now()
        if should_cancel and should_cancel():
            raise RestoreCancelled("restore cancelled by operator")
        if plan.probe_override:
            targets = [{"ip": plan.primary_ip, "ports": plan.tcp_ports, "prefix": plan.sandbox_prefix}]
            ips = [plan.primary_ip] if plan.primary_ip else []
        else:
            nics = server.guest_agent_nics(plan.restore_vmid) or []
            if nics:
                discovered = nics  # keep probing these even if a later query times out
            if discovered:
                # Our DHCP's IP first (stable sort keeps the agent order otherwise).
                ordered = sorted(discovered, key=lambda n: n[0] != plan.primary_ip)
                targets = [{"ip": ip, "ports": plan.tcp_ports, "prefix": pfx} for ip, pfx in ordered]
                ips = [ip for ip, _ in ordered]
            else:
                # No agent IP yet → try the DHCP-assigned management-range IP.
                targets = [{"ip": plan.primary_ip, "ports": plan.tcp_ports, "prefix": plan.sandbox_prefix}]
                ips = [plan.primary_ip] if plan.primary_ip else []
        if ips:
            if run_id is not None:
                db_restore.update_run(run_id, detected_ip=ips[0])
            try:
                checks = probe_fn(server, plan.mgmt_ip, targets)
            except Exception as exc:
                if now() >= deadline:
                    raise
                logger.info("restore probe pass failed, retrying: %s", exc)
                result, checks = "limited", {"note": f"probe pass failed: {exc}"}
            else:
                result = restore_probe.classify_multi(
                    checks, ips, require_icmp=plan.require_icmp, tcp_ports=plan.tcp_ports, had_ip=True
                )
        else:
            result, checks = "limited", {"note": "no target IP yet (no DHCP address, override, or guest agent)"}
        # Persist this pass's checks so the live card can list the ones already
        # green (and the detected IP above) while the window keeps probing.
        if run_id is not None:
            db_restore.update_run(run_id, checks=checks)
        if result == "passed" or now() >= deadline:
            return result, checks
        # Fixed cadence: next check ~interval after this one STARTED, so a slow
        # pass never stretches the rhythm (two ports coming up are shown within one
        # interval, not several seconds later).
        sleep(max(0.0, plan.interval - (now() - pass_start)))


def execute(
    run_id: int,
    plan: RunPlan,
    server: Any,
    *,
    production_hosts: set[str],
    now: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
    probe_fn: Callable[[Any, str, list[dict[str, Any]]], dict[str, Any]] | None = None,
    should_cancel: Callable[[], bool] | None = None,
) -> str:
    """Run the full pipeline for ``run_id`` and persist the outcome. Returns the
    final status (passed/limited/failed/error/held/cancelled)."""
    probe_fn = probe_fn or (lambda s, ip, t: s.probe(ip, t, plan.require_icmp))
    should_cancel = should_cancel or (lambda: db_restore.cancel_requested(run_id))

    def _checkpoint() -> None:
        if should_cancel():
            raise RestoreCancelled("restore cancelled by operator")

    start = now()
    db_restore.update_run(
        run_id,
        status="running",
        started_at=_utcnow(),
        restore_vmid=plan.restore_vmid,
        helper_vmid=plan.helper_vmid,
        vlan_tag=plan.vlan_tag,
        mgmt_ip=plan.mgmt_ip,
        source_pbs=plan.source.get("pbs"),
        source_datastore=plan.source.get("datastore"),
        source_namespace=plan.source.get("namespace"),
        snapshot_id=plan.snapshot_id,
        target_storages=[plan.target_storage],
    )
    result = "error"
    checks: dict[str, Any] = {}
    log_text: str | None = None
    try:
        server.verify_host_identity(production_hosts)
        server.reap(owned_vmids())
        server.ensure_sandbox_bridge()
        # Pre-flight: read just the backup's config (cheap, ~1s) before paying for
        # a helper and a long restore. A wrong PBS key / missing permission /
        # unreachable PBS aborts here with the real reason and NO helper churn,
        # instead of the old pattern (helper flashes up, restore dies at config
        # extraction, every retry repeats it). Inconclusive errors fall through to
        # the normal restore, so a working backup is never falsely blocked.
        _checkpoint()
        db_restore.update_run(run_id, phase="preflight")
        server.preflight_check(plan.source, plan.snapshot_id)
        # Bring the helper up FIRST and fail fast (≤60s) if it can't be reached.
        # A restore is expensive (minutes, many GB), so don't pay for it when the
        # cheap helper — which must serve the guest's DHCP and answer probes —
        # never comes up. (We can revert to overlapping the two with the VM boot
        # once reachability is rock-solid.)
        _checkpoint()
        db_restore.update_run(run_id, phase="helper")
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
        _checkpoint()
        db_restore.update_run(run_id, phase="restoring")
        server.restore(
            plan.restore_vmid,
            plan.source,
            plan.snapshot_id,
            plan.target_storage,
            on_progress=_progress_updater(server, run_id, plan.restore_vmid),
            on_task=lambda upid: db_restore.update_run(run_id, task_upid=upid),
            should_cancel=should_cancel,
        )
        _checkpoint()
        db_restore.update_run(run_id, phase="network")
        server.rewrite_nics(plan.restore_vmid, plan.vlan_tag, plan.guest_vlan, name=plan.label)
        db_restore.update_run(run_id, phase="booting")
        # Never start while the restore still holds the config lock — a restore
        # that is still running (e.g. after an app restart resumed the tail, or a
        # stale task) would make the start fail with "can't lock file". Wait for
        # the lock to clear, then start.
        server.wait_unlocked(plan.restore_vmid)
        server.start(plan.restore_vmid)
        db_restore.update_run(run_id, phase="probing")
        if plan.primary_ip:
            db_restore.update_run(run_id, detected_ip=plan.primary_ip)  # recorded/override IP
        result, checks = _probe_loop(
            server, plan, now=now, sleep=sleep, probe_fn=probe_fn, run_id=run_id, should_cancel=should_cancel
        )
        if result != "passed":
            log_text = _failure_diag(server, plan)
    except RestoreCancelled as exc:
        logger.info("restore run %s cancelled: %s", run_id, exc)
        result = "cancelled"
        checks = {"note": "cancelled by operator"}
    except Exception as exc:
        logger.warning("restore run %s failed: %s", run_id, exc)
        result = "error"
        checks = {"error": str(exc)}
        # Lead the log with the REAL reason (e.g. "wrong PBS key", a restore that
        # timed out, the helper agent unreachable). Network diagnostics are only
        # appended when a VM was actually restored and started — a restore that
        # failed at config extraction (wrong key, no permission, PBS unreachable)
        # leaves no guest, so probing a sandbox for an "unreachable VM" that never
        # booted is pure noise that hides the real cause.
        log_text = _error_log(server, plan, str(exc))

    duration = int(now() - start)
    return _finalize(run_id, plan, result, checks, duration, server, log=log_text)


def _finalize(
    run_id: int, plan: RunPlan, result: str, checks: dict, duration: int, server: Any, log: str | None = None
) -> str:
    if result != "passed" and plan.hold_on_failure and result in ("failed", "error"):
        # Keep the restored VM for inspection, but DISCARD the helper: it is cheap
        # and its only scarce resource — the management IP — would otherwise be
        # tied up for the whole hold window (spec §15). A re-test re-provisions a
        # fresh helper. The VM's vmid + sandbox VLAN stay reserved; helper_vmid +
        # mgmt_ip are cleared so the allocator frees them.
        if plan.helper_vmid:
            try:
                if server.exists(plan.helper_vmid, is_lxc=True):
                    server.destroy(plan.helper_vmid, is_lxc=True)
            except Exception as exc:
                logger.warning("held: discarding helper %s failed: %s", plan.helper_vmid, exc)
        db_restore.record_run_result(
            run_id, plan.vm_id, result, duration_seconds=duration, checks=checks, log=log, status="held"
        )
        db_restore.update_run(run_id, helper_vmid=None, mgmt_ip=None)
        return "held"

    # Teardown (passed/limited, or failed without hold): reap this env.
    _teardown(server, plan)
    db_restore.record_run_result(run_id, plan.vm_id, result, duration_seconds=duration, checks=checks, log=log)
    if result in ("failed", "error"):
        db_restore.schedule_retry(plan.vm_id, plan.retry_days)
    else:
        db_restore.set_vm_next_run(plan.vm_id, None)  # cadence governs again
    return result


def _teardown(server: Any, plan: RunPlan) -> None:
    # Only act on guests that actually exist — a run that errored before the
    # restore leaves no VM, so shutting it down would just spam task errors.
    steps = []
    try:
        # Hard-stop + destroy, no graceful shutdown: a restored prod VM has no
        # working guest agent in the sandbox, so an ACPI shutdown just times out
        # (60s) holding the config lock, which then blocks the destroy. destroy()
        # force-stops and waits before purging.
        if server.exists(plan.restore_vmid, is_lxc=False):
            steps.append(lambda: server.destroy(plan.restore_vmid))
        if server.exists(plan.helper_vmid, is_lxc=True):
            steps.append(lambda: server.destroy(plan.helper_vmid, is_lxc=True))
    except Exception as exc:
        logger.warning("teardown existence check failed: %s", exc)
    for fn in steps:
        try:
            fn()
        except Exception as exc:
            logger.warning("teardown step failed: %s", exc)
    try:
        server.reap(owned_vmids())
    except Exception as exc:
        logger.warning("reap after teardown failed: %s", exc)


def _error_log(server: Any, plan: RunPlan, error: str) -> str:
    """Run log for a run that errored out with an exception. Leads with the real
    reason, and only appends sandbox/helper network diagnostics when the restored
    VM actually exists — a restore that never produced a guest (wrong PBS key,
    missing permission, PBS unreachable, …) would otherwise be buried under a
    misleading "guest unreachable / no NIC configured" diagnosis of a VM that was
    never created."""
    reason = (error or "").strip() or "unknown error"
    parts = [f"Restore failed\n  {reason}"]
    try:
        vm_exists = bool(plan.restore_vmid) and server.exists(plan.restore_vmid, is_lxc=False)
    except Exception as exc:
        logger.debug("restore_vmid existence check failed: %s", exc)
        vm_exists = False
    if vm_exists:
        diag = _failure_diag(server, plan)
        if diag:
            parts.append(diag)
    return "\n\n".join(parts)


def _failure_diag(server: Any, plan: RunPlan) -> str | None:
    """Best-effort network diagnosis for the run log when a test does not pass —
    the sandbox/guest path AND the helper's management leg, over host SSH while the
    helper still exists. So an 'agent unreachable' failure records WHY (agent down
    inside the helper / L2-ARP / IP-not-applied / port blocked), not a bare
    timeout. Never raises; returns None when nothing could be gathered."""
    parts: list[str] = []
    try:
        diag_ips = [ip for ip, _ in (server.guest_agent_nics(plan.restore_vmid) or [])]
        if not diag_ips and plan.primary_ip:
            diag_ips = [plan.primary_ip]
        sbx = server.probe_netdiag(plan.helper_vmid, diag_ips, restore_vmid=plan.restore_vmid, ports=plan.tcp_ports)
        if sbx:
            parts.append(sbx)
    except Exception as exc:
        logger.debug("probe netdiag failed: %s", exc)
    try:
        mgmt = server.mgmt_netdiag(plan.helper_vmid, plan.mgmt_ip)
        if mgmt:
            parts.append(mgmt)
    except Exception as exc:
        logger.debug("mgmt netdiag failed: %s", exc)
    return "\n\n".join(parts) or None


def retest_held(run_id: int, server: Any, plan: RunPlan, *, probe_fn=None, now=time.monotonic, sleep=time.sleep) -> str:
    """Re-probe a held environment (no new restore). Returns the new result; on
    'passed' the caller offers to promote."""
    probe_fn = probe_fn or (lambda s, ip, t: s.probe(ip, t, plan.require_icmp))
    result, checks = _probe_loop(server, plan, now=now, sleep=sleep, probe_fn=probe_fn, run_id=run_id)
    db_restore.update_run(run_id, checks=checks)
    return result


def promote_held(run_id: int, plan: RunPlan, server: Any, *, duration_seconds: int = 0) -> None:
    """Document a (re-tested, green) held run as a successful restore and reap."""
    _teardown(server, plan)
    db_restore.record_run_result(
        run_id,
        plan.vm_id,
        "passed",
        duration_seconds=duration_seconds,
        evidence_note="promoted after manual fix + green re-test",
    )
    db_restore.set_vm_next_run(plan.vm_id, None)


def reap_held(run_id: int, plan: RunPlan, server: Any) -> None:
    """Discard a held environment without documenting success; schedule a retry."""
    _teardown(server, plan)
    run = db_restore.get_run(run_id)
    result = run["result"] if run and run.get("result") in ("failed", "error") else "failed"
    db_restore.record_run_result(
        run_id, plan.vm_id, result, duration_seconds=run.get("duration_seconds") or 0, status=result
    )
    db_restore.schedule_retry(plan.vm_id, plan.retry_days)


def reconcile_on_startup() -> None:
    """On restart, a run left in 'running' must NOT be orphaned — while it is
    'running' its VMs stay owned, so the reaper won't destroy a restore that is
    still progressing on the PVE host (a deploy must not interrupt it). Only if
    the restore server is no longer configured — so we cannot resume or even
    reach the host — do we mark such runs error. Resumable runs are left
    'running' for the resume pass (``sync.reconcile_and_resume_restores``)."""
    from app.services import restore_config

    if restore_config.load() is not None:
        return
    for r in db_restore.list_runs(limit=500, status="running"):
        db_restore.update_run(r["id"], status="error", result="error", finished_at=_utcnow())


def resume_run(
    run_id: int,
    plan: RunPlan,
    server: Any,
    upid: str | None,
    *,
    now: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
    probe_fn: Callable[[Any, str, list[dict[str, Any]]], dict[str, Any]] | None = None,
    should_cancel: Callable[[], bool] | None = None,
) -> str:
    """Resume a restore whose app process restarted mid-run — a deploy MUST NOT
    interrupt or tear down a running restore.

    ``qmrestore`` runs detached on the PVE host, so the data copy keeps going
    across an app restart; this re-attaches and finishes the tail. The one rule
    that makes a deploy safe: **never touch a restore that is still in progress,
    and never destroy what is already healthy.**

    - Re-attach to the running PVE task (by UPID; discovered from the host if the
      UPID was lost because the deploy hit before it was persisted) and wait for
      it to finish.
    - Then wait for the config lock to clear *before* any config write or boot.
      If it is still locked, the restore is genuinely mid-copy and we could not
      attach — LEAVE the run 'running' (resumed again next pass) and tear nothing
      down. (This also fixes the "VM is locked (create)" that used to abort and
      then destroy the very VM being restored.)
    - PRESERVE the existing environment: the helper is (re)built only when it is
      actually gone — not on every deploy — and the NICs/boot are redone only
      when the VM is not already running. So a deploy during a restore, or during
      its probe window, is a no-op instead of a teardown.
    """
    probe_fn = probe_fn or (lambda s, ip, t: s.probe(ip, t, plan.require_icmp))
    should_cancel = should_cancel or (lambda: db_restore.cancel_requested(run_id))
    start = now()
    # The isolated sandbox bridge is runtime-created (ip link); guarantee it is up
    # again (idempotent — also covers a host reboot during the downtime).
    try:
        server.ensure_sandbox_bridge()
    except Exception as exc:
        logger.warning("resume: ensure_sandbox_bridge (continuing): %s", exc)
    # Re-attach to the still-running restore task. If the UPID was lost, find the
    # live restore task for this vmid on the host so we still wait for it.
    if not upid:
        try:
            upid = server.find_running_restore_upid(plan.restore_vmid)
        except Exception as exc:
            logger.debug("resume: restore-task discovery (ignored): %s", exc)
    result = "error"
    checks: dict[str, Any] = {}
    try:
        if upid:
            server.poll_task(
                upid,
                on_progress=_progress_updater(server, run_id, plan.restore_vmid),
                now=now,
                sleep=sleep,
                should_cancel=should_cancel,
            )
        # Never write config or boot while the restore still holds the flock. If it
        # does not clear, the restore is still in progress — leave the run 'running'
        # and tear NOTHING down; the next resume pass picks it up.
        try:
            server.wait_unlocked(plan.restore_vmid, sleep=sleep)
        except RestoreCancelled:
            raise
        except Exception:
            logger.warning(
                "resume: VM %s still locked (restore in progress) — leaving run %s 'running', no teardown",
                plan.restore_vmid,
                run_id,
            )
            return "running"
        # Lock clear → the restore finished. Finish the tail without disturbing what
        # is already healthy: keep a present helper, (re)build only a gone one; only
        # re-point NICs + boot when the VM is not already running.
        running = server.is_running(plan.restore_vmid)
        if not server.exists(plan.helper_vmid, is_lxc=True):
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
        if not running:
            db_restore.update_run(run_id, phase="network")
            server.rewrite_nics(plan.restore_vmid, plan.vlan_tag, plan.guest_vlan, name=plan.label)
            db_restore.update_run(run_id, phase="booting")
            try:
                server.start(plan.restore_vmid)
            except Exception as exc:
                logger.debug("resume: start (maybe already running): %s", exc)
        # Advance the phase to probing — without this a resumed run kept the stale
        # "finalizing" (Cache-Flush) the restore's last 100% progress callback set,
        # so the card showed "finalizing" while it was really checking reachability.
        db_restore.update_run(run_id, phase="probing")
        result, checks = _probe_loop(
            server, plan, now=now, sleep=sleep, probe_fn=probe_fn, run_id=run_id, should_cancel=should_cancel
        )
    except RestoreCancelled as exc:
        logger.info("restore run %s cancelled during resume: %s", run_id, exc)
        result = "cancelled"
        checks = {"note": "cancelled by operator"}
    except Exception as exc:
        logger.warning("restore run %s resume failed: %s", run_id, exc)
        # Last safety net: if the restore is somehow still locked/in-progress, do
        # NOT finalize (which would tear the live VM down). Leave it 'running'.
        try:
            if server.exists(plan.restore_vmid) and server.vm_locked(plan.restore_vmid):
                logger.warning("resume: run %s not finalized — restore still locked", run_id)
                return "running"
        except Exception as exc2:
            logger.debug("resume: lock recheck (ignored): %s", exc2)
        result = "error"
        checks = {"error": str(exc)}
    # The run started before the app restart: measure from its recorded
    # started_at, not from this resume — else a deploy mid-run cuts the duration
    # down to the tail (a 20-minute restore showing 0:00).
    run = db_restore.get_run(run_id) or {}
    duration = _seconds_since(run.get("started_at"))
    if duration is None:
        duration = int(now() - start)
    return _finalize(run_id, plan, result, checks, duration, server)


def fits_disk(free_bytes: int | None, needed_bytes: int | None, safety: float = 1.2) -> bool:
    """Whether a restore of ``needed_bytes`` fits in ``free_bytes`` (spec §15
    RAM/disk skip). Unknown sizes optimistically fit."""
    if not free_bytes or not needed_bytes:
        return True
    return free_bytes >= needed_bytes * safety


# The host always keeps a slice of RAM for itself: 10%, but at least 10 GiB.
RAM_RESERVE_FRACTION = 0.10
RAM_RESERVE_MIN_BYTES = 10 * 1024**3


def ram_reserve_bytes(total_bytes: int | None) -> int:
    """How much RAM stays reserved for the host: max(10% of total, 10 GiB)."""
    if not total_bytes:
        return RAM_RESERVE_MIN_BYTES
    return max(int(total_bytes * RAM_RESERVE_FRACTION), RAM_RESERVE_MIN_BYTES)


def fits_ram(free_bytes: int | None, total_bytes: int | None, needed_bytes: int | None) -> bool:
    """Whether a guest needing ``needed_bytes`` of RAM can boot while leaving the
    host its reserve (10%, min 10 GiB). Unknown guest RAM optimistically fits."""
    if not needed_bytes:
        return True
    if free_bytes is None:
        return True
    usable = free_bytes - ram_reserve_bytes(total_bytes)
    return usable >= needed_bytes


def _progress_updater(server: Any, run_id: int, vmid: int) -> Callable[[dict], None]:
    """A stateful ``on_progress`` that turns pbs-restore's per-disk 0–100%% into
    overall progress across all the guest's volumes. pbs-restore reports "read N
    bytes" per disk and resets it at each disk, so sum it: completed disks' bytes
    (added when the next disk's ``drive-…`` line appears) plus the current disk's
    read, over the guest's total disk size (storage content). Monotonic. Falls back
    to the raw per-disk percent until the total is known, so an unrecognised log
    format or a server that can't report sizes behaves exactly as before."""
    st = {"done_base": 0, "cur_read": 0, "cur_drive": None, "total": None}

    def on_progress(p: dict[str, Any]) -> None:
        drive = p.get("drive")
        read = int(p.get("bytes_done") or 0)
        if drive and drive != st["cur_drive"]:
            if st["cur_drive"] is not None:
                st["done_base"] += st["cur_read"]  # the previous disk is fully restored
            st["cur_drive"] = drive
            st["cur_read"] = 0
            try:  # (re)read the total as disks are allocated; max() keeps it from shrinking
                st["total"] = max(st["total"] or 0, server.restore_total_bytes(vmid)) or None
            except Exception as exc:
                logger.debug("restore_total_bytes during progress failed: %s", exc)
        if read > st["cur_read"]:
            st["cur_read"] = read  # read climbs within a disk
        fields = overall_restore_fields(st["done_base"] + st["cur_read"], st["total"]) if drive else None
        db_restore.update_run(run_id, **(fields or _progress_fields(p)))

    return on_progress


def _progress_fields(p: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    if "percent" in p:
        out["percent"] = p["percent"]
        # The data copy is done at 100% but the task keeps running: Proxmox then
        # rescans volumes and the slow target flushes its write cache (can take
        # minutes). Show that as its own step instead of a frozen 100%. Derive the
        # phase from the CURRENT percent every time, not just set it once at 100:
        # PBS logs "progress 100%" per disk, so a multi-disk guest hits 100 on its
        # first disk early — the phase must fall back to "restoring" while the next
        # disk copies, and only read "finalizing" once the last line is a true 100%.
        out["phase"] = "finalizing" if p["percent"] >= 100 else "restoring"
    if "bytes_done" in p:
        out["bytes_done"] = p["bytes_done"]
    if "bytes_total" in p:
        out["bytes_total"] = p["bytes_total"]
    return out


def _seconds_since(started_at: Any) -> int | None:
    """Wall-clock seconds since ``started_at`` (a datetime; naive = UTC), or
    ``None`` when it is missing."""
    if started_at is None or not hasattr(started_at, "tzinfo"):
        return None
    if started_at.tzinfo is None:
        from datetime import UTC

        started_at = started_at.replace(tzinfo=UTC)
    return max(0, int((_utcnow() - started_at).total_seconds()))


def _utcnow():
    from datetime import UTC, datetime

    return datetime.now(UTC)
