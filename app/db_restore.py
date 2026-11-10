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
        "phase",
        "detected_ip",
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


def enqueue_if_never_passed(vm_id: int, trigger: str = "scheduled") -> int | None:
    """Queue a run for ``vm_id`` only if it has NO active run (queued/running/held)
    and has NEVER had a passed run. Atomic (one INSERT…SELECT), so it is safe to
    call eagerly — e.g. the moment auto-restore is switched on for a VM that was
    never successfully restored, so it enters the queue at once instead of waiting
    for the next sync tick. Returns the new run id, or None if it was skipped."""
    with connect() as conn, conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO restore_runs (vm_id, trigger, status)
            SELECT %s, %s, 'queued'
            WHERE NOT EXISTS (SELECT 1 FROM restore_runs WHERE vm_id = %s AND status = ANY(%s))
              AND NOT EXISTS (SELECT 1 FROM restore_runs WHERE vm_id = %s AND result = 'passed')
            RETURNING id
            """,
            (vm_id, trigger, vm_id, list(LIVE_RUN_STATUSES), vm_id),
        )
        row = cur.fetchone()
        conn.commit()
        return row["id"] if row else None


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


_RUNS_JOIN = "FROM restore_runs r LEFT JOIN vms v ON v.id = r.vm_id LEFT JOIN clusters c ON c.id = v.cluster_id"


def _runs_filter(
    status: str | None, trigger: str | None, cluster: str | None, vm_query: str | None
) -> tuple[str, list[Any]]:
    """Shared WHERE clause + params for the runs list and its count, so paging and
    the total always agree. Filtering is in SQL (never load all rows)."""
    conds: list[str] = []
    params: list[Any] = []
    if status:
        conds.append("r.status = %s")
        params.append(status)
    if trigger:
        conds.append("r.trigger = %s")
        params.append(trigger)
    if cluster:
        conds.append("c.name = %s")
        params.append(cluster)
    if vm_query:
        conds.append("(v.name ILIKE %s OR CAST(v.vmid AS TEXT) LIKE %s)")
        params += [f"%{vm_query}%", f"%{vm_query}%"]
    where = (" WHERE " + " AND ".join(conds)) if conds else ""
    return where, params


def list_runs(
    limit: int = 50,
    status: str | None = None,
    *,
    offset: int = 0,
    trigger: str | None = None,
    cluster: str | None = None,
    vm_query: str | None = None,
) -> list[RestoreRunRow]:
    # Join the VM name/vmid so the UI shows "srv01 (100)" not an internal id,
    # plus the guest's provisioned size and per-disk breakdown for the size bars.
    base = (
        "SELECT r.*, v.name AS vm_name, v.vmid AS vmid_src, c.name AS cluster_name, c.slug AS cluster_slug, "
        "v.disk_bytes, "
        "(SELECT COALESCE(jsonb_agg(jsonb_build_object('dev', d.dev, 'storage', d.storage, 'size_bytes', d.size_bytes) "
        "ORDER BY d.ordinal), '[]'::jsonb) FROM vm_disks d WHERE d.vm_id = v.id) AS disks "
        f"{_RUNS_JOIN}"
    )
    where, params = _runs_filter(status, trigger, cluster, vm_query)
    with connect() as conn, conn.cursor() as cur:
        cur.execute(
            # Queued on top in the order the drainer takes them (manual sort_order,
            # then FIFO), then the running one, then finished runs by start, newest first.
            f"{base}{where} ORDER BY CASE r.status WHEN 'queued' THEN 0 WHEN 'running' THEN 1 ELSE 2 END, "
            "CASE WHEN r.status = 'queued' THEN r.sort_order END ASC NULLS LAST, "
            "CASE WHEN r.status = 'queued' THEN r.queued_at END ASC, "
            "COALESCE(r.started_at, r.queued_at) DESC, r.id DESC LIMIT %s OFFSET %s",
            (*params, limit, offset),
        )
        return list(cur.fetchall())


def count_runs(
    status: str | None = None,
    *,
    trigger: str | None = None,
    cluster: str | None = None,
    vm_query: str | None = None,
) -> int:
    """Total rows matching the same filter as ``list_runs`` — for pagination."""
    where, params = _runs_filter(status, trigger, cluster, vm_query)
    with connect() as conn, conn.cursor() as cur:
        cur.execute(f"SELECT COUNT(*) AS n {_RUNS_JOIN}{where}", params)
        return int(cur.fetchone()["n"])


def run_filter_clusters() -> list[str]:
    """Distinct cluster names that have runs — populates the cluster filter."""
    with connect() as conn, conn.cursor() as cur:
        cur.execute(
            f"SELECT DISTINCT c.name {_RUNS_JOIN} WHERE c.name IS NOT NULL ORDER BY c.name",
        )
        return [row["name"] for row in cur.fetchall()]


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
        cur.execute(
            "SELECT * FROM restore_runs WHERE status = 'queued' ORDER BY sort_order ASC NULLS LAST, queued_at, id LIMIT 1"
        )
        return cur.fetchone()


def list_queued_runs() -> list[RestoreRunRow]:
    """All queued runs in effective order (manual ``sort_order`` first, then FIFO
    by ``queued_at`` for runs never reordered) — for resource-aware draining."""
    with connect() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT * FROM restore_runs WHERE status = 'queued' ORDER BY sort_order ASC NULLS LAST, queued_at, id"
        )
        return list(cur.fetchall())


def reorder_queue(ordered_ids: list[int]) -> int:
    """Apply a new manual order to the not-yet-started (queued) runs: set each
    run's ``sort_order`` to its position in ``ordered_ids`` (0-based). Ids that
    are not currently 'queued' are skipped, so a stale drag can never reorder —
    or revive — a run that has already started. Returns the number updated."""
    if not ordered_ids:
        return 0
    with connect() as conn, conn.cursor() as cur:
        updated = 0
        for pos, run_id in enumerate(ordered_ids):
            cur.execute(
                "UPDATE restore_runs SET sort_order = %s WHERE id = %s AND status = 'queued'",
                (pos, run_id),
            )
            updated += cur.rowcount
        conn.commit()
    return updated


def has_active_run() -> bool:
    with connect() as conn, conn.cursor() as cur:
        cur.execute("SELECT 1 FROM restore_runs WHERE status = 'running' LIMIT 1")
        return cur.fetchone() is not None


def has_queued_runs() -> bool:
    with connect() as conn, conn.cursor() as cur:
        cur.execute("SELECT 1 FROM restore_runs WHERE status = 'queued' LIMIT 1")
        return cur.fetchone() is not None


def delete_run(run_id: int) -> bool:
    """Remove a queued run from the queue. Only 'queued' runs may be deleted —
    running/held runs own a live environment and must be reaped, not dropped."""
    with connect() as conn, conn.cursor() as cur:
        cur.execute("DELETE FROM restore_runs WHERE id = %s AND status = 'queued'", (run_id,))
        deleted = cur.rowcount > 0
        conn.commit()
    return deleted


def request_cancel(run_id: int) -> bool:
    """Flag a running run for cancellation. The worker's long loops poll this and
    abort with a clean teardown. Only a 'running' run can be cancelled."""
    with connect() as conn, conn.cursor() as cur:
        cur.execute(
            "UPDATE restore_runs SET cancel_requested = TRUE WHERE id = %s AND status = 'running'",
            (run_id,),
        )
        ok = cur.rowcount > 0
        conn.commit()
    return ok


def cancel_requested(run_id: int) -> bool:
    """Whether a cancel was requested for this run (polled by the worker loops)."""
    with connect() as conn, conn.cursor() as cur:
        cur.execute("SELECT cancel_requested FROM restore_runs WHERE id = %s", (run_id,))
        row = cur.fetchone()
        return bool(row and row["cancel_requested"])


def queue_state() -> str:
    """One word for the top-bar indicator: 'running' if a restore is active,
    'queued' if runs are waiting, else 'idle'. (Pause is tracked separately.)"""
    with connect() as conn, conn.cursor() as cur:
        cur.execute("SELECT status FROM restore_runs WHERE status IN ('running', 'queued')")
        states = {r["status"] for r in cur.fetchall()}
    if "running" in states:
        return "running"
    if "queued" in states:
        return "queued"
    return "idle"


def latest_run_result_by_vm() -> dict[int, str]:
    """The most recent finished restore-run result per VM (passed/limited/
    failed/error), for the VM-list result dot. Newest finished run wins."""
    with connect() as conn, conn.cursor() as cur:
        cur.execute(
            """
            SELECT DISTINCT ON (vm_id) vm_id, result
            FROM restore_runs
            WHERE result IS NOT NULL
            ORDER BY vm_id, finished_at DESC NULLS LAST, id DESC
            """
        )
        return {r["vm_id"]: r["result"] for r in cur.fetchall()}


def active_run_by_vm() -> dict[int, dict[str, Any]]:
    """Per VM its live run for the VM-list state icon: ``{"status": "running",
    "percent": n}`` while a restore runs, else ``{"status": "queued"}`` while one
    waits in the queue. Running wins over queued."""
    with connect() as conn, conn.cursor() as cur:
        cur.execute(
            """
            SELECT DISTINCT ON (vm_id) vm_id, status, percent
            FROM restore_runs
            WHERE status IN ('running', 'queued', 'planned')
            ORDER BY vm_id, (status = 'running') DESC, id
            """
        )
        return {
            r["vm_id"]: {"status": "running" if r["status"] == "running" else "queued", "percent": r["percent"]}
            for r in cur.fetchall()
        }


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
    phase_timings: dict[str, int] | None = None,
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
        if phase_timings is not None:
            sets.append("phase_timings = %s")
            params.append(Jsonb(phase_timings))
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
