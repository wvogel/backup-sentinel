from __future__ import annotations

import json
from datetime import UTC, datetime

from app.db_core import connect
from app.services.crypto import keyed_digest
from app.types import AuditLogRow


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


# Audit log: append-only (a database trigger refuses UPDATE/DELETE/TRUNCATE)
# and chained — every entry's hash covers its content and the hash before it,
# keyed with BSENTINEL_SECRET_KEY. Changing or removing an entry in the
# database breaks the chain from there on; verify_audit_log() finds the spot.
_AUDIT_LOCK = 0x42534155  # advisory lock: one writer extends the chain at a time


def _audit_hash(previous: str, actor: str, key: str, old_value: str, new_value: str, created_at: datetime) -> str:
    stamp = created_at.astimezone(UTC).isoformat(timespec="microseconds")
    message = json.dumps([previous, actor, key, old_value, new_value, stamp], ensure_ascii=False)
    return keyed_digest("audit-log", message)


def _append_audit_rows(rows: list[tuple[str, str, str, str]]) -> None:
    """Append (actor, key, old_value, new_value) rows, extending the hash chain."""
    if not rows:
        return
    now = datetime.now(UTC)
    with connect() as conn, conn.cursor() as cur:
        cur.execute("SELECT pg_advisory_xact_lock(%s)", (_AUDIT_LOCK,))
        cur.execute("SELECT entry_hash FROM settings_audit_log ORDER BY id DESC LIMIT 1")
        last = cur.fetchone()
        previous = (last or {}).get("entry_hash") or ""
        for actor, key, old_value, new_value in rows:
            previous = _audit_hash(previous, actor, key, old_value, new_value, now)
            cur.execute(
                """
                INSERT INTO settings_audit_log (actor, key, old_value, new_value, created_at, entry_hash)
                VALUES (%s, %s, %s, %s, %s, %s)
                """,
                (actor, key, old_value, new_value, now, previous),
            )
        conn.commit()


def append_settings_audit_entries(entries: list[dict[str, str]]) -> None:
    _append_audit_rows([(e["actor"], e["key"], e["old_value"], e["new_value"]) for e in entries])


def append_audit_event(actor: str, event: str, target: str = "", details: str = "") -> None:
    """Append a generic audit event (cluster/PBS/report/restore-test lifecycle).

    Uses the same settings_audit_log table for simplicity. Events are
    distinguished by their `key` starting with a namespace like
    'cluster.', 'pbs.', 'report.' or 'restore_test.' — whereas setting
    changes use plain key names like 'notify_gotify_enabled'.
    """
    _append_audit_rows([(actor, event, target, details)])


def seal_audit_log() -> int:
    """Give entries from before the chain their hash, once. Returns how many."""
    sealed = 0
    with connect() as conn, conn.cursor() as cur:
        cur.execute("SELECT pg_advisory_xact_lock(%s)", (_AUDIT_LOCK,))
        cur.execute("SELECT 1 FROM settings_audit_log WHERE entry_hash IS NULL LIMIT 1")
        if not cur.fetchone():
            return 0
        cur.execute(
            "SELECT id, actor, key, old_value, new_value, created_at, entry_hash FROM settings_audit_log ORDER BY id"
        )
        previous = ""
        for row in cur.fetchall():
            if row["entry_hash"] is None:
                digest = _audit_hash(
                    previous, row["actor"], row["key"], row["old_value"], row["new_value"], row["created_at"]
                )
                cur.execute("UPDATE settings_audit_log SET entry_hash = %s WHERE id = %s", (digest, row["id"]))
                sealed += 1
                previous = digest
            else:
                previous = row["entry_hash"]
        conn.commit()
    return sealed


def verify_audit_log() -> dict[str, object]:
    """Recompute the chain: {"ok", "entries", "broken_at" (id of the first entry
    that does not match, else None), "head" (hash of the newest entry)}."""
    with connect() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT id, actor, key, old_value, new_value, created_at, entry_hash FROM settings_audit_log ORDER BY id"
        )
        rows = cur.fetchall()
    previous = ""
    for row in rows:
        expected = _audit_hash(
            previous, row["actor"], row["key"], row["old_value"], row["new_value"], row["created_at"]
        )
        if row["entry_hash"] != expected:
            return {"ok": False, "entries": len(rows), "broken_at": row["id"], "head": previous}
        previous = expected
    return {"ok": True, "entries": len(rows), "broken_at": None, "head": previous}


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
