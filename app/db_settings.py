from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from app.db_core import connect
from app.types import AuditLogRow


def get_setting(key: str, default: str = "") -> str:
    with connect() as conn, conn.cursor() as cur:
        cur.execute("SELECT value FROM app_settings WHERE key = %s", (key,))
        row = cur.fetchone()
        return row["value"] if row else default


def get_settings_dict(prefix: str = "") -> dict[str, str]:
    with connect() as conn, conn.cursor() as cur:
        if prefix:
            cur.execute("SELECT key, value FROM app_settings WHERE key LIKE %s", (prefix + "%",))
        else:
            cur.execute("SELECT key, value FROM app_settings")
        return {row["key"]: row["value"] for row in cur.fetchall()}


def save_settings(settings: dict[str, str]) -> None:
    with connect() as conn, conn.cursor() as cur:
        for key, value in settings.items():
            cur.execute(
                """
                INSERT INTO app_settings (key, value) VALUES (%s, %s)
                ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value
                """,
                (key, value),
            )
        conn.commit()


def append_settings_audit_entries(entries: list[dict[str, str]]) -> None:
    if not entries:
        return
    now = datetime.now(UTC)
    with connect() as conn, conn.cursor() as cur:
        for entry in entries:
            cur.execute(
                """
                INSERT INTO settings_audit_log (actor, key, old_value, new_value, created_at)
                VALUES (%s, %s, %s, %s, %s)
                """,
                (entry["actor"], entry["key"], entry["old_value"], entry["new_value"], now),
            )
        conn.commit()


def list_settings_audit_entries(limit: int = 20) -> list[AuditLogRow]:
    with connect() as conn, conn.cursor() as cur:
        cur.execute(
            """
            SELECT actor, key, old_value, new_value, created_at
            FROM settings_audit_log
            ORDER BY created_at DESC, id DESC
            LIMIT %s
            """,
            (limit,),
        )
        return list(cur.fetchall())
