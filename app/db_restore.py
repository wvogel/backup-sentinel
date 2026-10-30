"""Database layer for automated restore testing.

Covers `restore_runs` (the operational record per run, spec §14) and
`vm_network_interfaces` (guest IPs collected from production, spec §9).
Restore-server *config* lives in `app.services.restore_config` (encrypted
settings); scheduling/result semantics live in `app.db_governance` and
`app.services.restore_worker`.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from psycopg.types.json import Jsonb

from app.db_core import connect
from app.types import RestoreRunRow, VMNetworkInterfaceRow

# Statuses that occupy or reserve host resources (an allocated VMID / VLAN tag /
# management IP): a run in one of these still "owns" its environment.
LIVE_RUN_STATUSES = ("queued", "running", "held")

# Columns that may be updated live during a run.
_UPDATABLE = frozenset(
    {
        "trigger",
        "status",
        "restore_vmid",
        "helper_vmid",
        "vlan_tag",
        "mgmt_ip",
        "source_pbs",
        "source_datastore",
        "source_namespace",
        "snapshot_id",
        "target_storages",
        "bytes_total",
        "bytes_done",
        "percent",
        "throughput_bps",
        "eta",
        "result",
        "restore_test_id",
        "log",
        "checks",
        "started_at",
        "finished_at",
        "duration_seconds",
    }
)


def create_run(vm_id: int, trigger: str = "scheduled") -> int:
    with connect() as conn, conn.cursor() as cur:
        cur.execute(
            "INSERT INTO restore_runs (vm_id, trigger, status) VALUES (%s, %s, 'queued') RETURNING id",
            (vm_id, trigger),
        )
        conn.commit()
        return cur.fetchone()["id"]


def get_run(run_id: int) -> RestoreRunRow | None:
    with connect() as conn, conn.cursor() as cur:
        cur.execute("SELECT * FROM restore_runs WHERE id = %s", (run_id,))
        return cur.fetchone()


def update_run(run_id: int, **fields: Any) -> None:
    """Update whitelisted columns of a run. `checks` is JSON-encoded."""
    cols = [c for c in fields if c in _UPDATABLE]
    if not cols:
        return
    values: list[Any] = []
    for c in cols:
        v = fields[c]
        values.append(Jsonb(v) if c == "checks" and v is not None else v)
    assignments = ", ".join(f"{c} = %s" for c in cols)
    with connect() as conn, conn.cursor() as cur:
        cur.execute(f"UPDATE restore_runs SET {assignments} WHERE id = %s", (*values, run_id))
        conn.commit()


def list_runs(limit: int = 50, status: str | None = None) -> list[RestoreRunRow]:
    with connect() as conn, conn.cursor() as cur:
        if status:
            cur.execute(
                "SELECT * FROM restore_runs WHERE status = %s ORDER BY queued_at DESC, id DESC LIMIT %s",
                (status, limit),
            )
        else:
            cur.execute("SELECT * FROM restore_runs ORDER BY queued_at DESC, id DESC LIMIT %s", (limit,))
        return list(cur.fetchall())


def active_and_held_runs() -> list[RestoreRunRow]:
    """Runs that currently own an environment's resources (for allocation)."""
    with connect() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT * FROM restore_runs WHERE status IN ('running', 'held') ORDER BY id",
        )
        return list(cur.fetchall())


def queued_vm_ids() -> set[int]:
    """VM ids with a run that is not yet finished (queued/running/held)."""
    with connect() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT DISTINCT vm_id FROM restore_runs WHERE status = ANY(%s)",
            (list(LIVE_RUN_STATUSES),),
        )
        return {row["vm_id"] for row in cur.fetchall()}


def next_queued_run() -> RestoreRunRow | None:
    with connect() as conn, conn.cursor() as cur:
        cur.execute("SELECT * FROM restore_runs WHERE status = 'queued' ORDER BY queued_at, id LIMIT 1")
        return cur.fetchone()


def has_active_run() -> bool:
    with connect() as conn, conn.cursor() as cur:
        cur.execute("SELECT 1 FROM restore_runs WHERE status = 'running' LIMIT 1")
        return cur.fetchone() is not None


def set_network_interfaces(vm_id: int, rows: list[dict[str, Any]]) -> None:
    """Replace the stored NICs for a VM (full refresh each sync)."""
    now = datetime.now(UTC)
    with connect() as conn, conn.cursor() as cur:
        cur.execute("DELETE FROM vm_network_interfaces WHERE vm_id = %s", (vm_id,))
        for r in rows:
            cur.execute(
                """
                INSERT INTO vm_network_interfaces
                    (vm_id, nic_index, mac, ip_addresses, prefix, is_primary, source, seen_at)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                """,
                (
                    vm_id,
                    r["nic_index"],
                    r["mac"],
                    r.get("ip_addresses", []),
                    r.get("prefix"),
                    r.get("is_primary", False),
                    r.get("source", ""),
                    now,
                ),
            )
        conn.commit()


