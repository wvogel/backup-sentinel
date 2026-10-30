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
                {"nic_index": 0, "mac": "aa:aa", "ip_addresses": ["10.0.0.1"], "prefix": 24, "is_primary": True,
                 "source": "agent"},
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
