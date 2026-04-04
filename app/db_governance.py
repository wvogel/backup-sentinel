from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from typing import Any

from app.db_core import connect
from app.services.backup_status import assess_backup_status
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
    """
    params: list[Any] = []
    if cluster_id is not None:
        query += " WHERE v.cluster_id = %s"
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
        row["status_backup_kind"] = status_backup_kind
        severity, age_days = assess_backup_status(status_backup_kind, row["last_backup_at"], now)
        row["backup_severity"] = severity
        row["backup_age_days"] = age_days
        if status_backup_kind == "unconfigured":
            row["backup_kind_label"] = "auto"
        else:
            row["backup_kind_label"] = {
                "daily": "täglich",
                "weekly": "wöchentlich",
                "none": "nie",
            }.get(effective_backup_kind, effective_backup_kind)

        if row["restore_required"] and status_backup_kind != "none":
            default_interval = 365 if status_backup_kind == "weekly" else 90
            interval_days = row["restore_interval_days"] or default_interval
            row["restore_interval_days"] = interval_days
            if row["last_restore_tested_at"] is None:
                row["restore_due_status"] = "critical"
                row["restore_age_days"] = None
            else:
                restore_age = (now.date() - row["last_restore_tested_at"]).days
                row["restore_age_days"] = restore_age
                if restore_age >= interval_days:
                    row["restore_due_status"] = "critical"
                elif restore_age >= max(1, interval_days - 30):
                    row["restore_due_status"] = "warning"
                else:
                    row["restore_due_status"] = "ok"
        else:
            row["restore_due_status"] = "ok"
            row["restore_age_days"] = None
    return rows


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


def vm_backup_sparkline_data(cluster_id: int, days: int = 31) -> dict[int, list[str]]:
    start = datetime.now(UTC) - timedelta(days=days - 1)
    with connect() as conn, conn.cursor() as cur:
        cur.execute(
            """
            SELECT
                be.vm_id,
                DATE(be.started_at) AS backup_date,
                BOOL_OR(be.status = 'ok' AND be.removed_at IS NULL AND COALESCE(be.size_bytes, 0) > 1) AS has_ok,
                BOOL_OR(be.status = 'ok' AND be.removed_at IS NOT NULL AND COALESCE(be.size_bytes, 0) > 1) AS has_deleted,
                BOOL_OR(be.status != 'ok') AS has_failed
            FROM backup_events be
            JOIN vms v ON v.id = be.vm_id
            WHERE v.cluster_id = %s
              AND be.started_at >= %s
            GROUP BY be.vm_id, DATE(be.started_at)
            """,
            (cluster_id, start),
        )
        rows = list(cur.fetchall())
    day_status: dict[tuple[int, date], str] = {}
    for row in rows:
        status = (
            "ok"
            if row["has_ok"]
            else ("deleted" if row["has_deleted"] else ("failed" if row["has_failed"] else "none"))
        )
        day_status[(row["vm_id"], row["backup_date"])] = status

    with connect() as conn, conn.cursor() as cur:
        cur.execute("SELECT id FROM vms WHERE cluster_id = %s", (cluster_id,))
        vm_ids = [row["id"] for row in cur.fetchall()]

    result: dict[int, list[str]] = {}
    for vm_id in vm_ids:
        spark = []
        for i in range(days):
            d = (start + timedelta(days=i)).date()
            spark.append(day_status.get((vm_id, d), "none"))
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
    overdue = sum(1 for row in rows if row["restore_due_status"] in {"warning", "critical"})
    ok = sum(
        1
        for row in rows
        if row["restore_required"] and row["status_backup_kind"] != "none" and row["restore_due_status"] == "ok"
    )
    pct = round(ok / required * 100, 1) if required > 0 else 100.0
    return {"required": required, "tested": tested, "ok": ok, "overdue": overdue, "pct": pct}
