"""Snapshots on PBS datastores and what their backup tasks uploaded
(app.services.pbs_space)."""

from __future__ import annotations

from typing import Any

from app.db_core import connect

_KEY = ("store", "ns", "backup_type", "backup_id", "backup_time")


def replace_pbs_snapshots(pbs_id: int, listed: dict[tuple[str, str], list[dict[str, Any]]]) -> None:
    """The snapshots of each (datastore, namespace) just listed in full:
    new ones are added, those no longer there (pruned) go."""
    with connect() as conn, conn.cursor() as cur:
        for (store, ns), snapshots in listed.items():
            cur.execute("DELETE FROM pbs_snapshots WHERE pbs_id = %s AND store = %s AND ns = %s", (pbs_id, store, ns))
            cur.executemany(
                """
                INSERT INTO pbs_snapshots (pbs_id, store, ns, backup_type, backup_id, backup_time, guest_name)
                VALUES (%s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT DO NOTHING
                """,
                [
                    (pbs_id, store, ns, s["backup_type"], s["backup_id"], s["backup_time"], s.get("guest_name"))
                    for s in snapshots
                ],
            )
        conn.commit()


def list_pbs_snapshots(pbs_id: int, store: str, ns: str | None = None) -> list[dict[str, Any]]:
    """Snapshots of a datastore (of one namespace when given) with what their
    backup task stored, where a task log told (else None)."""
    query = """
        SELECT s.ns, s.backup_type, s.backup_id, s.backup_time, s.guest_name, t.stored_bytes, t.upload_bytes
        FROM pbs_snapshots s
        LEFT JOIN pbs_backup_tasks t
               ON t.pbs_id = s.pbs_id AND t.store = s.store AND t.ns = s.ns AND t.backup_type = s.backup_type
              AND t.backup_id = s.backup_id AND t.backup_time = s.backup_time
        WHERE s.pbs_id = %s AND s.store = %s
    """
    params: list[Any] = [pbs_id, store]
    if ns is not None:
        query += " AND s.ns = %s"
        params.append(ns)
    with connect() as conn, conn.cursor() as cur:
        cur.execute(query, params)
        return list(cur.fetchall())


def pbs_snapshots_without_stats(pbs_id: int) -> list[dict[str, Any]]:
    with connect() as conn, conn.cursor() as cur:
        cur.execute(
            """
            SELECT s.store, s.backup_type, s.backup_id, s.backup_time
            FROM pbs_snapshots s
            WHERE s.pbs_id = %s AND NOT EXISTS (
                SELECT 1 FROM pbs_backup_tasks t
                WHERE t.pbs_id = s.pbs_id AND t.store = s.store AND t.ns = s.ns AND t.backup_type = s.backup_type
                  AND t.backup_id = s.backup_id AND t.backup_time = s.backup_time
            )
            """,
            (pbs_id,),
        )
        return list(cur.fetchall())


def pbs_backup_tasks_read(pbs_id: int) -> set[str]:
    """UPIDs of the backup tasks whose log was read already."""
    with connect() as conn, conn.cursor() as cur:
        cur.execute("SELECT upid FROM pbs_backup_tasks WHERE pbs_id = %s", (pbs_id,))
        return {row["upid"] for row in cur.fetchall()}


def save_pbs_backup_task(pbs_id: int, upid: str, started_at: int, stats: dict[str, Any] | None) -> None:
    """A backup task's log was read: what it told (nothing when it named no
    snapshot — remembered all the same, so it is not read again)."""
    stats = stats or {}
    with connect() as conn, conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO pbs_backup_tasks
                (pbs_id, upid, started_at, store, ns, backup_type, backup_id, backup_time, upload_bytes, stored_bytes)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT DO NOTHING
            """,
            (
                pbs_id,
                upid,
                started_at,
                *(stats.get(key) for key in _KEY),
                stats.get("upload_bytes"),
                stats.get("stored_bytes"),
            ),
        )
        conn.commit()


def forget_pbs_backup_tasks(pbs_id: int) -> None:
    """Drop what tasks told that are older than every snapshot still there."""
    with connect() as conn, conn.cursor() as cur:
        cur.execute(
            """
            DELETE FROM pbs_backup_tasks
            WHERE pbs_id = %s AND started_at < (
                SELECT COALESCE(MIN(backup_time), 0) - 86400 FROM pbs_snapshots WHERE pbs_id = %s
            )
            """,
            (pbs_id, pbs_id),
        )
        conn.commit()
