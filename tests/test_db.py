"""Database tests against a real PostgreSQL.

They TRUNCATE all tables, so they only run when BSENTINEL_DB_TESTS=1 is set
(CI does this with a throwaway Postgres service). Locally:

    docker run -d --rm --name bs-test-db -p 55432:5432 \\
      -e POSTGRES_DB=t -e POSTGRES_USER=t -e POSTGRES_PASSWORD=t postgres:18
    BSENTINEL_DB_TESTS=1 BSENTINEL_DATABASE_URL=postgresql://t:t@localhost:55432/t pytest tests/test_db.py
"""

import os
import unittest
from datetime import UTC, datetime, timedelta

if os.environ.get("BSENTINEL_DB_TESTS") != "1":
    raise unittest.SkipTest("set BSENTINEL_DB_TESTS=1 to run database tests (they truncate all tables)")

from app import db
from app.services.vzdump_log import VmBackupRun

NOW = datetime.now(UTC).replace(second=0, microsecond=0)


def setUpModule() -> None:
    db.init_db()


def _vm(vmid: int, name: str) -> dict:
    return {"node_name": "pve1", "vmid": vmid, "name": name, "backup_kind": "daily", "last_backup_at": None}


def _event(vmid: int, started_at: datetime, size: int = 10_000) -> dict:
    return {"vmid": vmid, "started_at": started_at, "size_bytes": size, "status": "ok"}


class DbTestCase(unittest.TestCase):
    def setUp(self) -> None:
        with db.connect() as conn, conn.cursor() as cur:
            cur.execute("TRUNCATE clusters RESTART IDENTITY CASCADE")
            conn.commit()
        db.create_cluster("Test", "test", "https://pve.example:8006", "secret")
        self.cid = db.get_cluster_by_slug("test")["id"]
        db.sync_cluster_inventory(self.cid, [{"name": "pve1", "status": "online"}], [_vm(100, "a"), _vm(101, "b")])

    def vm_row(self, vmid: int) -> dict | None:
        with db.connect() as conn, conn.cursor() as cur:
            cur.execute("SELECT * FROM vms WHERE cluster_id = %s AND vmid = %s", (self.cid, vmid))
            return cur.fetchone()


class GoneVmTests(DbTestCase):
    def test_missing_vm_is_flagged_and_cleared_when_it_returns(self) -> None:
        db.sync_cluster_inventory(self.cid, [], [_vm(100, "a")])
        self.assertIsNotNone(self.vm_row(101)["missing_since"])
        db.sync_cluster_inventory(self.cid, [], [_vm(100, "a"), _vm(101, "b")])
        self.assertIsNone(self.vm_row(101)["missing_since"])

    def test_prune_deletes_only_gone_vms_without_backups(self) -> None:
        db.sync_cluster_inventory(self.cid, [], [_vm(100, "a"), _vm(101, "b"), _vm(102, "c")])
        db.sync_backup_events_for_source(self.cid, "pbs", [_event(101, NOW)])
        db.sync_cluster_inventory(self.cid, [], [_vm(100, "a")])  # 101 + 102 gone, 101 has a backup
        self.assertEqual(db.prune_gone_vms_without_backups(self.cid), 1)
        self.assertIsNone(self.vm_row(102))
        self.assertIsNotNone(self.vm_row(101))
        self.assertIsNotNone(self.vm_row(100))

    def test_governance_flags_gone_vms_and_hides_those_without_backups(self) -> None:
        db.sync_cluster_inventory(self.cid, [], [_vm(100, "a"), _vm(101, "b"), _vm(102, "c")])
        db.sync_backup_events_for_source(self.cid, "pbs", [_event(101, NOW)])
        db.sync_cluster_inventory(self.cid, [], [_vm(100, "a")])
        rows = {row["vmid"]: row for row in db.vm_governance_rows(self.cid)}
        self.assertEqual(set(rows), {100, 101})
        self.assertTrue(rows[101]["vm_gone"])
        self.assertEqual(rows[101]["backup_severity"], "ok")
        self.assertFalse(rows[100]["vm_gone"])


