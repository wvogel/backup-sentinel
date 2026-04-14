from __future__ import annotations

from datetime import UTC, datetime

from app.db_core import connect
from app.types import NotificationLogRow


def try_claim_dedup(dedup_key: str, ttl_seconds: int = 86400) -> bool:
    """Atomically claim a dedup key. Returns True if the caller should send
    the notification (key was not claimed in last ttl_seconds), False otherwise.
    Uses INSERT...ON CONFLICT for atomicity across restarts and threads."""
    with connect() as conn, conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO notification_dedup (dedup_key, sent_at)
            VALUES (%s, now())
            ON CONFLICT (dedup_key) DO UPDATE
              SET sent_at = EXCLUDED.sent_at
              WHERE notification_dedup.sent_at < now() - make_interval(secs => %s)
            RETURNING dedup_key
            """,
            (dedup_key, ttl_seconds),
        )
        row = cur.fetchone()
        claimed = row is not None
        if claimed:
            cur.execute(
                "DELETE FROM notification_dedup WHERE sent_at < now() - make_interval(secs => %s)",
                (ttl_seconds * 7,),
            )
        conn.commit()
        return claimed


def insert_notification_log(
    notification_type: str,
    title: str,
    message: str,
    channel: str,
    cluster_name: str | None = None,
) -> None:
    with connect() as conn, conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO notification_log (notification_type, title, message, cluster_name, channel, created_at)
            VALUES (%s, %s, %s, %s, %s, %s)
            """,
            (notification_type, title, message, cluster_name, channel, datetime.now(UTC)),
        )
        cur.execute("DELETE FROM notification_log WHERE created_at < now() - interval '90 days'")
        conn.commit()


def list_notification_logs(
    days: int = 30,
    limit: int = 50,
    offset: int = 0,
    notification_type: str | None = None,
    cluster_name: str | None = None,
    channel: str | None = None,
) -> tuple[list[NotificationLogRow], int]:
    where_clauses = ["created_at >= now() - make_interval(days => %s)"]
    params: list[object] = [days]
    if notification_type:
        where_clauses.append("notification_type = %s")
        params.append(notification_type)
    if cluster_name:
        where_clauses.append("cluster_name = %s")
        params.append(cluster_name)
    if channel:
        where_clauses.append("channel = %s")
        params.append(channel)
    where_sql = " AND ".join(where_clauses)

    with connect() as conn, conn.cursor() as cur:
        cur.execute(
            f"SELECT count(*) AS cnt FROM notification_log WHERE {where_sql}",
            tuple(params),
        )
        total = cur.fetchone()["cnt"]
        cur.execute(
            f"""
            SELECT id, notification_type, title, message, cluster_name, channel, created_at
            FROM notification_log
            WHERE {where_sql}
            ORDER BY created_at DESC
            LIMIT %s OFFSET %s
            """,
            (*tuple(params), limit, offset),
        )
        rows = [NotificationLogRow(**row) for row in cur.fetchall()]
        return rows, total


def list_distinct_filters(days: int = 30) -> dict[str, list[str]]:
    """Return distinct values for filter dropdowns."""
    with connect() as conn, conn.cursor() as cur:
        cur.execute(
            """SELECT DISTINCT notification_type FROM notification_log
               WHERE created_at >= now() - make_interval(days => %s)
               ORDER BY notification_type""",
            (days,),
        )
        types = [r["notification_type"] for r in cur.fetchall()]
        cur.execute(
            """SELECT DISTINCT cluster_name FROM notification_log
               WHERE created_at >= now() - make_interval(days => %s)
                 AND cluster_name IS NOT NULL
               ORDER BY cluster_name""",
            (days,),
        )
        clusters = [r["cluster_name"] for r in cur.fetchall()]
        cur.execute(
            """SELECT DISTINCT channel FROM notification_log
               WHERE created_at >= now() - make_interval(days => %s)
               ORDER BY channel""",
            (days,),
        )
        channels = [r["channel"] for r in cur.fetchall()]
        return {"types": types, "clusters": clusters, "channels": channels}


def list_notification_logs_for_period(period_start: datetime, period_end: datetime) -> list[NotificationLogRow]:
    with connect() as conn, conn.cursor() as cur:
        cur.execute(
            """
            SELECT id, notification_type, title, message, cluster_name, channel, created_at
            FROM notification_log
            WHERE created_at >= %s AND created_at < %s
            ORDER BY created_at DESC
            """,
            (period_start, period_end),
        )
        return [NotificationLogRow(**row) for row in cur.fetchall()]
