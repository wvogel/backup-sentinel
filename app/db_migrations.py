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
    # (version, description, [sql_statements])
    # Example (leave commented until you add a real one):
    # (1, "drop legacy notes column", ["ALTER TABLE clusters DROP COLUMN IF EXISTS notes_legacy"]),
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
