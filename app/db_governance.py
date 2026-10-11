from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from typing import Any

from app.config import BACKUP_DAY_OFFSET_HOURS, DEFAULT_TIMEZONE
from app.db_core import connect
from app.services.backup_status import RESTORE_CLASS_INTERVALS, RESTORE_DUE_STATUSES, assess_backup_status
from app.types import RestoreTestRow


def vm_governance_rows(cluster_id: int | None = None) -> list[dict[str, Any]]:
    query = """
        SELECT
            v.id,
            v.cluster_id,
            c.name AS cluster_name,
            c.slug AS cluster_slug,
            v.node_name,
            v.vmid,
            v.name AS vm_name,
            v.backup_kind,
            v.missing_since,
            CASE WHEN v.missing_since IS NOT NULL THEN (
                SELECT ARRAY(
                    SELECT DISTINCT unnest(be.storages)
                    FROM backup_events be
                    WHERE be.vm_id = v.id AND be.status = 'ok' AND be.removed_at IS NULL
                      AND COALESCE(be.size_bytes, 0) > 1
                    ORDER BY 1
                )
            ) END AS remaining_storages,
            COALESCE(p.backup_policy_override, v.backup_kind) AS effective_backup_kind,
            be_latest.started_at AS last_backup_at,
            be_latest.source AS last_backup_source,
            be_in_progress.started_at AS in_progress_since,
            COALESCE(p.owner_name, 'unassigned') AS owner_name,
            COALESCE(p.criticality, 'medium') AS criticality,
            p.backup_policy_override,
            p.restore_interval_days,
            COALESCE(p.restore_required, TRUE) AS restore_required,
            COALESCE(p.rpo_hours, 24) AS rpo_hours,
            COALESCE(p.notes, '') AS notes,
            rt.tested_at AS last_restore_tested_at,
            rt.result AS last_restore_result,
            rt.recovery_type AS last_restore_recovery_type,
            rt.duration_minutes AS last_restore_duration_minutes
        FROM vms v
        JOIN clusters c ON c.id = v.cluster_id
        LEFT JOIN vm_policies p ON p.vm_id = v.id
        LEFT JOIN LATERAL (
            SELECT be.started_at, be.source
            FROM backup_events be
            WHERE be.vm_id = v.id AND be.status = 'ok' AND be.removed_at IS NULL
              AND COALESCE(be.size_bytes, 0) > 1
            ORDER BY be.started_at DESC
            LIMIT 1
        ) be_latest ON TRUE

        LEFT JOIN LATERAL (
            SELECT be.started_at
            FROM backup_events be
            WHERE be.vm_id = v.id
              AND be.removed_at IS NULL
              AND be.finished_at IS NULL
              AND be.status != 'failed'
              AND COALESCE(be.size_bytes, 0) <= 1
            ORDER BY be.started_at DESC
            LIMIT 1
        ) be_in_progress ON TRUE
        LEFT JOIN LATERAL (
            SELECT tested_at, result, recovery_type, duration_minutes
            FROM restore_tests
            WHERE vm_id = v.id
            ORDER BY tested_at DESC, created_at DESC
            LIMIT 1
        ) rt ON TRUE
        WHERE (v.missing_since IS NULL OR be_latest.started_at IS NOT NULL)
    """
    params: list[Any] = []
    if cluster_id is not None:
        query += " AND v.cluster_id = %s"
        params.append(cluster_id)
    query += " ORDER BY c.name, v.name"

    rows: list[dict[str, Any]]
    with connect() as conn, conn.cursor() as cur:
        cur.execute(query, params)
        rows = list(cur.fetchall())

    now = datetime.now(UTC)
    for row in rows:
        effective_backup_kind = row["effective_backup_kind"]
        status_backup_kind = effective_backup_kind
        if status_backup_kind == "none" and not row["backup_policy_override"]:
            status_backup_kind = "unconfigured"
        # Deleted VMs only linger for their remaining backups — no new backups
        # or restore tests are expected, so they never raise warnings.
        row["vm_gone"] = row["missing_since"] is not None
        if row["vm_gone"]:
            status_backup_kind = "none"
        row["status_backup_kind"] = status_backup_kind
        severity, age_days = assess_backup_status(status_backup_kind, row["last_backup_at"], now)
        row["backup_severity"] = severity
        row["backup_age_days"] = age_days
        from app.i18n import t

        if status_backup_kind == "unconfigured":
            row["backup_kind_label"] = t("cluster.policy_auto")
        else:
            row["backup_kind_label"] = {
                "daily": t("cluster.policy_daily"),
                "weekly": t("cluster.policy_weekly"),
                "none": t("cluster.policy_none"),
            }.get(effective_backup_kind, effective_backup_kind)
        # Compact badge text; the full label stays in the tooltip.
        row["backup_kind_short"] = {"daily": "d", "weekly": "w", "none": "–"}.get(
            effective_backup_kind if status_backup_kind != "unconfigured" else "", row["backup_kind_label"]
        )

        row["restore_class"] = row["criticality"] if row["restore_required"] else "none"
        if row["restore_required"] and status_backup_kind != "none":
            class_interval = RESTORE_CLASS_INTERVALS.get(row["criticality"], RESTORE_CLASS_INTERVALS["medium"])
            if status_backup_kind == "weekly":
                class_interval = max(class_interval, 365)
            interval_days = row["restore_interval_days"] or class_interval
            row["restore_interval_days"] = interval_days
            if row["last_restore_tested_at"] is None:
                # Never tested: due, but not "critical" — that is reserved for
                # tests that have actually expired.
                row["restore_due_status"] = "open"
                row["restore_age_days"] = None
            else:
                restore_age = (now.date() - row["last_restore_tested_at"]).days
                row["restore_age_days"] = restore_age
                # Warn ahead of the due date: 1 month for yearly intervals,
                # 2 weeks for quarterly (or shorter) ones.
                warn_lead_days = 30 if interval_days >= 180 else 14
                if restore_age >= interval_days:
                    row["restore_due_status"] = "critical"
                elif restore_age >= max(1, interval_days - warn_lead_days):
                    row["restore_due_status"] = "warning"
                else:
                    row["restore_due_status"] = "ok"
        else:
            row["restore_due_status"] = "ok"
            row["restore_age_days"] = None
    return rows