class StorageAndDetailTests(DbTestCase):
    def test_storages_are_replaced_on_every_sync(self) -> None:
        db.sync_backup_events_for_source(self.cid, "pbs", [_event(100, NOW)])
        db.update_backup_event_storages(
            self.cid, [{**_event(100, NOW), "storage": "pbs-a"}, {**_event(100, NOW), "storage": "pbs-b"}]
        )
        db.update_backup_event_storages(self.cid, [{**_event(100, NOW), "storage": "pbs-b"}])
        with db.connect() as conn, conn.cursor() as cur:
            cur.execute("SELECT storages FROM backup_events")
            self.assertEqual(cur.fetchone()["storages"], ["pbs-b"])

    def test_task_log_details_match_the_nearest_event(self) -> None:
        db.sync_backup_events_for_source(self.cid, "pbs", [_event(100, NOW), _event(100, NOW - timedelta(days=1))])
        runs = [
            VmBackupRun(vmid=100, started_at=NOW + timedelta(minutes=3), duration_seconds=305, reused_pct=90),
            VmBackupRun(vmid=100, started_at=NOW - timedelta(hours=5), duration_seconds=1),  # no event nearby
            VmBackupRun(vmid=100, started_at=NOW, failed=True),  # failed runs are ignored
        ]
        self.assertEqual(db.update_backup_event_details(self.cid, runs), 1)
        with db.connect() as conn, conn.cursor() as cur:
            cur.execute("SELECT started_at, duration_seconds, reused_pct FROM backup_events ORDER BY started_at")
            older, latest = cur.fetchall()
        self.assertEqual((latest["duration_seconds"], latest["reused_pct"]), (305, 90))
        self.assertIsNone(older["duration_seconds"])

    def test_processed_tasks_are_remembered(self) -> None:
        db.mark_backup_tasks_processed(self.cid, ["UPID:a", "UPID:b"])
        db.mark_backup_tasks_processed(self.cid, ["UPID:b"])
        self.assertEqual(db.processed_backup_task_upids(self.cid), {"UPID:a", "UPID:b"})

    def test_sparkline_carries_log_details(self) -> None:
        db.sync_backup_events_for_source(self.cid, "pbs", [_event(100, NOW)])
        db.update_backup_event_details(
            self.cid, [VmBackupRun(vmid=100, started_at=NOW, duration_seconds=305, full_backup=True)]
        )
        vm_id = self.vm_row(100)["id"]
        days = [d for d in db.vm_backup_sparkline_data(self.cid)[vm_id] if d["status"] == "ok"]
        self.assertEqual(len(days), 1)
        self.assertEqual(days[0]["duration_seconds"], 305)
        self.assertIs(days[0]["full_backup"], True)


class TypicalDurationTests(DbTestCase):
    def test_median_of_recent_runs_needs_three_samples(self) -> None:
        days = [NOW - timedelta(days=i) for i in range(4)]
        db.sync_backup_events_for_source(self.cid, "pbs", [_event(100, d) for d in days] + [_event(101, NOW)])
        runs = [VmBackupRun(vmid=100, started_at=d, duration_seconds=s) for d, s in zip(days, (300, 310, 900, 290))]
        runs.append(VmBackupRun(vmid=101, started_at=NOW, duration_seconds=60))
        db.update_backup_event_details(self.cid, runs)
        self.assertEqual(db.typical_backup_durations(self.cid), {100: 305})


class RestoreClassTests(DbTestCase):
    def setUp(self) -> None:
        super().setUp()
        db.sync_backup_events_for_source(self.cid, "pbs", [_event(100, NOW), _event(101, NOW)])
        self.vm100 = self.vm_row(100)["id"]

    def rows(self) -> dict:
        return {row["vmid"]: row for row in db.vm_governance_rows(self.cid)}

    def test_never_tested_is_open_with_default_180_days(self) -> None:
        row = self.rows()[100]
        self.assertEqual(row["restore_due_status"], "open")
        self.assertEqual(row["restore_interval_days"], 180)
        self.assertEqual(row["restore_class"], "medium")

    def test_class_sets_interval_and_expiry(self) -> None:
        db.update_vm_restore_class(self.vm100, "high")
        db.create_restore_test(self.vm100, (NOW - timedelta(days=100)).date(), "passed", "full", 5)
        row = self.rows()[100]
        self.assertEqual(row["restore_interval_days"], 90)
        self.assertEqual(row["restore_due_status"], "critical")
        db.update_vm_restore_class(self.vm100, "low")
        self.assertEqual(self.rows()[100]["restore_due_status"], "ok")

    def test_none_class_needs_no_test_and_survives_backup_policy_changes(self) -> None:
        db.update_vm_restore_class(self.vm100, "none")
        db.update_vm_backup_policy(self.vm100, "weekly")
        row = self.rows()[100]
        self.assertEqual(row["restore_class"], "none")
        self.assertEqual(row["restore_due_status"], "ok")


if __name__ == "__main__":
    unittest.main()
