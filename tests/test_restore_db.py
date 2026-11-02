"""Database tests for the automated-restore-testing schema and db layer.

Like test_db.py, these TRUNCATE tables, so they only run with
BSENTINEL_DB_TESTS=1 (see test_db.py for the throwaway-Postgres recipe).
"""

import os
import unittest

if os.environ.get("BSENTINEL_DB_TESTS") != "1":
    raise unittest.SkipTest("set BSENTINEL_DB_TESTS=1 to run database tests (they truncate all tables)")

from app import db, db_restore


def setUpModule() -> None:
    db.init_db()


def _vm(vmid: int, name: str) -> dict:
    return {"node_name": "pve1", "vmid": vmid, "name": name, "backup_kind": "daily", "last_backup_at": None}


class RestoreDbTestCase(unittest.TestCase):
    def setUp(self) -> None:
        with db.connect() as conn, conn.cursor() as cur:
            cur.execute("TRUNCATE clusters RESTART IDENTITY CASCADE")
            conn.commit()
        db.create_cluster("Test", "test", "https://pve.example:8006", "secret")
        self.cid = db.get_cluster_by_slug("test")["id"]
        db.sync_cluster_inventory(self.cid, [{"name": "pve1", "status": "online"}], [_vm(100, "a")])
        with db.connect() as conn, conn.cursor() as cur:
            cur.execute("SELECT id FROM vms WHERE cluster_id = %s AND vmid = 100", (self.cid,))
            self.vm_id = cur.fetchone()["id"]

    def test_create_and_update_run(self) -> None:
        run_id = db_restore.create_run(self.vm_id, trigger="manual")
        run = db_restore.get_run(run_id)
        self.assertEqual(run["status"], "queued")
        self.assertEqual(run["trigger"], "manual")

        db_restore.update_run(run_id, status="running", percent=42, mgmt_ip="10.9.0.5", vlan_tag=901, restore_vmid=9100)
        run = db_restore.get_run(run_id)
        self.assertEqual(run["percent"], 42)
        self.assertEqual(run["mgmt_ip"], "10.9.0.5")
        self.assertEqual(run["status"], "running")

    def test_enqueue_if_never_passed(self) -> None:
        # Fresh VM, never restored → enqueues one queued run.
        rid = db_restore.enqueue_if_never_passed(self.vm_id)
        self.assertIsNotNone(rid)
        self.assertEqual(db_restore.get_run(rid)["status"], "queued")
        # Already has an active (queued) run → skipped.
        self.assertIsNone(db_restore.enqueue_if_never_passed(self.vm_id))

    def test_enqueue_if_never_passed_skips_after_a_pass(self) -> None:
        db_restore.record_run_result(
            db_restore.create_run(self.vm_id), self.vm_id, "passed", duration_seconds=1, status="passed"
        )
        # A VM that has already passed is not re-queued on enable.
        self.assertIsNone(db_restore.enqueue_if_never_passed(self.vm_id))

    def test_enqueue_if_never_passed_requeues_after_only_failures(self) -> None:
        db_restore.record_run_result(
            db_restore.create_run(self.vm_id), self.vm_id, "error", duration_seconds=1, status="error"
        )
        # Only failed/errored before (never passed) and nothing active → enqueues.
        self.assertIsNotNone(db_restore.enqueue_if_never_passed(self.vm_id))

    def test_list_runs_pagination_and_filters(self) -> None:
        db.create_cluster("Other", "other", "https://pve2.example:8006", "secret")
        cid2 = db.get_cluster_by_slug("other")["id"]
        db.sync_cluster_inventory(cid2, [{"name": "pve1", "status": "online"}], [_vm(200, "beta")])
        with db.connect() as conn, conn.cursor() as cur:
            cur.execute("SELECT id FROM vms WHERE cluster_id = %s AND vmid = 200", (cid2,))
            vm2 = cur.fetchone()["id"]
        for _ in range(3):
            db_restore.create_run(self.vm_id, trigger="manual")  # cluster "Test"
        for _ in range(2):
            db_restore.create_run(vm2, trigger="scheduled")  # cluster "Other", vm name "beta"

        self.assertEqual(db_restore.count_runs(), 5)
        self.assertEqual(db_restore.count_runs(trigger="manual"), 3)
        self.assertEqual(db_restore.count_runs(cluster="Other"), 2)
        self.assertEqual(db_restore.count_runs(vm_query="bet"), 2)  # ILIKE on vm name
        self.assertEqual(db_restore.count_runs(vm_query="200"), 2)  # LIKE on vmid

        # Offset/limit paginate without overlap.
        page1 = db_restore.list_runs(limit=2, offset=0)
        page2 = db_restore.list_runs(limit=2, offset=2)
        self.assertEqual(len(page1), 2)
        self.assertEqual(len(page2), 2)
        self.assertTrue({r["id"] for r in page1}.isdisjoint({r["id"] for r in page2}))
        self.assertEqual(set(db_restore.run_filter_clusters()), {"Test", "Other"})

    def test_list_runs_orders_queued_then_running_then_finished(self) -> None:
        done_old = db_restore.create_run(self.vm_id)
        db_restore.update_run(done_old, status="passed", finished_at="2026-11-01T08:00:00+00:00")
        running = db_restore.create_run(self.vm_id)
        db_restore.update_run(running, status="running", started_at="2026-11-01T09:00:00+00:00")
        done_new = db_restore.create_run(self.vm_id)
        db_restore.update_run(done_new, status="failed", finished_at="2026-11-01T10:00:00+00:00")
        q1 = db_restore.create_run(self.vm_id)
        q2 = db_restore.create_run(self.vm_id)
        # Queued first in drainer (FIFO) order, then running, then finished newest first.
        self.assertEqual([r["id"] for r in db_restore.list_runs()], [q1, q2, running, done_new, done_old])

    def test_checks_jsonb_roundtrip(self) -> None:
        run_id = db_restore.create_run(self.vm_id)
        db_restore.update_run(run_id, checks={"10.0.0.1": {"icmp": True, "ports": {"443": True}}})
        run = db_restore.get_run(run_id)
        self.assertTrue(run["checks"]["10.0.0.1"]["icmp"])

    def test_active_and_held_and_queued_ids(self) -> None:
        q = db_restore.create_run(self.vm_id)  # queued
        r = db_restore.create_run(self.vm_id)
        db_restore.update_run(r, status="held", restore_vmid=9100, vlan_tag=901, mgmt_ip="10.9.0.5")
        owned = db_restore.active_and_held_runs()
        self.assertEqual([row["id"] for row in owned], [r])
        self.assertEqual(db_restore.queued_vm_ids(), {self.vm_id})
        self.assertEqual(db_restore.next_queued_run()["id"], q)

    def test_network_interfaces_roundtrip(self) -> None:
        db_restore.set_network_interfaces(
            self.vm_id,
            [
                {"nic_index": 1, "mac": "bb:bb", "ip_addresses": ["10.0.0.2"], "prefix": 24, "source": "agent"},
                {
                    "nic_index": 0,
                    "mac": "aa:aa",
                    "ip_addresses": ["10.0.0.1"],
                    "prefix": 24,
                    "is_primary": True,
                    "source": "agent",
                },
            ],
        )
        nics = db_restore.get_network_interfaces(self.vm_id)
        self.assertEqual([n["nic_index"] for n in nics], [0, 1])
        self.assertTrue(nics[0]["is_primary"])
        self.assertEqual(nics[0]["ip_addresses"], ["10.0.0.1"])

    def test_vm_policy_restore_defaults(self) -> None:
        db.update_vm_restore_class(self.vm_id, "high")
        with db.connect() as conn, conn.cursor() as cur:
            cur.execute("SELECT * FROM vm_policies WHERE vm_id = %s", (self.vm_id,))
            pol = cur.fetchone()
        self.assertEqual(pol["restore_cadence"], "half_year")
        self.assertFalse(pol["auto_restore_enabled"])
        self.assertEqual(pol["restore_retry_days"], 7)
        self.assertTrue(pol["probe_require_icmp"])