def update_vm_restore_class(vm_id: int, restore_class: str) -> None:
    """high/medium/low → criticality (sets the interval); none → no test required."""
    required = restore_class != "none"
    criticality = restore_class if required else "medium"
    with connect() as conn, conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO vm_policies (vm_id, owner_name, criticality, restore_interval_days, restore_required, rpo_hours, notes)
            VALUES (%s, 'unassigned', %s, NULL, %s, 24, '')
            ON CONFLICT (vm_id) DO UPDATE SET
                criticality = EXCLUDED.criticality,
                restore_required = EXCLUDED.restore_required
            """,
            (vm_id, criticality, required),
        )
        conn.commit()


def create_restore_test(
    vm_id: int,
    tested_at: date,
    result: str,
    recovery_type: str,
    duration_minutes: int,
    evidence_note: str = "",
) -> None:
    now = datetime.now(UTC)
    with connect() as conn, conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO restore_tests (vm_id, tested_at, result, recovery_type, duration_minutes, evidence_note, created_at)
            VALUES (%s, %s, %s, %s, %s, %s, %s)
            """,
            (vm_id, tested_at, result, recovery_type, duration_minutes, evidence_note, now),
        )
        conn.commit()


def recent_restore_tests(cluster_id: int | None = None) -> list[RestoreTestRow]:
    query = """
        SELECT rt.*, v.name AS vm_name, v.vmid, c.name AS cluster_name
        FROM restore_tests rt
        JOIN vms v ON v.id = rt.vm_id
        JOIN clusters c ON c.id = v.cluster_id
    """
    params: list[Any] = []
    if cluster_id is not None:
        query += " WHERE v.cluster_id = %s"
        params.append(cluster_id)
    query += " ORDER BY rt.tested_at DESC, rt.created_at DESC LIMIT 50"
    with connect() as conn, conn.cursor() as cur:
        cur.execute(query, params)
        return list(cur.fetchall())


