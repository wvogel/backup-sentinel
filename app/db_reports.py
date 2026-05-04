from __future__ import annotations

from calendar import monthrange
from datetime import UTC, date, datetime
from typing import Any

from psycopg.types.json import Jsonb

from app.db_clusters import list_clusters
from app.db_core import connect
from app.db_governance import vm_governance_rows
from app.services.reporting import serialize_report_payload
from app.types import MonthlyReportRow


def cluster_summaries() -> list[dict[str, Any]]:
    """Build dashboard summary for all clusters using batch queries."""
    clusters = list_clusters()
    if not clusters:
        return []

    # Single query for all VMs across all clusters
    all_vms = vm_governance_rows()

    # Group VMs by cluster_id
    vms_by_cluster: dict[int, list[dict[str, Any]]] = {}
    for vm in all_vms:
        vms_by_cluster.setdefault(vm["cluster_id"], []).append(vm)

    # Batch: node counts per cluster (single query)
    node_counts: dict[int, int] = {}
    with connect() as conn, conn.cursor() as cur:
        cur.execute("SELECT cluster_id, COUNT(*) AS cnt FROM nodes GROUP BY cluster_id")
        for row in cur.fetchall():
            node_counts[row["cluster_id"]] = row["cnt"]

    # Batch: encryption audit for all clusters (single query)
    enc_by_cluster: dict[int, dict[str, Any]] = {}
    with connect() as conn, conn.cursor() as cur:
        cur.execute(
            """
            SELECT
                v.cluster_id,
                COUNT(*) FILTER (WHERE be.status = 'ok' AND be.removed_at IS NULL) AS total_backups,
                COUNT(*) FILTER (WHERE be.status = 'ok' AND be.removed_at IS NULL AND be.encrypted = TRUE) AS encrypted_backups
            FROM backup_events be
            JOIN vms v ON v.id = be.vm_id
            GROUP BY v.cluster_id
            """
        )
        for row in cur.fetchall():
            total = row["total_backups"] or 0
            encrypted = row["encrypted_backups"] or 0
            pct = round(encrypted / total * 100) if total else 100
            enc_by_cluster[row["cluster_id"]] = {
                "total_backups": total,
                "encrypted_backups": encrypted,
                "pct_encrypted": pct,
            }

    summaries: list[dict[str, Any]] = []
    for cluster in clusters:
        cid = cluster["id"]
        vms = vms_by_cluster.get(cid, [])
        warning_count = sum(int(vm["backup_severity"] == "warning") for vm in vms)
        critical_count = sum(int(vm["backup_severity"] == "critical") for vm in vms)
        overdue_restore_count = sum(int(vm["restore_due_status"] in {"warning", "critical"}) for vm in vms)
        summaries.append(
            {
                "cluster_name": cluster["name"],
                "cluster_slug": cluster["slug"],
                "api_url": cluster["api_url"],
                "registered_at": cluster["registered_at"],
                "last_pve_sync_at": cluster.get("last_pve_sync_at"),
                "last_pve_sync_success_at": cluster.get("last_pve_sync_success_at"),
                "last_pve_sync_failure_started_at": cluster.get("last_pve_sync_failure_started_at"),
                "node_count": node_counts.get(cid, 0),
                "vm_count": len(vms),
                "warning_count": warning_count,
                "critical_count": critical_count,
                "encryption": enc_by_cluster.get(
                    cid, {"total_backups": 0, "encrypted_backups": 0, "pct_encrypted": 100}
                ),
                "overdue_restore_count": overdue_restore_count,
            }
        )
    return summaries


