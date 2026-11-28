"""The tables behind the space per guest (app.db_pbs_space).

These write to the database, so they only run with BSENTINEL_DB_TESTS=1.
"""

import os
import unittest

if os.environ.get("BSENTINEL_DB_TESTS") != "1":
    raise unittest.SkipTest("database tests need BSENTINEL_DB_TESTS=1")

from app import db

GROUP = ("nas-b1", "berlin", "vm", "104")
OTHER = ("nas-b1", "berlin", "vm", "105")


def setUpModule() -> None:
    db.init_db()


def _snap(vmid: str, time: int, name: str | None = None) -> dict:
    return {"backup_type": "vm", "backup_id": vmid, "backup_time": time, "guest_name": name}


class PbsSpaceDbTestCase(unittest.TestCase):
    def setUp(self) -> None:
        with db.connect() as conn, conn.cursor() as cur:
            cur.execute("TRUNCATE clusters CASCADE")
            cur.execute(
                """
                INSERT INTO clusters (name, slug, api_url, enrollment_secret, created_at)
                VALUES ('c', 'c', 'https://pve:8006', 'x', now()) RETURNING id
                """
            )
            cluster = cur.fetchone()["id"]
            cur.execute(
                """
                INSERT INTO pbs_connections (cluster_id, name, api_url, api_token_id, api_token_secret, created_at)
                VALUES (%s, 'pbs-a', 'https://pbs-a:8007', 't', 's', now()) RETURNING id
                """,
                (cluster,),
            )
            self.pbs = cur.fetchone()["id"]
            conn.commit()

    def test_chunks_follow_the_snapshots(self) -> None:
        listed = {("nas-b1", "berlin"): [_snap("104", 100, "web"), _snap("104", 200), _snap("105", 100, "db")]}
        db.replace_pbs_snapshots(self.pbs, listed)
        db.save_pbs_group_chunks(self.pbs, GROUP, b"\x01" * 16, 2, {100: b"\x01", 200: b"\x03"})
        db.save_pbs_group_chunks(self.pbs, OTHER, b"\x02" * 8, 1, {100: b"\x01"})
        self.assertEqual(db.pbs_indexed_snapshots(self.pbs), {(*GROUP, 100), (*GROUP, 200), (*OTHER, 100)})
        self.assertEqual(db.load_pbs_group_chunks(self.pbs, GROUP), (b"\x01" * 16, {100: b"\x01", 200: b"\x03"}))
        self.assertEqual(db.pbs_store_digests(self.pbs, "nas-b1"), {GROUP: b"\x01" * 16, OTHER: b"\x02" * 8})
        db.save_pbs_exclusive_chunks(self.pbs, {GROUP: 2, OTHER: 0})

        rows = {row["backup_id"]: row for row in db.pbs_space_groups(self.pbs, "nas-b1")}
        self.assertEqual(
            (rows["104"]["snapshots"], rows["104"]["known"], rows["104"]["name"], rows["104"]["chunks"]),
            (2, 2, "web", 2),
        )
        self.assertEqual((rows["104"]["exclusive"], rows["105"]["exclusive"]), (2, 0))

        # One snapshot pruned, one guest gone: both are named for a recount.
        db.replace_pbs_snapshots(self.pbs, {("nas-b1", "berlin"): [_snap("104", 200)]})
        self.assertEqual(db.forget_pbs_chunks(self.pbs), {GROUP, OTHER})
        self.assertEqual(db.load_pbs_group_chunks(self.pbs, GROUP)[1], {200: b"\x03"})
        self.assertEqual(db.load_pbs_group_chunks(self.pbs, OTHER), (b"", {}))
        self.assertEqual(db.forget_pbs_chunks(self.pbs), set())

        # Saved anew, what is the guest's own alone is open again.
        db.save_pbs_group_chunks(self.pbs, GROUP, b"\x01" * 8, 1, {200: b"\x01"})
        (row,) = db.pbs_space_groups(self.pbs, "nas-b1")
        self.assertEqual((row["chunks"], row["exclusive"], row["known"]), (1, None, 1))

    def test_what_the_garbage_collection_counted_is_kept(self) -> None:
        self.assertIsNone(db.get_pbs_store_space(self.pbs, "nas-b1"))
        db.save_pbs_store_space(self.pbs, "nas-b1", 2000, 2, True)
        db.save_pbs_store_space(self.pbs, "nas-b1", None, None, None)  # not readable this time: kept
        self.assertEqual(
            dict(db.get_pbs_store_space(self.pbs, "nas-b1")), {"disk_bytes": 2000, "disk_chunks": 2, "denied": True}
        )
        db.save_pbs_store_space(self.pbs, "nas-b1", 3000, 3, False)
        self.assertEqual(
            dict(db.get_pbs_store_space(self.pbs, "nas-b1")), {"disk_bytes": 3000, "disk_chunks": 3, "denied": False}
        )
