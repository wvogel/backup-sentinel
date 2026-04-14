from __future__ import annotations

from datetime import UTC, datetime

from app.db_core import connect
from app.types import NotificationLogRow


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


def list_notification_logs(days: int = 30, limit: int = 50, offset: int = 0) -> tuple[list[NotificationLogRow], int]:
    with connect() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT count(*) AS cnt FROM notification_log WHERE created_at >= now() - make_interval(days => %s)",
            (days,),
        )
        total = cur.fetchone()["cnt"]
        cur.execute(
            """
            SELECT id, notification_type, title, message, cluster_name, channel, created_at
            FROM notification_log
            WHERE created_at >= now() - make_interval(days => %s)
            ORDER BY created_at DESC
            LIMIT %s OFFSET %s
            """,
            (days, limit, offset),
        )
        rows = [NotificationLogRow(**row) for row in cur.fetchall()]
        return rows, total


def list_notification_logs_for_period(
    period_start: datetime, period_end: datetime
) -> list[NotificationLogRow]:
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