def archive_monthly_report(
    report_month: date, trigger_mode: str, pdf_path: Any, json_path: Any, report: dict[str, Any]
) -> None:
    now = datetime.now(UTC)
    snapshot = serialize_report_payload(report)
    with connect() as conn, conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO monthly_reports (report_month, generated_at, trigger_mode, pdf_path, json_path, snapshot_json)
            VALUES (%s, %s, %s, %s, %s, %s)
            """,
            (report_month, now, trigger_mode, str(pdf_path), str(json_path), Jsonb(snapshot)),
        )
        conn.commit()


def list_monthly_reports() -> list[MonthlyReportRow]:
    with connect() as conn, conn.cursor() as cur:
        cur.execute("SELECT * FROM monthly_reports ORDER BY generated_at DESC")
        return list(cur.fetchall())


def delete_monthly_report(report_id: int) -> MonthlyReportRow | None:
    with connect() as conn, conn.cursor() as cur:
        cur.execute("DELETE FROM monthly_reports WHERE id = %s RETURNING *", (report_id,))
        row = cur.fetchone()
        conn.commit()
        return row


def get_monthly_report(report_id: int) -> MonthlyReportRow | None:
    with connect() as conn, conn.cursor() as cur:
        cur.execute("SELECT * FROM monthly_reports WHERE id = %s", (report_id,))
        return cur.fetchone()


def sync_backup_events_for_source(cluster_id: int, source: str, events: list[dict[str, Any]]) -> None:
    with connect() as conn, conn.cursor() as cur:
        cur.execute("SELECT id, vmid FROM vms WHERE cluster_id = %s", (cluster_id,))
        vmid_to_id = {row["vmid"]: row["id"] for row in cur.fetchall()}
        seen_keys: set[tuple[int, datetime]] = set()

        for event in events:
            vm_id = vmid_to_id.get(event["vmid"])
            if vm_id is None:
                continue
            started_at = event["started_at"]
            if hasattr(started_at, "replace"):
                started_at = started_at.replace(second=0, microsecond=0)
            seen_keys.add((vm_id, started_at))
            cur.execute(
                """
                INSERT INTO backup_events
                    (vm_id, started_at, finished_at, duration_seconds, size_bytes, encrypted, verify_state, status, upid, source)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (vm_id, started_at) DO UPDATE SET
                    size_bytes    = COALESCE(EXCLUDED.size_bytes, backup_events.size_bytes),
                    encrypted     = COALESCE(EXCLUDED.encrypted, backup_events.encrypted),
                    verify_state  = COALESCE(EXCLUDED.verify_state, backup_events.verify_state),
                    source        = CASE WHEN backup_events.source = 'pbs' AND EXCLUDED.source = 'pve' THEN 'pbs'
                                         ELSE COALESCE(EXCLUDED.source, backup_events.source) END,
                    -- A real backup with size > 1 byte is the source of truth — even if an
                    -- earlier sync (or stale-stub cleanup) flagged the row as failed.
                    -- Only preserve `failed` when no real size has been reported yet.
                    status        = CASE
                        WHEN COALESCE(EXCLUDED.size_bytes, 0) > 1 THEN EXCLUDED.status
                        WHEN backup_events.status = 'failed' THEN backup_events.status
                        ELSE EXCLUDED.status
                    END,
                    finished_at   = CASE
                        WHEN COALESCE(EXCLUDED.size_bytes, 0) > 1
                          THEN COALESCE(EXCLUDED.finished_at, backup_events.finished_at)
                        ELSE backup_events.finished_at
                    END,
                    removed_at    = NULL
                """,
                (
                    vm_id,
                    started_at,
                    event.get("finished_at"),
                    event.get("duration_seconds"),
                    event.get("size_bytes"),
                    event.get("encrypted"),
                    event.get("verify_state"),
                    event["status"],
                    event.get("upid", ""),
                    source,
                ),
            )

        today = datetime.now(UTC).date()
        vm_ids = tuple(vmid_to_id.values())
        if not vm_ids:
            conn.commit()
            return

        cur.execute(
            """
            SELECT id, vm_id, started_at FROM backup_events
            WHERE vm_id = ANY(%s) AND source = %s AND removed_at IS NULL
            """,
            (list(vm_ids), source),
        )
        for row in cur.fetchall():
            key = (row["vm_id"], row["started_at"].replace(second=0, microsecond=0))
            if key not in seen_keys:
                cur.execute("UPDATE backup_events SET removed_at = %s WHERE id = %s", (today, row["id"]))
        conn.commit()


def cleanup_stale_inprogress_backups(cluster_id: int, max_age_hours: int = 48) -> int:
    """Reconcile stub in-progress backup rows with their real outcome.

    PVE emits a row per VM with a pre-job placeholder (`size_bytes <= 1`,
    `finished_at IS NULL`) and later writes a real row once the backup
    finishes. When the backup job runs overnight — or succeeds in pieces —
    the stubs from the first half of the job can still be sitting in the
    DB while later VMs are already complete.

    Strategy:
      1. If the stub has a later OK + size>1 row for the same VM within
         36 hours, delete the stub — it's the same successful job.
         This also repairs rows that an earlier buggy version of this
         function marked as `status='failed'`.
      2. Only truly orphaned stubs — no newer OK row AND started >48h ago —
         are marked as failed. 48h safely covers even the longest real
         backup jobs while still catching genuinely abandoned runs.

    Returns the total number of rows affected (deleted + marked failed).
    """
    with connect() as conn, conn.cursor() as cur:
        # 1. Drop stubs (including previously mis-marked 'failed' stubs)
        #    when a later successful backup exists for the same VM.
        cur.execute(
            """
            DELETE FROM backup_events
            WHERE id IN (
                SELECT be.id
                FROM backup_events be
                JOIN vms v ON v.id = be.vm_id
                WHERE v.cluster_id = %s
                  AND be.removed_at IS NULL
                  AND COALESCE(be.size_bytes, 0) <= 1
                  AND EXISTS (
                      SELECT 1
                      FROM backup_events be2
                      WHERE be2.vm_id = be.vm_id
                        AND be2.started_at > be.started_at
                        AND be2.started_at < be.started_at + interval '36 hours'
                        AND be2.status = 'ok'
                        AND COALESCE(be2.size_bytes, 0) > 1
                        AND be2.removed_at IS NULL
                  )
            )
            RETURNING id
            """,
            (cluster_id,),
        )
        merged = len(cur.fetchall())

        # 2. Mark genuinely orphaned stubs as failed — no successor, old enough.
        cur.execute(
            """
            UPDATE backup_events
            SET finished_at = now(),
                status = 'failed'
            WHERE id IN (
                SELECT be.id
                FROM backup_events be
                JOIN vms v ON v.id = be.vm_id
                WHERE v.cluster_id = %s
                  AND be.finished_at IS NULL
                  AND be.removed_at IS NULL
                  AND be.status != 'failed'
                  AND COALESCE(be.size_bytes, 0) <= 1
                  AND be.started_at < now() - make_interval(hours => %s)
            )
            RETURNING id
            """,
            (cluster_id, max_age_hours),
        )
        aborted = len(cur.fetchall())
        conn.commit()
        return merged + aborted


def upsert_backup_events(cluster_id: int, events: list[dict[str, Any]]) -> None:
    if not events:
        return
    source = next((str(event.get("source") or "").strip() for event in events if event.get("source")), "")
    if not source:
        return
    sync_backup_events_for_source(cluster_id, source, events)


def list_backup_events_for_period(
    period_start: datetime, period_end: datetime, cluster_id: int | None = None
) -> list[dict[str, Any]]:
    params: list[Any] = [period_start, period_end]
    query = """
        SELECT
            be.id, be.started_at, be.finished_at, be.duration_seconds,
            be.size_bytes, be.encrypted, be.verify_state,
            be.status, be.upid, be.source, be.removed_at,
            v.name AS vm_name, v.vmid, v.node_name,
            c.name AS cluster_name, c.slug AS cluster_slug, c.id AS cluster_id
        FROM backup_events be
        JOIN vms v ON v.id = be.vm_id
        JOIN clusters c ON c.id = v.cluster_id
        WHERE be.started_at >= %s AND be.started_at < %s
    """
    if cluster_id is not None:
        query += " AND v.cluster_id = %s"
        params.append(cluster_id)
    query += " ORDER BY c.name, v.name, be.started_at DESC"
    with connect() as conn, conn.cursor() as cur:
        cur.execute(query, params)
        return list(cur.fetchall())


def list_all_available_backup_events(cluster_id: int | None = None) -> list[dict[str, Any]]:
    params: list[Any] = []
    query = """
        SELECT
            be.id, be.started_at, be.finished_at, be.duration_seconds,
            be.size_bytes, be.encrypted, be.verify_state,
            be.status, be.upid, be.source, be.removed_at,
            v.name AS vm_name, v.vmid, v.node_name,
            c.name AS cluster_name, c.slug AS cluster_slug, c.id AS cluster_id
        FROM backup_events be
        JOIN vms v ON v.id = be.vm_id
        JOIN clusters c ON c.id = v.cluster_id
        WHERE be.removed_at IS NULL
    """
    if cluster_id is not None:
        query += " AND v.cluster_id = %s"
        params.append(cluster_id)
    query += " ORDER BY c.name, v.name, be.started_at DESC"
    with connect() as conn, conn.cursor() as cur:
        cur.execute(query, params)
        return list(cur.fetchall())


def list_all_report_backup_events(cluster_id: int | None = None, *, limit: int = 50_000) -> list[dict[str, Any]]:
    params: list[Any] = []
    query = """
        SELECT
            be.id, be.started_at, be.finished_at, be.duration_seconds,
            be.size_bytes, be.encrypted, be.verify_state,
            be.status, be.upid, be.source, be.removed_at,
            v.name AS vm_name, v.vmid, v.node_name,
            c.name AS cluster_name, c.slug AS cluster_slug, c.id AS cluster_id
        FROM backup_events be
        JOIN vms v ON v.id = be.vm_id
        JOIN clusters c ON c.id = v.cluster_id
        WHERE 1 = 1
    """
    if cluster_id is not None:
        query += " AND v.cluster_id = %s"
        params.append(cluster_id)
    query += " ORDER BY c.name, v.name, be.started_at DESC LIMIT %s"
    params.append(limit)
    with connect() as conn, conn.cursor() as cur:
        cur.execute(query, params)
        return list(cur.fetchall())


def get_monthly_report_for_month(month: date) -> MonthlyReportRow | None:
    with connect() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT * FROM monthly_reports WHERE report_month = %s ORDER BY generated_at DESC LIMIT 1",
            (month,),
        )
        return cur.fetchone()


def backup_events_for_month(year: int, month: int, cluster_id: int | None = None) -> list[dict[str, Any]]:
    month_start = datetime(year, month, 1, tzinfo=UTC)
    _, last_day = monthrange(year, month)
    month_end = datetime(year, month, last_day, 23, 59, 59, tzinfo=UTC)
    return list_backup_events_for_period(month_start, month_end, cluster_id)
