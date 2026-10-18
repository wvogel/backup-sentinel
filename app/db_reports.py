from __future__ import annotations

from calendar import monthrange
from datetime import UTC, date, datetime
from typing import Any

from psycopg.types.json import Jsonb

from app.db_clusters import list_clusters
from app.db_core import connect
from app.db_governance import vm_governance_rows
from app.services.backup_status import RESTORE_DUE_STATUSES
from app.services.reporting import serialize_report_payload
from app.types import MonthlyReportRow


def cluster_summaries(
    clusters: list[dict[str, Any]] | None = None, vm_rows: list[dict[str, Any]] | None = None
) -> list[dict[str, Any]]:
    """Build dashboard summary for all clusters using batch queries. Pass
    `clusters` and `vm_rows` when the caller has loaded them already."""
    if clusters is None:
        clusters = list_clusters()
    if not clusters:
        return []

    # Single query for all VMs across all clusters
    all_vms = vm_governance_rows() if vm_rows is None else vm_rows

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
        overdue_restore_count = sum(int(vm["restore_due_status"] in RESTORE_DUE_STATUSES) for vm in vms)
        summaries.append(
            {
                "cluster_name": cluster["name"],
                "cluster_slug": cluster["slug"],
                "api_url": cluster["api_url"],
                "registered_at": cluster["registered_at"],
                "last_pve_sync_at": cluster.get("last_pve_sync_at"),
                "last_pve_sync_success_at": cluster.get("last_pve_sync_success_at"),
                "last_pve_sync_failure_started_at": cluster.get("last_pve_sync_failure_started_at"),
                "last_pve_sync_ok": cluster.get("last_pve_sync_ok"),
                "node_count": node_counts.get(cid, 0),
                "qdevice": cluster.get("qdevice"),
                "vm_count": len(vms),
                "warning_count": warning_count,
                "critical_count": critical_count,
                "encryption": enc_by_cluster.get(
                    cid, {"total_backups": 0, "encrypted_backups": 0, "pct_encrypted": 100}
                ),
                "overdue_restore_count": overdue_restore_count,
                # From the last sync's in-progress markers (no live PVE call on the dashboard).
                "running_vms": [vm["vm_name"] for vm in vms if vm["in_progress_since"]],
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


def sync_backup_events_for_source(
    cluster_id: int, source: str, events: list[dict[str, Any]], *, mark_removed: bool = True
) -> None:
    """Upsert this source's events; ones no longer seen are marked removed
    unless `mark_removed` is off (a source could not be read completely)."""
    with connect() as conn, conn.cursor() as cur:
        cur.execute("SELECT id, vmid FROM vms WHERE cluster_id = %s", (cluster_id,))
        vmid_to_id = {row["vmid"]: row["id"] for row in cur.fetchall()}
        seen_keys: set[tuple[int, datetime]] = set()
        rows: list[tuple[Any, ...]] = []

        for event in events:
            vm_id = vmid_to_id.get(event["vmid"])
            if vm_id is None:
                continue
            started_at = event["started_at"]
            if hasattr(started_at, "replace"):
                started_at = started_at.replace(second=0, microsecond=0)
            seen_keys.add((vm_id, started_at))
            rows.append(
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
                    event.get("guest_name"),
                )
            )
        # One batch instead of a round trip per snapshot (in order, so the
        # merge rules apply exactly as row by row).
        if rows:
            cur.executemany(
                """
                INSERT INTO backup_events
                    (vm_id, started_at, finished_at, duration_seconds, size_bytes, encrypted, verify_state, status, upid, source, guest_name)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (vm_id, started_at) DO UPDATE SET
                    size_bytes    = COALESCE(EXCLUDED.size_bytes, backup_events.size_bytes),
                    encrypted     = COALESCE(EXCLUDED.encrypted, backup_events.encrypted),
                    verify_state  = COALESCE(EXCLUDED.verify_state, backup_events.verify_state),
                    guest_name    = COALESCE(EXCLUDED.guest_name, backup_events.guest_name),
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
                rows,
            )

        today = datetime.now(UTC).date()
        vm_ids = tuple(vmid_to_id.values())
        if not vm_ids or not mark_removed:
            conn.commit()
            return

        cur.execute(
            """
            SELECT id, vm_id, started_at FROM backup_events
            WHERE vm_id = ANY(%s) AND source = %s AND removed_at IS NULL
            """,
            (list(vm_ids), source),
        )
        gone = [
            row["id"]
            for row in cur.fetchall()
            if (row["vm_id"], row["started_at"].replace(second=0, microsecond=0)) not in seen_keys
        ]
        if gone:
            cur.execute("UPDATE backup_events SET removed_at = %s WHERE id = ANY(%s)", (today, gone))
        conn.commit()


def update_backup_event_storages(cluster_id: int, events: list[dict[str, Any]]) -> None:
    """Record on which storages each backup currently lies.

    The same backup can sit on several storages (e.g. a PBS plus an offsite
    sync copy) but is one row per VM and start minute. Pass ALL events of a
    sync (PVE and PBS); each row's set is replaced with what was seen now.
    """
    storages_by_key: dict[tuple[int, datetime], set[str]] = {}
    locations_by_key: dict[tuple[int, datetime], set[str]] = {}
    for event in events:
        storage = event.get("storage")
        started_at = event.get("started_at")
        if not storage or started_at is None:
            continue
        key = (event["vmid"], started_at.replace(second=0, microsecond=0))
        storages_by_key.setdefault(key, set()).add(storage)
        if event.get("location"):
            locations_by_key.setdefault(key, set()).add(event["location"])

    with connect() as conn, conn.cursor() as cur:
        cur.execute("SELECT id, vmid FROM vms WHERE cluster_id = %s", (cluster_id,))
        vmid_to_id = {row["vmid"]: row["id"] for row in cur.fetchall()}
        cur.execute(
            "UPDATE backup_events SET storages = NULL, locations = NULL"
            " WHERE vm_id = ANY(%s) AND (storages IS NOT NULL OR locations IS NOT NULL)",
            (list(vmid_to_id.values()),),
        )
        rows = [
            (sorted(storages), sorted(locations_by_key.get(key, ())), vmid_to_id[key[0]], key[1])
            for key, storages in storages_by_key.items()
            if key[0] in vmid_to_id
        ]
        if rows:
            cur.executemany(
                "UPDATE backup_events SET storages = %s, locations = %s WHERE vm_id = %s AND started_at = %s",
                rows,
            )
        conn.commit()


def typical_backup_durations(cluster_id: int) -> dict[int, int]:
    """Median runtime (seconds) of each VM's last 10 successful backups.

    Only VMs with at least 3 known runtimes (from task logs) are included.
    """
    with connect() as conn, conn.cursor() as cur:
        cur.execute(
            """
            SELECT vmid, PERCENTILE_CONT(0.5) WITHIN GROUP (ORDER BY duration_seconds) AS median
            FROM (
                SELECT v.vmid, be.duration_seconds,
                       ROW_NUMBER() OVER (PARTITION BY be.vm_id ORDER BY be.started_at DESC) AS rn
                FROM backup_events be
                JOIN vms v ON v.id = be.vm_id
                WHERE v.cluster_id = %s AND be.status = 'ok' AND be.duration_seconds IS NOT NULL
            ) recent
            WHERE rn <= 10
            GROUP BY vmid
            HAVING COUNT(*) >= 3
            """,
            (cluster_id,),
        )
        return {row["vmid"]: int(row["median"]) for row in cur.fetchall()}


def weekly_backup_stats(since: datetime) -> dict[str, Any]:
    """Failed backups (total and per cluster slug) and full runs (by "cluster / VM") since `since`, for the weekly report."""
    with connect() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT COUNT(*) AS failed FROM backup_events WHERE status <> 'ok' AND started_at >= %s",
            (since,),
        )
        failed = cur.fetchone()["failed"]
        cur.execute(
            """
            SELECT c.slug, COUNT(*) AS failed
            FROM backup_events be
            JOIN vms v ON v.id = be.vm_id
            JOIN clusters c ON c.id = v.cluster_id
            WHERE be.status <> 'ok' AND be.started_at >= %s
            GROUP BY c.slug
            """,
            (since,),
        )
        failed_by_cluster = {row["slug"]: row["failed"] for row in cur.fetchall()}
        cur.execute(
            """
            SELECT DISTINCT c.name || ' / ' || v.name AS vm
            FROM backup_events be
            JOIN vms v ON v.id = be.vm_id
            JOIN clusters c ON c.id = v.cluster_id
            WHERE be.full_backup AND be.started_at >= %s
            ORDER BY 1
            """,
            (since,),
        )
        return {
            "failed": failed,
            "failed_by_cluster": failed_by_cluster,
            "full_runs": [row["vm"] for row in cur.fetchall()],
        }


def processed_backup_task_upids(cluster_id: int) -> set[str]:
    with connect() as conn, conn.cursor() as cur:
        cur.execute("SELECT upid FROM processed_backup_tasks WHERE cluster_id = %s", (cluster_id,))
        return {row["upid"] for row in cur.fetchall()}


def mark_backup_tasks_processed(cluster_id: int, upids: list[str]) -> None:
    now = datetime.now(UTC)
    with connect() as conn, conn.cursor() as cur:
        for upid in upids:
            cur.execute(
                "INSERT INTO processed_backup_tasks (upid, cluster_id, processed_at) VALUES (%s, %s, %s)"
                " ON CONFLICT (upid) DO NOTHING",
                (upid, cluster_id, now),
            )
        # Tasks are fetched for the last 31 days (fetch_finished_backup_tasks); older markers are useless.
        cur.execute(
            "DELETE FROM processed_backup_tasks WHERE cluster_id = %s AND processed_at < %s - INTERVAL '35 days'",
            (cluster_id, now),
        )
        conn.commit()


def update_backup_event_details(cluster_id: int, runs: list[Any]) -> int:
    """Attach vzdump log details to the matching backup events.

    A run matches the event of the same VM whose start is closest, within
    ±10 minutes (PBS snapshot times match exactly; file backups may drift).
    Returns the number of successful runs that found no event (yet).
    """
    unmatched = 0
    with connect() as conn, conn.cursor() as cur:
        cur.execute("SELECT id, vmid FROM vms WHERE cluster_id = %s", (cluster_id,))
        vmid_to_id = {row["vmid"]: row["id"] for row in cur.fetchall()}
        for run in runs:
            vm_id = vmid_to_id.get(run.vmid)
            if run.started_at is None or vm_id is None:
                continue
            if run.failed:
                # The sync may have seen this backup running and left an
                # in-progress placeholder: it is over, and it failed.
                cur.execute(
                    """
                    UPDATE backup_events SET status = 'failed', finished_at = NOW()
                    WHERE id = (
                        SELECT id FROM backup_events
                        WHERE vm_id = %s
                          AND finished_at IS NULL
                          AND status != 'failed'
                          AND COALESCE(size_bytes, 0) <= 1
                          AND started_at BETWEEN %s - INTERVAL '10 minutes' AND %s + INTERVAL '10 minutes'
                        ORDER BY ABS(EXTRACT(EPOCH FROM started_at - %s))
                        LIMIT 1
                    )
                    """,
                    (vm_id, run.started_at, run.started_at, run.started_at),
                )
                continue
            cur.execute(
                """
                UPDATE backup_events SET
                    duration_seconds = COALESCE(%s, duration_seconds),
                    transferred_bytes = COALESCE(%s, transferred_bytes),
                    throughput_bps = COALESCE(%s, throughput_bps),
                    reused_pct = COALESCE(%s, reused_pct),
                    full_backup = COALESCE(%s, full_backup)
                WHERE id = (
                    SELECT id FROM backup_events
                    WHERE vm_id = %s
                      AND started_at BETWEEN %s - INTERVAL '10 minutes' AND %s + INTERVAL '10 minutes'
                    ORDER BY ABS(EXTRACT(EPOCH FROM started_at - %s))
                    LIMIT 1
                )
                """,
                (
                    run.duration_seconds,
                    run.transferred_bytes,
                    run.throughput_bps,
                    run.reused_pct,
                    run.full_backup,
                    vm_id,
                    run.started_at,
                    run.started_at,
                    run.started_at,
                ),
            )
            if cur.rowcount == 0:
                unmatched += 1
        conn.commit()
    return unmatched


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
      2. Only truly orphaned stubs — no newer OK row AND older than
         `max_age_hours` — are marked as failed. The sync passes 6 h; the
         dashboard asks PVE live, so longer backups still show as running.

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