def detect_backup_size_anomalies(cluster_id: int, threshold: float = 0.5) -> list[dict[str, Any]]:
    query = """
        WITH recent AS (
            SELECT
                v.id AS vm_id,
                v.name AS vm_name,
                v.vmid,
                be.size_bytes,
                be.started_at,
                ROW_NUMBER() OVER (PARTITION BY v.id ORDER BY be.started_at DESC) AS rn
            FROM backup_events be
            JOIN vms v ON v.id = be.vm_id
            WHERE v.cluster_id = %s
              AND be.status = 'ok'
              AND be.size_bytes IS NOT NULL
              AND COALESCE(be.size_bytes, 0) > 1
              AND be.removed_at IS NULL
        )
        SELECT
            vm_id, vm_name, vmid,
            MAX(CASE WHEN rn = 1 THEN size_bytes END) AS latest_size,
            AVG(CASE WHEN rn BETWEEN 2 AND 6 THEN size_bytes END) AS avg_size
        FROM recent
        WHERE rn <= 6
        GROUP BY vm_id, vm_name, vmid
    """
    with connect() as conn, conn.cursor() as cur:
        cur.execute(query, (cluster_id,))
        rows = list(cur.fetchall())
    result = []
    for row in rows:
        latest_size = row["latest_size"]
        avg_size = row["avg_size"]
        if latest_size and avg_size and avg_size > 0:
            deviation = abs(latest_size - avg_size) / avg_size
            if deviation >= threshold:
                result.append({**row, "deviation": deviation})
    return result


def encryption_audit(cluster_id: int | None = None) -> dict[str, Any]:
    query = """
        SELECT
            COUNT(*) FILTER (WHERE be.status = 'ok' AND be.removed_at IS NULL) AS total_backups,
            COUNT(*) FILTER (WHERE be.status = 'ok' AND be.removed_at IS NULL AND be.encrypted = TRUE) AS encrypted_backups
        FROM backup_events be
        JOIN vms v ON v.id = be.vm_id
    """
    params: list[Any] = []
    if cluster_id is not None:
        query += " WHERE v.cluster_id = %s"
        params.append(cluster_id)
    with connect() as conn, conn.cursor() as cur:
        cur.execute(query, params)
        row = cur.fetchone() or {"total_backups": 0, "encrypted_backups": 0}
    total = row["total_backups"] or 0
    encrypted = row["encrypted_backups"] or 0
    pct = round(encrypted / total * 100) if total else 100
    return {"total_backups": total, "encrypted_backups": encrypted, "pct_encrypted": pct}


def unencrypted_vms(cluster_id: int | None = None) -> list[dict[str, Any]]:
    query = """
        SELECT DISTINCT ON (v.id)
            v.id, v.name AS vm_name, v.vmid, v.node_name, be.started_at AS last_unencrypted_at
        FROM backup_events be
        JOIN vms v ON v.id = be.vm_id
        LEFT JOIN vm_policies p ON p.vm_id = v.id
        WHERE be.removed_at IS NULL AND be.status = 'ok' AND be.encrypted = FALSE
          AND COALESCE(be.size_bytes, 0) > 1
          AND COALESCE(p.backup_policy_override, v.backup_kind) != 'none'
    """
    params: list[Any] = []
    if cluster_id is not None:
        query += " AND v.cluster_id = %s"
        params.append(cluster_id)
    query += " ORDER BY v.id, be.started_at DESC"
    with connect() as conn, conn.cursor() as cur:
        cur.execute(query, params)
        return list(cur.fetchall())


