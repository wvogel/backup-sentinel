"""Lightweight DB migrations.

The base schema is created by `init_db()` in `db_core.py` using
`CREATE TABLE IF NOT EXISTS` — sufficient for additive changes. This
module handles non-idempotent migrations (DROP COLUMN, data migrations,
schema refactors) via a numbered ledger.

Adding a migration:

    1. Append an entry to MIGRATIONS with a NEW, never-reused version number
       and one or more SQL statements (as a list of strings).
    2. Never edit existing entries.
    3. Migrations are applied in ascending version order on every startup.
    4. Each migration runs in its own transaction — if it fails, the
       schema_migrations row is NOT written, and the next restart retries.

Example:

    MIGRATIONS: list[tuple[int, str, list[str]]] = [
        (1, "drop legacy notes column", ["ALTER TABLE clusters DROP COLUMN IF EXISTS notes_legacy"]),
    ]
"""

from __future__ import annotations

import logging
from typing import Any

from app.db_core import connect

logger = logging.getLogger(__name__)


MIGRATIONS: list[tuple[int, str, list[str]]] = [
    (
        1,
        "Re-classify failed-but-large backup_events as ok",
        # An earlier ON CONFLICT rule preserved status='failed' even when a
        # later sync reported a real size > 1 byte — leaving genuine
        # successful backups stuck as 'failed'. Reverse that for any row
        # where the size proves the backup completed.
        ["UPDATE backup_events SET status = 'ok' WHERE status = 'failed' AND COALESCE(size_bytes, 0) > 1"],
    ),
    (
        2,
        "Re-read vzdump logs: TPM state bitmap no longer marks a full run",
        # The always-new bitmap of tpmstate0-backup flagged nearly every backup of
        # VMs with a TPM as a full run. Forget the flags and the processed tasks so
        # the next sync parses the logs again (as far as PVE still keeps them).
        ["UPDATE backup_events SET full_backup = NULL WHERE full_backup", "DELETE FROM processed_backup_tasks"],
    ),
    (
        3,
        "Mark duplicate PBS backup events caused by PVE-proxy double-reporting",
        # Superseded by migration 4 (DELETE instead of removed_at).
        # Kept as a no-op so the version number is not reused.
        [],
    ),
    (
        4,
        "Delete duplicate PBS backup events from PVE-proxy double-reporting",
        # Superseded by migration 5.
        [],
    ),
    (
        5,
        "Delete duplicate PBS events, keep the row with the smaller size",
        # PVE-via-PBS reports the total referenced chunk size (inflated);
        # direct-PBS reports the actual snapshot size (correct, smaller).
        # PVE events are inserted first (lower id), so the earlier migrations
        # that kept the lower id kept the WRONG row.  Now: keep the smaller
        # size; if equal, keep the lower id.  Window widened to 120 s
        # (minute-rounding can produce up to 119 s difference).
        # Superseded by migration 6.
        [],
    ),
    (
        6,
        "Remove inflated PVE-via-PBS duplicate backup events",
        # Superseded by migration 7 (fetch_origin column approach).
        [],
    ),
    (
        7,
        "Add fetch_origin column; delete PVE-via-PBS duplicates by origin",
        [
            "ALTER TABLE backup_events ADD COLUMN IF NOT EXISTS fetch_origin TEXT",
            "UPDATE backup_events SET fetch_origin = 'pbs' WHERE source = 'pbs' AND fetch_origin IS NULL",
            """
            UPDATE backup_events SET fetch_origin = 'pve'
            WHERE source = 'pbs'
              AND removed_at IS NOT NULL
              AND EXISTS (
                  SELECT 1 FROM backup_events b
                  WHERE b.vm_id = backup_events.vm_id
                    AND b.id <> backup_events.id
                    AND b.source = 'pbs'
                    AND b.removed_at IS NULL
                    AND ABS(EXTRACT(EPOCH FROM b.started_at - backup_events.started_at)) <= 7200
              )
            """,
            "DELETE FROM backup_events WHERE fetch_origin = 'pve'",
            "UPDATE backup_events SET removed_at = NULL WHERE source = 'pbs' AND removed_at IS NOT NULL",
        ],
    ),
    (
        8,
        "Recompute restore-run durations shortened by a resume after an app restart",
        # Before v3.5.6 a run resumed after a deploy measured its duration from
        # the resume, not from started_at. Recompute from finished_at - started_at
        # — but ONLY for runs that were provably never held: a held run's
        # finished_at is overwritten when it is later promoted or reaped, so
        # its timestamps span the hold window. Only failed/error runs can be
        # held; a passed one only via promotion, which its restore test's
        # evidence note records. Only lengthen, never shorten.
        [
            """
            UPDATE restore_runs r
            SET duration_seconds = EXTRACT(EPOCH FROM r.finished_at - r.started_at)::int
            WHERE r.started_at IS NOT NULL
              AND r.finished_at IS NOT NULL
              AND r.result IN ('passed', 'limited', 'cancelled')
              AND r.status = r.result
              AND EXTRACT(EPOCH FROM r.finished_at - r.started_at) > COALESCE(r.duration_seconds, 0) + 60
              AND NOT EXISTS (
                  SELECT 1 FROM restore_tests t
                  WHERE t.id = r.restore_test_id AND t.evidence_note LIKE 'promoted%'
              )
            """,
            """
            UPDATE restore_tests t
            SET duration_minutes = ROUND(r.duration_seconds / 60.0)::int
            FROM restore_runs r
            WHERE r.restore_test_id = t.id
              AND t.recovery_type = 'auto-full-vm'
              AND t.evidence_note NOT LIKE 'promoted%'
              AND r.duration_seconds IS NOT NULL
              AND t.duration_minutes IS DISTINCT FROM ROUND(r.duration_seconds / 60.0)::int
            """,
        ],
    ),
    (
        9,
        "Re-link restore runs the stuck-run watchdog overwrote right after they finished",
        # Before v3.12.2 the watchdog could mark a run "stuck" in the seconds
        # between the worker's teardown and its result write: the run read
        # "error" and its passed/limited restore test lost its link (it then
        # looked manually documented). The worker's test row is still there,
        # written moments before the watchdog's finished_at — restore the run's
        # real outcome from it. (Its probe checks were overwritten and stay lost.)
        [
            """
            UPDATE restore_runs r
            SET status = t.result, result = t.result, restore_test_id = t.id, checks = NULL,
                finished_at = t.created_at,
                duration_seconds = GREATEST(0, EXTRACT(EPOCH FROM t.created_at - r.started_at)::int)
            FROM restore_tests t
            WHERE r.result = 'error'
              AND r.restore_test_id IS NULL
              AND r.started_at IS NOT NULL
              AND r.checks->>'error' LIKE 'recovered: run was stuck%'
              AND t.vm_id = r.vm_id
              AND t.recovery_type = 'auto-full-vm'
              AND t.result IN ('passed', 'limited')
              AND t.created_at BETWEEN r.finished_at - INTERVAL '5 minutes' AND r.finished_at
              AND t.created_at > r.started_at
              AND NOT EXISTS (SELECT 1 FROM restore_runs o WHERE o.restore_test_id = t.id)
            """,
        ],
    ),
    # 10, 11: one-off data fixes, applied and removed. Numbers taken — never reuse.
    (
        12,
        "NIS2 report: when a guest was first seen; per-scope report files",
        # first_seen_at: a guest never backed up (or backed up late) counts as
        # uncovered from when it was first seen. Existing guests get their
        # earliest backup, or the migration time.
        [
            "ALTER TABLE vms ADD COLUMN IF NOT EXISTS first_seen_at TIMESTAMPTZ",
            """
            UPDATE vms SET first_seen_at = COALESCE(
                (SELECT MIN(be.started_at) FROM backup_events be WHERE be.vm_id = vms.id), now()
            )
            WHERE first_seen_at IS NULL
            """,
            "ALTER TABLE vms ALTER COLUMN first_seen_at SET DEFAULT now()",
            "ALTER TABLE vms ALTER COLUMN first_seen_at SET NOT NULL",
            "ALTER TABLE monthly_reports ADD COLUMN IF NOT EXISTS files JSONB",
        ],
    ),
    (
        13,
        "Archived reports: layout version they were rendered with",
        # Every report archived so far has the old layout (signature lines):
        # version 1. report_archive rebuilds older versions once.
        ["ALTER TABLE monthly_reports ADD COLUMN IF NOT EXISTS format INT NOT NULL DEFAULT 1"],
    ),
    (
        14,
        "NIS2 report: per-cluster start of the evidence",
        # First month the NIS2 report measures a cluster in, set by the user (e.g.
        # its VMs moved in later, with older snapshots). NULL: since it was added.
        ["ALTER TABLE clusters ADD COLUMN IF NOT EXISTS report_since DATE"],
    ),
    (
        15,
        "Evidence hardening: report hashes, append-only audit log, expiring enrollment",
        [
            # SHA-256 of every archived file ("all" / cluster slug / "json").
            "ALTER TABLE monthly_reports ADD COLUMN IF NOT EXISTS sha256 JSONB",
            # NULL = expired: secrets issued before this need renewing in the settings.
            "ALTER TABLE clusters ADD COLUMN IF NOT EXISTS enrollment_expires_at TIMESTAMPTZ",
            # Keyed hash chain over the audit log (app.db_settings); NULL only on
            # entries from before the chain, filled once at start.
            "ALTER TABLE settings_audit_log ADD COLUMN IF NOT EXISTS entry_hash TEXT",
            """
            CREATE OR REPLACE FUNCTION settings_audit_log_append_only() RETURNS trigger AS $$
            BEGIN
                IF TG_OP = 'UPDATE' AND OLD.entry_hash IS NULL AND NEW.entry_hash IS NOT NULL
                   AND ROW(NEW.id, NEW.actor, NEW.key, NEW.old_value, NEW.new_value, NEW.created_at)
                       IS NOT DISTINCT FROM
                       ROW(OLD.id, OLD.actor, OLD.key, OLD.old_value, OLD.new_value, OLD.created_at) THEN
                    RETURN NEW;
                END IF;
                RAISE EXCEPTION 'settings_audit_log is append-only';
            END
            $$ LANGUAGE plpgsql
            """,
            "DROP TRIGGER IF EXISTS settings_audit_log_no_change ON settings_audit_log",
            """
            CREATE TRIGGER settings_audit_log_no_change
            BEFORE UPDATE OR DELETE ON settings_audit_log
            FOR EACH ROW EXECUTE FUNCTION settings_audit_log_append_only()
            """,
            "DROP TRIGGER IF EXISTS settings_audit_log_no_truncate ON settings_audit_log",
            """
            CREATE TRIGGER settings_audit_log_no_truncate
            BEFORE TRUNCATE ON settings_audit_log
            FOR EACH STATEMENT EXECUTE FUNCTION settings_audit_log_append_only()
            """,
        ],
    ),
    (
        16,
        "Pinned TLS certificates of PVE/PBS hosts",
        [
            # One pin per host and port (app.services.tls_trust). `source` says where
            # the pin comes from: bootstrap / first-use / accepted. seen_*: another
            # certificate the host presented since, waiting for an admin's decision.
            """
            CREATE TABLE IF NOT EXISTS tls_pins (
                host TEXT NOT NULL,
                port INTEGER NOT NULL,
                fingerprint TEXT NOT NULL,
                source TEXT NOT NULL,
                pinned_at TIMESTAMPTZ NOT NULL,
                seen_fingerprint TEXT,
                seen_at TIMESTAMPTZ,
                PRIMARY KEY (host, port)
            )
            """,
        ],
    ),
]


