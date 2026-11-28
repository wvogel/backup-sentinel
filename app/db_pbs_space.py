"""Snapshots on PBS datastores and the chunks they consist of
(app.services.pbs_space)."""

from __future__ import annotations

from typing import Any

from app.db_core import connect

Group = tuple[str, str, str, str]  # store, namespace, backup type, backup id

_SAME_GROUP = (
    "pbs_id = s.pbs_id AND store = s.store AND ns = s.ns AND backup_type = s.backup_type AND backup_id = s.backup_id"
)


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


def pbs_indexed_snapshots(pbs_id: int) -> set[tuple[str, str, str, str, int]]:
    """The snapshots whose index files were read already."""
    with connect() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT store, ns, backup_type, backup_id, backup_time FROM pbs_snapshot_chunks WHERE pbs_id = %s",
            (pbs_id,),
        )
        return {(r["store"], r["ns"], r["backup_type"], r["backup_id"], r["backup_time"]) for r in cur.fetchall()}


def forget_pbs_chunks(pbs_id: int) -> set[Group]:
    """Drop what is kept of snapshots that are gone (pruned). Returns the
    guests that lost one: their chunks have to be counted again."""
    with connect() as conn, conn.cursor() as cur:
        cur.execute(
            f"""
            DELETE FROM pbs_snapshot_chunks s
            WHERE s.pbs_id = %s AND NOT EXISTS (
                SELECT 1 FROM pbs_snapshots WHERE {_SAME_GROUP} AND backup_time = s.backup_time
            )
            RETURNING store, ns, backup_type, backup_id
            """,
            (pbs_id,),
        )
        lost = {(r["store"], r["ns"], r["backup_type"], r["backup_id"]) for r in cur.fetchall()}
        cur.execute(
            f"""
            DELETE FROM pbs_group_chunks s
            WHERE s.pbs_id = %s AND NOT EXISTS (SELECT 1 FROM pbs_snapshots WHERE {_SAME_GROUP})
            RETURNING store, ns, backup_type, backup_id
            """,
            (pbs_id,),
        )
        lost |= {(r["store"], r["ns"], r["backup_type"], r["backup_id"]) for r in cur.fetchall()}
        conn.commit()
        return lost


def load_pbs_group_chunks(pbs_id: int, group: Group) -> tuple[bytes, dict[int, bytes]]:
    """A guest's chunk digests and, per indexed snapshot, its bitmap."""
    key = (pbs_id, *group)
    where = "pbs_id = %s AND store = %s AND ns = %s AND backup_type = %s AND backup_id = %s"
    with connect() as conn, conn.cursor() as cur:
        cur.execute(f"SELECT digests FROM pbs_group_chunks WHERE {where}", key)
        row = cur.fetchone()
        cur.execute(f"SELECT backup_time, bitmap FROM pbs_snapshot_chunks WHERE {where}", key)
        return bytes(row["digests"]) if row else b"", {r["backup_time"]: bytes(r["bitmap"]) for r in cur.fetchall()}


def save_pbs_group_chunks(pbs_id: int, group: Group, digests: bytes, chunks: int, bitmaps: dict[int, bytes]) -> None:
    """A guest's chunks after its snapshots changed. How many of them are
    its own alone is open again (exclusive NULL) until counted."""
    key = (pbs_id, *group)
    where = "pbs_id = %s AND store = %s AND ns = %s AND backup_type = %s AND backup_id = %s"
    with connect() as conn, conn.cursor() as cur:
        cur.execute(f"DELETE FROM pbs_snapshot_chunks WHERE {where}", key)
        cur.execute(f"DELETE FROM pbs_group_chunks WHERE {where}", key)
        if bitmaps:
            cur.execute(
                """
                INSERT INTO pbs_group_chunks (pbs_id, store, ns, backup_type, backup_id, digests, chunks)
                VALUES (%s, %s, %s, %s, %s, %s, %s)
                """,
                (*key, digests, chunks),
            )
            cur.executemany(
                """
                INSERT INTO pbs_snapshot_chunks (pbs_id, store, ns, backup_type, backup_id, backup_time, bitmap)
                VALUES (%s, %s, %s, %s, %s, %s, %s)
                """,
                [(*key, backup_time, bitmap) for backup_time, bitmap in bitmaps.items()],
            )
        conn.commit()


