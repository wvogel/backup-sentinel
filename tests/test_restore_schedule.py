"""Scheduling + result-to-clock semantics (DB-gated) and cadence mapping."""

import os
import unittest

from app.services.backup_status import cadence_days


class CadenceTest(unittest.TestCase):
    def test_cadence_days_mapping(self) -> None:
        self.assertEqual(cadence_days("month"), 30)
        self.assertEqual(cadence_days("quarter"), 91)
        self.assertEqual(cadence_days("half_year"), 182)
        self.assertEqual(cadence_days("year"), 365)
        self.assertEqual(cadence_days("bogus"), 182)  # default half-year


if os.environ.get("BSENTINEL_DB_TESTS") != "1":
    raise unittest.SkipTest("set BSENTINEL_DB_TESTS=1 to run database tests (they truncate all tables)")

from app import db, db_restore


def setUpModule() -> None:
    db.init_db()


class ScheduleTest(unittest.TestCase):
    def setUp(self) -> None:
        with db.connect() as conn, conn.cursor() as cur:
            cur.execute("TRUNCATE clusters RESTART IDENTITY CASCADE")
            conn.commit()
        db.create_cluster("Test", "test", "https://pve.example:8006", "secret")
        self.cid = db.get_cluster_by_slug("test")["id"]
        db.sync_cluster_inventory(
            self.cid,
            [{"name": "pve1", "status": "online"}],
            [{"node_name": "pve1", "vmid": 100, "name": "web", "backup_kind": "daily", "last_backup_at": None}],
        )
        with db.connect() as conn, conn.cursor() as cur:
            cur.execute("SELECT id FROM vms WHERE cluster_id = %s AND vmid = 100", (self.cid,))
            self.vm_id = cur.fetchone()["id"]

    def _enable(self, **over) -> None:
        cols = {"owner_name": "x", "criticality": "medium", "rpo_hours": 24, "auto_restore_enabled": True}
        cols.update(over)
        keys = ", ".join(cols)
        ph = ", ".join(["%s"] * len(cols))
        updates = ", ".join(f"{k} = EXCLUDED.{k}" for k in cols)
        with db.connect() as conn, conn.cursor() as cur:
            cur.execute(
                f"INSERT INTO vm_policies (vm_id, {keys}) VALUES (%s, {ph}) "
                f"ON CONFLICT (vm_id) DO UPDATE SET {updates}",
                (self.vm_id, *cols.values()),
            )
            conn.commit()

    def test_due_includes_never_tested_enabled(self) -> None:
        self._enable()
        due = [r["vm_id"] for r in db_restore.due_auto_restore_candidates()]
        self.assertIn(self.vm_id, due)

    def test_disabled_not_due(self) -> None:
        self._enable(auto_restore_enabled=False)
        self.assertEqual(db_restore.due_auto_restore_candidates(), [])

    def test_live_run_excludes_from_due(self) -> None:
        self._enable()
        db_restore.create_run(self.vm_id)  # queued
        due = [r["vm_id"] for r in db_restore.due_auto_restore_candidates()]
        self.assertNotIn(self.vm_id, due)

    def test_failed_result_writes_no_restore_test_row(self) -> None:
        self._enable()
        run_id = db_restore.create_run(self.vm_id)
        test_id = db_restore.record_run_result(run_id, self.vm_id, "failed", duration_seconds=120)
        self.assertIsNone(test_id)
        with db.connect() as conn, conn.cursor() as cur:
            cur.execute("SELECT COUNT(*) AS n FROM restore_tests WHERE vm_id = %s", (self.vm_id,))
            self.assertEqual(cur.fetchone()["n"], 0)
            cur.execute("SELECT status, result FROM restore_runs WHERE id = %s", (run_id,))
            row = cur.fetchone()
        self.assertEqual(row["status"], "failed")

    def test_passed_result_writes_and_links(self) -> None:
        self._enable()
        run_id = db_restore.create_run(self.vm_id)
        test_id = db_restore.record_run_result(run_id, self.vm_id, "passed", duration_seconds=300)
        self.assertIsNotNone(test_id)
        run = db_restore.get_run(run_id)
        self.assertEqual(run["restore_test_id"], test_id)
        self.assertEqual(run["result"], "passed")
        # A recent test makes it no longer due.
        self.assertNotIn(self.vm_id, [r["vm_id"] for r in db_restore.due_auto_restore_candidates()])

    def test_held_status_keeps_no_test_row(self) -> None:
        self._enable()
        run_id = db_restore.create_run(self.vm_id)
        test_id = db_restore.record_run_result(run_id, self.vm_id, "failed", duration_seconds=10, status="held")
        self.assertIsNone(test_id)
        self.assertEqual(db_restore.get_run(run_id)["status"], "held")