AUDIT_CHAIN_MIGRATION = 15  # the migration that added settings_audit_log.entry_hash


def _ensure_migrations_table(cur: Any) -> None:
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS schema_migrations (
            version INT PRIMARY KEY,
            description TEXT NOT NULL,
            applied_at TIMESTAMPTZ NOT NULL DEFAULT now()
        )
        """
    )


def _applied_versions(cur: Any) -> set[int]:
    cur.execute("SELECT version FROM schema_migrations")
    return {row["version"] for row in cur.fetchall()}


def run_migrations() -> list[int]:
    """Apply all pending migrations in ascending version order.

    Called at startup from `init_db()`. Idempotent — already-applied
    migrations are skipped via the `schema_migrations` ledger. Returns the
    versions applied by this call.
    """
    with connect() as conn, conn.cursor() as cur:
        _ensure_migrations_table(cur)
        conn.commit()
        applied = _applied_versions(cur)

    pending = sorted((v, d, s) for v, d, s in MIGRATIONS if v not in applied)
    if not pending:
        return []

    for version, description, statements in pending:
        logger.info("Applying migration %d: %s", version, description)
        with connect() as conn, conn.cursor() as cur:
            try:
                for stmt in statements:
                    cur.execute(stmt)
                cur.execute(
                    "INSERT INTO schema_migrations (version, description) VALUES (%s, %s)",
                    (version, description),
                )
                conn.commit()
                logger.info("Migration %d applied", version)
            except Exception as exc:
                conn.rollback()
                logger.error("Migration %d failed: %s", version, exc)
                raise
    return [version for version, _description, _statements in pending]