def pbs_store_digests(pbs_id: int, store: str) -> dict[Group, bytes]:
    """The chunk digests of every guest on a datastore."""
    with connect() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT ns, backup_type, backup_id, digests FROM pbs_group_chunks WHERE pbs_id = %s AND store = %s",
            (pbs_id, store),
        )
        return {(store, r["ns"], r["backup_type"], r["backup_id"]): bytes(r["digests"]) for r in cur.fetchall()}


def save_pbs_exclusive_chunks(pbs_id: int, counts: dict[Group, int]) -> None:
    with connect() as conn, conn.cursor() as cur:
        cur.executemany(
            """
            UPDATE pbs_group_chunks SET exclusive = %s
            WHERE pbs_id = %s AND store = %s AND ns = %s AND backup_type = %s AND backup_id = %s
            """,
            [(count, pbs_id, *group) for group, count in counts.items()],
        )
        conn.commit()


def save_pbs_counted_chunks(pbs_id: int, store: str, chunks: int) -> None:
    """How many different chunks all guests of a datastore use together."""
    with connect() as conn, conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO pbs_store_space (pbs_id, store, counted_chunks) VALUES (%s, %s, %s)
            ON CONFLICT (pbs_id, store) DO UPDATE SET counted_chunks = EXCLUDED.counted_chunks
            """,
            (pbs_id, store, chunks),
        )
        conn.commit()


def pbs_store_capacity(pbs_ids: list[int], store: str) -> dict[str, Any] | None:
    """Size, use and free room of a datastore as last read (under any of the
    ids its PBS is registered with)."""
    with connect() as conn, conn.cursor() as cur:
        cur.execute(
            """
            SELECT total_bytes, used_bytes, avail_bytes FROM pbs_datastore_usage
            WHERE pbs_id = ANY(%s) AND store = %s ORDER BY updated_at DESC LIMIT 1
            """,
            (pbs_ids, store),
        )
        return cur.fetchone()


def save_pbs_store_space(
    pbs_id: int, store: str, disk_bytes: int | None, disk_chunks: int | None, denied: bool | None
) -> None:
    """What the datastore's last garbage collection counted; `denied` None
    leaves as it was whether the token may read index files."""
    with connect() as conn, conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO pbs_store_space (pbs_id, store, disk_bytes, disk_chunks, denied)
            VALUES (%s, %s, %s, %s, COALESCE(%s, FALSE))
            ON CONFLICT (pbs_id, store) DO UPDATE SET
                disk_bytes = COALESCE(EXCLUDED.disk_bytes, pbs_store_space.disk_bytes),
                disk_chunks = COALESCE(EXCLUDED.disk_chunks, pbs_store_space.disk_chunks),
                denied = COALESCE(%s, pbs_store_space.denied)
            """,
            (pbs_id, store, disk_bytes, disk_chunks, denied, denied),
        )
        conn.commit()


def get_pbs_store_space(pbs_id: int, store: str) -> dict[str, Any] | None:
    with connect() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT disk_bytes, disk_chunks, denied, counted_chunks FROM pbs_store_space WHERE pbs_id = %s AND store = %s",
            (pbs_id, store),
        )
        return cur.fetchone()


def pbs_space_groups(pbs_id: int, store: str) -> list[dict[str, Any]]:
    """The guests with snapshots on a datastore: how many snapshots, for how
    many the chunks are known, and the chunk counts."""
    with connect() as conn, conn.cursor() as cur:
        cur.execute(
            f"""
            SELECT s.ns, s.backup_type, s.backup_id,
                   COUNT(*) AS snapshots,
                   (SELECT COUNT(*) FROM pbs_snapshot_chunks WHERE {_SAME_GROUP}) AS known,
                   (ARRAY_AGG(s.guest_name ORDER BY s.backup_time DESC)
                        FILTER (WHERE s.guest_name IS NOT NULL AND s.guest_name <> ''))[1] AS name,
                   (SELECT chunks FROM pbs_group_chunks WHERE {_SAME_GROUP}) AS chunks,
                   (SELECT exclusive FROM pbs_group_chunks WHERE {_SAME_GROUP}) AS exclusive
            FROM pbs_snapshots s
            WHERE s.pbs_id = %s AND s.store = %s
            GROUP BY s.pbs_id, s.store, s.ns, s.backup_type, s.backup_id
            """,
            (pbs_id, store),
        )
        return list(cur.fetchall())