def get_network_interfaces(vm_id: int) -> list[VMNetworkInterfaceRow]:
    with connect() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT * FROM vm_network_interfaces WHERE vm_id = %s ORDER BY nic_index",
            (vm_id,),
        )
        return list(cur.fetchall())


# --- Scheduling & result semantics (spec §8, §14, §15) -----------------------

# Results that count as a completed test (reset the due clock). failed/error do
# NOT, so a broken restore stays overdue and visible.
_COUNTS_AS_TEST = ("passed", "limited")

_DUE_CANDIDATE_SQL = """
    SELECT v.id AS vm_id, v.cluster_id, v.node_name, v.vmid, v.name AS vm_name, v.guest_type,
           p.restore_cadence, p.hold_on_failure, p.probe_primary_ip, p.probe_tcp_ports,
           p.probe_require_icmp, p.restore_retry_days, p.probe_window_seconds,
           p.probe_first_delay_seconds, p.probe_interval_seconds, p.stall_backstop_factor
    FROM vms v
    JOIN vm_policies p ON p.vm_id = v.id
    WHERE p.auto_restore_enabled
      AND NOT EXISTS (
          SELECT 1 FROM restore_runs r WHERE r.vm_id = v.id AND r.status IN ('queued', 'running', 'held')
      )
      AND (
          (p.next_run_at IS NOT NULL AND p.next_run_at <= NOW())
          OR (
              p.next_run_at IS NULL AND (
                  (SELECT MAX(tested_at) FROM restore_tests rt WHERE rt.vm_id = v.id) IS NULL
                  OR (SELECT MAX(tested_at) FROM restore_tests rt WHERE rt.vm_id = v.id)
                       <= (NOW()::date - (CASE p.restore_cadence
                             WHEN 'month' THEN 30 WHEN 'quarter' THEN 91
                             WHEN 'year' THEN 365 ELSE 182 END))
              )
          )
      )
    ORDER BY v.id
"""


def due_auto_restore_candidates() -> list[dict[str, Any]]:
    """Enabled VMs whose cadence/next_run_at makes them due and which have no
    live run. The *auto-restorable* (3-tuple) filter is applied by the enqueue
    step, which has the scratch host's live datastore list (spec §7, §15)."""
    with connect() as conn, conn.cursor() as cur:
        cur.execute(_DUE_CANDIDATE_SQL)
        return list(cur.fetchall())


def record_run_result(
    run_id: int,
    vm_id: int,
    result: str,
    *,
    duration_seconds: int,
    evidence_note: str = "",
    checks: dict[str, Any] | None = None,
    log: str | None = None,
    status: str | None = None,
) -> int | None:
    """Finalize a run. Writes a `restore_tests` row ONLY for passed/limited (so
    failed/error/held never reset the due clock). Returns the new test id or None.
    `status` defaults to `result`; pass 'held' to park the environment."""
    now = datetime.now(UTC)
    test_id: int | None = None
    with connect() as conn, conn.cursor() as cur:
        if result in _COUNTS_AS_TEST:
            cur.execute(
                """
                INSERT INTO restore_tests
                    (vm_id, tested_at, result, recovery_type, duration_minutes, evidence_note, created_at)
                VALUES (%s, %s, %s, 'auto-full-vm', %s, %s, %s) RETURNING id
                """,
                (vm_id, now.date(), result, max(0, round(duration_seconds / 60)), evidence_note, now),
            )
            test_id = cur.fetchone()["id"]
        sets = ["status = %s", "result = %s", "finished_at = %s", "duration_seconds = %s", "restore_test_id = %s"]
        params: list[Any] = [status or result, result, now, duration_seconds, test_id]
        if checks is not None:
            sets.append("checks = %s")
            params.append(Jsonb(checks))
        if log is not None:
            sets.append("log = %s")
            params.append(log)
        params.append(run_id)
        cur.execute(f"UPDATE restore_runs SET {', '.join(sets)} WHERE id = %s", params)
        conn.commit()
    return test_id


def set_vm_next_run(vm_id: int, when: datetime | None) -> None:
    """Set (or clear) a VM's manual/retry next-run time."""
    with connect() as conn, conn.cursor() as cur:
        cur.execute("UPDATE vm_policies SET next_run_at = %s WHERE vm_id = %s", (when, vm_id))
        conn.commit()


def schedule_retry(vm_id: int, retry_days: int) -> None:
    """After a failed/error run, re-arm the VM to run again in `retry_days`."""
    from datetime import timedelta

    set_vm_next_run(vm_id, datetime.now(UTC) + timedelta(days=retry_days))
