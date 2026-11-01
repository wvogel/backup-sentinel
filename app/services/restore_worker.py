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
from app.services.restore_server import RestoreCancelled

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
    probe_targets: list[dict[str, Any]]  # {ip, ports}
    primary_ip: str | None
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

    The target IP is resolved per iteration: the recorded/override IP if known,
    otherwise discovered from the restored VM's guest agent (which only answers
    once it has booted). So a VM whose IP we don't have recorded is no longer an
    instant "limited" — we wait for the agent within the window and then probe."""
    sleep(plan.first_delay)
    deadline = now() + plan.window
    target_ip = plan.primary_ip  # override or recorded; may be None
    checks: dict[str, Any] = {}
    result = "limited"
    while True:
        if should_cancel and should_cancel():
            raise RestoreCancelled("restore cancelled by operator")
        if not target_ip:
            target_ip = next(iter(server.guest_agent_ips(plan.restore_vmid) or []), None)
            if target_ip and run_id is not None:
                db_restore.update_run(run_id, detected_ip=target_ip)
        if target_ip:
            targets = plan.probe_targets if plan.primary_ip else [{"ip": target_ip, "ports": plan.tcp_ports}]
            checks = probe_fn(server, plan.mgmt_ip, targets)
            result = restore_probe.classify(
                checks.get(target_ip), require_icmp=plan.require_icmp, tcp_ports=plan.tcp_ports, had_ip=True
            )
        else:
            result, checks = "limited", {"note": "no target IP yet (no record, override, or guest agent)"}
        if result == "passed" or now() >= deadline:
            return result, checks
        sleep(plan.interval)


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
    try:
        server.verify_host_identity(production_hosts)
        server.reap(owned_vmids())
        server.ensure_sandbox_bridge()
        # Bring the helper up FIRST and fail fast (≤60s) if it can't be reached.
        # A restore is expensive (minutes, many GB), so don't pay for it when the
        # cheap helper — which must serve the guest's DHCP and answer probes —
        # never comes up. (We can revert to overlapping the two with the VM boot
        # once reachability is rock-solid.)
        _checkpoint()
        db_restore.update_run(run_id, phase="helper")
        server.provision_helper(plan.helper_vmid, plan.vlan_tag, plan.mgmt_ip, plan.reservations)
        _checkpoint()
        db_restore.update_run(run_id, phase="restoring")
        server.restore(
            plan.restore_vmid,
            plan.source,
            plan.snapshot_id,
            plan.target_storage,
            on_progress=lambda p: db_restore.update_run(run_id, **_progress_fields(p)),
            on_task=lambda upid: db_restore.update_run(run_id, task_upid=upid),
            should_cancel=should_cancel,
        )
        _checkpoint()
        db_restore.update_run(run_id, phase="network")
        server.rewrite_nics(plan.restore_vmid, plan.vlan_tag)
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
    except RestoreCancelled as exc:
        logger.info("restore run %s cancelled: %s", run_id, exc)
        result = "cancelled"
        checks = {"note": "cancelled by operator"}
    except Exception as exc:
        logger.warning("restore run %s failed: %s", run_id, exc)
        result = "error"
        checks = {"error": str(exc)}

    duration = int(now() - start)
    return _finalize(run_id, plan, result, checks, duration, server)


def _finalize(run_id: int, plan: RunPlan, result: str, checks: dict, duration: int, server: Any) -> str:
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
            run_id, plan.vm_id, result, duration_seconds=duration, checks=checks, status="held"
        )
        db_restore.update_run(run_id, helper_vmid=None, mgmt_ip=None)
        return "held"

    # Teardown (passed/limited, or failed without hold): reap this env.
    _teardown(server, plan)
    db_restore.record_run_result(run_id, plan.vm_id, result, duration_seconds=duration, checks=checks)
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
    """Resume a restore whose app process restarted mid-run. Re-attaches to the
    PVE restore task (by UPID) and WAITS for it (plus the config lock) before
    re-running the tail idempotently (fresh helper, NICs, start, probe). Waiting
    for the lock is essential: the original restore task can still be running on
    the host, and starting the VM then fails with "can't lock file". The restore
    VM itself is preserved; only the cheap helper is rebuilt. Returns the final
    status."""
    probe_fn = probe_fn or (lambda s, ip, t: s.probe(ip, t, plan.require_icmp))
    should_cancel = should_cancel or (lambda: db_restore.cancel_requested(run_id))
    start = now()
    result = "error"
    checks: dict[str, Any] = {}
    try:
        if upid:
            server.poll_task(
                upid,
                on_progress=lambda p: db_restore.update_run(run_id, **_progress_fields(p)),
                now=now,
                sleep=sleep,
                should_cancel=should_cancel,
            )
        # The helper may be half-provisioned or gone; rebuild it fresh. The
        # restore VM itself is never destroyed here.
        try:
            server.destroy(plan.helper_vmid, is_lxc=True)
        except Exception as exc:
            logger.debug("resume: helper cleanup (ignored): %s", exc)
        server.rewrite_nics(plan.restore_vmid, plan.vlan_tag)
        server.provision_helper(plan.helper_vmid, plan.vlan_tag, plan.mgmt_ip, plan.reservations)
        server.wait_unlocked(plan.restore_vmid)  # the restore may still hold the lock
        try:
            server.start(plan.restore_vmid)
        except Exception as exc:
            logger.debug("resume: start (maybe already running): %s", exc)
        result, checks = _probe_loop(
            server, plan, now=now, sleep=sleep, probe_fn=probe_fn, run_id=run_id, should_cancel=should_cancel
        )
    except RestoreCancelled as exc:
        logger.info("restore run %s cancelled during resume: %s", run_id, exc)
        result = "cancelled"
        checks = {"note": "cancelled by operator"}
    except Exception as exc:
        logger.warning("restore run %s resume failed: %s", run_id, exc)
        result = "error"
        checks = {"error": str(exc)}
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


def _progress_fields(p: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    if "percent" in p:
        out["percent"] = p["percent"]
        # The data copy is done at 100% but the task keeps running: Proxmox then
        # rescans volumes and the slow target flushes its write cache (can take
        # minutes). Show that as its own step instead of a frozen 100%.
        if p["percent"] >= 100:
            out["phase"] = "finalizing"
    if "bytes_done" in p:
        out["bytes_done"] = p["bytes_done"]
    if "bytes_total" in p:
        out["bytes_total"] = p["bytes_total"]
    return out


def _utcnow():
    from datetime import UTC, datetime

    return datetime.now(UTC)