def vm_backup_sparkline_data(cluster_id: int, days: int = 31) -> dict[int, list[dict[str, Any]]]:
    """Return per VM one entry per day for the last `days` days.

    Each entry: {"date", "status": ok|deleted|failed|none, "size_bytes", plus the
    vzdump log details of the day's latest successful backup: "duration_seconds",
    "throughput_bps", "reused_pct", "full_backup"} — all for the tooltip.

    The "backup day" boundary is shifted by BACKUP_DAY_OFFSET_HOURS (default: 6h)
    so a job that finishes at 00:30 still counts for the previous evening's run.
    Timestamps are evaluated in DEFAULT_TIMEZONE so the day boundary matches
    the operator's wall clock, not UTC.
    """
    start = datetime.now(UTC) - timedelta(days=days - 1)
    with connect() as conn, conn.cursor() as cur:
        cur.execute(
            """
            SELECT
                be.vm_id,
                ((be.started_at - make_interval(hours => %s)) AT TIME ZONE %s)::date AS backup_date,
                BOOL_OR(be.status = 'ok' AND be.removed_at IS NULL AND COALESCE(be.size_bytes, 0) > 1) AS has_ok,
                BOOL_OR(be.status = 'ok' AND be.removed_at IS NOT NULL AND COALESCE(be.size_bytes, 0) > 1) AS has_deleted,
                BOOL_OR(be.status != 'ok') AS has_failed,
                MAX(be.size_bytes) FILTER (WHERE be.status = 'ok' AND COALESCE(be.size_bytes, 0) > 1) AS size_bytes,
                (ARRAY_AGG(be.duration_seconds ORDER BY be.started_at DESC)
                    FILTER (WHERE be.status = 'ok' AND COALESCE(be.size_bytes, 0) > 1))[1] AS duration_seconds,
                (ARRAY_AGG(be.throughput_bps ORDER BY be.started_at DESC)
                    FILTER (WHERE be.status = 'ok' AND COALESCE(be.size_bytes, 0) > 1))[1] AS throughput_bps,
                (ARRAY_AGG(be.reused_pct ORDER BY be.started_at DESC)
                    FILTER (WHERE be.status = 'ok' AND COALESCE(be.size_bytes, 0) > 1))[1] AS reused_pct,
                (ARRAY_AGG(be.full_backup ORDER BY be.started_at DESC)
                    FILTER (WHERE be.status = 'ok' AND COALESCE(be.size_bytes, 0) > 1))[1] AS full_backup
            FROM backup_events be
            JOIN vms v ON v.id = be.vm_id
            WHERE v.cluster_id = %s
              AND be.started_at >= %s
            GROUP BY be.vm_id, backup_date
            """,
            (BACKUP_DAY_OFFSET_HOURS, DEFAULT_TIMEZONE, cluster_id, start),
        )
        rows = list(cur.fetchall())
    day_status: dict[tuple[int, date], tuple[str, dict[str, Any]]] = {}
    for row in rows:
        status = (
            "ok"
            if row["has_ok"]
            else ("deleted" if row["has_deleted"] else ("failed" if row["has_failed"] else "none"))
        )
        details = {k: row[k] for k in ("size_bytes", "duration_seconds", "throughput_bps", "reused_pct", "full_backup")}
        day_status[(row["vm_id"], row["backup_date"])] = (status, details)

    with connect() as conn, conn.cursor() as cur:
        cur.execute("SELECT id FROM vms WHERE cluster_id = %s", (cluster_id,))
        vm_ids = [row["id"] for row in cur.fetchall()]

    # The day-axis is the operator's wall clock — "today" is today, not a
    # shifted day. The offset only affects how individual events are bucketed
    # (long-running overnight jobs whose last VMs finish in the morning still
    # land on the day they started), not which day the rightmost dot represents.
    from zoneinfo import ZoneInfo

    tz = ZoneInfo(DEFAULT_TIMEZONE)
    today = datetime.now(tz).date()

    result: dict[int, list[dict[str, Any]]] = {}
    for vm_id in vm_ids:
        spark = []
        for i in range(days):
            d = today - timedelta(days=days - 1 - i)
            status, details = day_status.get((vm_id, d), ("none", {}))
            spark.append({"date": d, "status": status, **details})
        result[vm_id] = spark
    return result


def restore_test_coverage() -> dict[str, Any]:
    rows = vm_governance_rows()
    required = sum(1 for row in rows if row["restore_required"] and row["status_backup_kind"] != "none")
    tested = sum(
        1
        for row in rows
        if row["restore_required"] and row["status_backup_kind"] != "none" and row["last_restore_tested_at"] is not None
    )
    overdue = sum(1 for row in rows if row["restore_due_status"] in RESTORE_DUE_STATUSES)
    ok = sum(
        1
        for row in rows
        if row["restore_required"] and row["status_backup_kind"] != "none" and row["restore_due_status"] == "ok"
    )
    pct = round(ok / required * 100, 1) if required > 0 else 100.0
    return {"required": required, "tested": tested, "ok": ok, "overdue": overdue, "pct": pct}
