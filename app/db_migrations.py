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
        # PVE ctime and PBS backup-time can differ by 30+ minutes.
        # PVE-via-PBS reports inflated chunk-reference size (~10x real).
        # Any PBS row whose size is >5x another for the same VM within
        # 2 hours is the inflated duplicate.  Also clear stale removed_at.
        [
            """
            DELETE FROM backup_events
            WHERE id IN (
                SELECT a.id
                FROM backup_events a
                JOIN backup_events b ON a.vm_id = b.vm_id
                    AND a.id <> b.id
                    AND a.source = 'pbs' AND b.source = 'pbs'
                    AND a.size_bytes > b.size_bytes * 5
                    AND ABS(EXTRACT(EPOCH FROM a.started_at - b.started_at)) <= 7200
            )
            """,
            "UPDATE backup_events SET removed_at = NULL WHERE source = 'pbs' AND removed_at IS NOT NULL",
        ],
    ),
]


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


def run_migrations() -> None:
    """Apply all pending migrations in ascending version order.

    Called at startup from `init_db()`. Idempotent — already-applied
    migrations are skipped via the `schema_migrations` ledger.
    """
    with connect() as conn, conn.cursor() as cur:
        _ensure_migrations_table(cur)
        conn.commit()
        applied = _applied_versions(cur)

    pending = sorted((v, d, s) for v, d, s in MIGRATIONS if v not in applied)
    if not pending:
        return

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
