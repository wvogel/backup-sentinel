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
    probe_fn: Callable[[Any, int, list[dict[str, Any]]], dict[str, Any]],
) -> tuple[str, dict[str, Any]]:
    """Poll until success or the window elapses. Returns (result, checks)."""
    had_ip = plan.primary_ip is not None
    if not had_ip:
        return "limited", {}

    sleep(plan.first_delay)
    deadline = now() + plan.window
    checks: dict[str, Any] = {}
    result = "failed"
    while True:
        checks = probe_fn(server, plan.helper_vmid, plan.probe_targets)
        primary = checks.get(plan.primary_ip)
        result = restore_probe.classify(primary, require_icmp=plan.require_icmp, tcp_ports=plan.tcp_ports, had_ip=True)
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
    probe_fn: Callable[[Any, int, list[dict[str, Any]]], dict[str, Any]] | None = None,
) -> str:
    """Run the full pipeline for ``run_id`` and persist the outcome. Returns the
    final status (passed/limited/failed/error/held)."""
    probe_fn = probe_fn or (lambda s, hv, t: s.probe(hv, t))
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
        server.restore(
            plan.restore_vmid,
            plan.source,
            plan.snapshot_id,
            plan.target_storage,
            on_progress=lambda p: db_restore.update_run(run_id, **_progress_fields(p)),
        )
        server.rewrite_nics(plan.restore_vmid, plan.vlan_tag)
        server.provision_helper(plan.helper_vmid, plan.vlan_tag, plan.mgmt_ip, plan.reservations)
        server.start(plan.restore_vmid)
        result, checks = _probe_loop(server, plan, now=now, sleep=sleep, probe_fn=probe_fn)
    except Exception as exc:
        logger.warning("restore run %s failed: %s", run_id, exc)
        result = "error"
        checks = {"error": str(exc)}

    duration = int(now() - start)
    return _finalize(run_id, plan, result, checks, duration, server)


def _finalize(run_id: int, plan: RunPlan, result: str, checks: dict, duration: int, server: Any) -> str:
    if result != "passed" and plan.hold_on_failure and result in ("failed", "error"):
        # Keep the environment up for manual inspection; the queue continues.
        db_restore.record_run_result(
            run_id, plan.vm_id, result, duration_seconds=duration, checks=checks, status="held"
        )
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
    for fn in (
        lambda: server.shutdown(plan.restore_vmid),
        lambda: server.destroy(plan.helper_vmid, is_lxc=True),
        lambda: server.destroy(plan.restore_vmid),
    ):
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
    probe_fn = probe_fn or (lambda s, hv, t: s.probe(hv, t))
    result, checks = _probe_loop(server, plan, now=now, sleep=sleep, probe_fn=probe_fn)
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
    """A crash leaves runs in 'running'; mark them error (held survive)."""
    for r in db_restore.list_runs(limit=500, status="running"):
        db_restore.update_run(r["id"], status="error", result="error", finished_at=_utcnow())


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
    if "bytes_done" in p:
        out["bytes_done"] = p["bytes_done"]
    if "bytes_total" in p:
        out["bytes_total"] = p["bytes_total"]
    return out


def _utcnow():
    from datetime import UTC, datetime

    return datetime.now(UTC)
