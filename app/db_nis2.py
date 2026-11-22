"""Queries behind the NIS2 evidence report (see app/services/nis2_report.py)."""

from __future__ import annotations

from datetime import date, datetime
from typing import Any

from app.db_core import connect

__all__ = [
    "nis2_backup_counts",
    "nis2_backup_times",
    "nis2_earliest_event_by_cluster",
    "nis2_guest_rows",
    "nis2_restore_protocol",
    "nis2_restore_test_dates",
]


def nis2_guest_rows() -> list[dict[str, Any]]:
    """Every known guest with what the report needs: cluster, policy, lifetime.

    `backup_kind` is the effective kind; with no backup job and no override it
    is "unconfigured" (measured as daily — such guests are what an audit looks for).
    """
    with connect() as conn, conn.cursor() as cur:
        cur.execute(
            """
            SELECT
                v.id, v.cluster_id, c.name AS cluster_name, c.slug AS cluster_slug,
                v.vmid, v.name AS vm_name, v.node_name, v.guest_type,
                v.first_seen_at, v.missing_since,
                COALESCE(p.backup_policy_override, v.backup_kind) AS effective_kind,
                p.backup_policy_override,
                COALESCE(p.rpo_hours, 24) AS rpo_hours,
                COALESCE(p.restore_required, TRUE) AS restore_required,
                COALESCE(p.restore_cadence, 'half_year') AS restore_cadence
            FROM vms v
            JOIN clusters c ON c.id = v.cluster_id
            LEFT JOIN vm_policies p ON p.vm_id = v.id
            ORDER BY c.name, v.name, v.vmid
            """
        )
        rows = list(cur.fetchall())
    for row in rows:
        kind = row["effective_kind"]
        row["backup_kind"] = "unconfigured" if kind == "none" and not row["backup_policy_override"] else kind
    return rows


def nis2_backup_times(since: datetime) -> dict[int, list[datetime]]:
    """Start times of successful backups per guest from `since` on — plus each
    guest's latest one before it, which opens the first stretch. Pruned backups
    count: the backup existed at that time."""
    with connect() as conn, conn.cursor() as cur:
        cur.execute(
            """
            SELECT vm_id, started_at FROM backup_events
            WHERE status = 'ok' AND COALESCE(size_bytes, 0) > 1 AND started_at >= %s
            UNION
            SELECT vm_id, MAX(started_at) FROM backup_events
            WHERE status = 'ok' AND COALESCE(size_bytes, 0) > 1 AND started_at < %s
            GROUP BY vm_id
            """,
            (since, since),
        )
        out: dict[int, list[datetime]] = {}
        for row in cur.fetchall():
            out.setdefault(row["vm_id"], []).append(row["started_at"])
    for times in out.values():
        times.sort()
    return out


def nis2_backup_counts(start: datetime, end: datetime) -> dict[int, tuple[int, int]]:
    """(successful, failed) backups per guest started in [start, end)."""
    with connect() as conn, conn.cursor() as cur:
        cur.execute(
            """
            SELECT vm_id,
                   COUNT(*) FILTER (WHERE status = 'ok' AND COALESCE(size_bytes, 0) > 1) AS ok,
                   COUNT(*) FILTER (WHERE status = 'failed') AS failed
            FROM backup_events
            WHERE started_at >= %s AND started_at < %s
            GROUP BY vm_id
            """,
            (start, end),
        )
        return {r["vm_id"]: (r["ok"], r["failed"]) for r in cur.fetchall()}


def nis2_restore_test_dates() -> dict[int, list[date]]:
    """Dates of valid restore tests (passed/limited, automated or documented) per guest."""
    with connect() as conn, conn.cursor() as cur:
        cur.execute("SELECT vm_id, tested_at FROM restore_tests WHERE result IN ('passed', 'limited')")
        out: dict[int, list[date]] = {}
        for row in cur.fetchall():
            out.setdefault(row["vm_id"], []).append(row["tested_at"])
    return out


def nis2_restore_protocol(start: datetime, end: datetime, start_date: date, end_date: date) -> list[dict[str, Any]]:
    """Every restore attempt finished in the period: all automated runs (any
    result — failures stay on record), plus documented tests without a run
    (`tested_at` in [start_date, end_date))."""
    with connect() as conn, conn.cursor() as cur:
        cur.execute(
            """
            SELECT 'run' AS kind, r.id, r.vm_id, r.trigger, r.result, r.finished_at AS at,
                   r.started_at, r.duration_seconds, r.source_pbs, r.source_datastore,
                   r.source_namespace, r.snapshot_id, r.checks, r.log,
                   NULL::text AS recovery_type, NULL::text AS evidence_note
            FROM restore_runs r
            WHERE r.finished_at >= %s AND r.finished_at < %s AND r.result IS NOT NULL
            UNION ALL
            SELECT 'manual' AS kind, t.id, t.vm_id, 'manual' AS trigger, t.result,
                   t.created_at AS at, NULL, t.duration_minutes * 60, NULL, NULL, NULL, NULL,
                   NULL, NULL, t.recovery_type, t.evidence_note
            FROM restore_tests t
            WHERE t.tested_at >= %s AND t.tested_at < %s
              AND NOT EXISTS (SELECT 1 FROM restore_runs o WHERE o.restore_test_id = t.id)
            ORDER BY at
            """,
            (start, end, start_date, end_date),
        )
        return list(cur.fetchall())


def nis2_earliest_event_by_cluster() -> dict[int, datetime]:
    """When Backup Sentinel's backup history of each cluster begins."""
    with connect() as conn, conn.cursor() as cur:
        cur.execute(
            """
            SELECT v.cluster_id, MIN(be.started_at) AS first
            FROM backup_events be JOIN vms v ON v.id = be.vm_id
            GROUP BY v.cluster_id
            """
        )
        return {r["cluster_id"]: r["first"] for r in cur.fetchall()}
