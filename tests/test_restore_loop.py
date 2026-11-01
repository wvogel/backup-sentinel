"""Orchestrator integration tests (DB-gated) with a fake RestoreServer."""

import os
import unittest

if os.environ.get("BSENTINEL_DB_TESTS") != "1":
    raise unittest.SkipTest("set BSENTINEL_DB_TESTS=1 to run database tests (they truncate all tables)")

from app import db, db_restore
from app.services import restore_orchestrator as orch

CONFIG = dict(
    api_url="https://10.0.0.9:8006",
    api_user="root@pam",
    token_id="r",
    token_secret="s",
    ssh_host="10.0.0.9",
    ssh_private_key="k",
    mgmt_iface="vmbr0",
    mgmt_netmask="255.255.255.0",
    mgmt_ip_from="10.9.0.10",
    mgmt_ip_to="10.9.0.12",
    sandbox_bridge="vmbr9",
    sandbox_vlan_from=900,
    sandbox_vlan_to=999,
    restore_vmid_from=9000,
    restore_vmid_to=9100,
    target_storages=[{"storage": "zfs", "type": "zfspool", "disk_class": "enterprise-ssd", "order": 0}],
    probe_first_delay_seconds=0,
    probe_interval_seconds=0,
    probe_window_seconds=0,
    probe_offset=254,
)


class FakeServer:
    def __init__(self, storages, snapshots):
        self._storages = storages
        self._snapshots = snapshots
        self.executed = []

    def list_pbs_storages(self):
        return self._storages

    def list_snapshots(self, storage, vmid):
        return self._snapshots


def setUpModule() -> None:
    db.init_db()


class OrchestratorTest(unittest.TestCase):
    def setUp(self):
        with db.connect() as conn, conn.cursor() as cur:
            cur.execute("TRUNCATE clusters RESTART IDENTITY CASCADE")
            conn.commit()
        db.create_cluster("Prod", "prod", "https://pve.prod.example:8006", "secret")
        self.cid = db.get_cluster_by_slug("prod")["id"]
        db.sync_cluster_inventory(
            self.cid,
            [{"name": "pve1", "status": "online"}],
            [{"node_name": "pve1", "vmid": 100, "name": "web", "backup_kind": "daily", "last_backup_at": None}],
        )
        with db.connect() as conn, conn.cursor() as cur:
            cur.execute("SELECT id FROM vms WHERE vmid = 100")
            self.vm_id = cur.fetchone()["id"]
            cur.execute(
                "INSERT INTO vm_policies (vm_id, owner_name, criticality, rpo_hours, auto_restore_enabled) "
                "VALUES (%s, 'x', 'medium', 24, TRUE)",
                (self.vm_id,),
            )
            # The VM's real backup location (as the protection column records it).
            cur.execute(
                "INSERT INTO backup_events (vm_id, started_at, status, size_bytes, source, locations) "
                "VALUES (%s, NOW(), 'ok', 100, 'pbs', %s)",
                (self.vm_id, ["pbs:pbs-a/ds1/prod"]),
            )
            conn.commit()
        db_restore.set_network_interfaces(
            self.vm_id,
            [
                {
                    "nic_index": 0,
                    "mac": "aa:bb:cc:dd:ee:01",
                    "ip_addresses": ["10.0.0.50"],
                    "prefix": 24,
                    "is_primary": True,
                    "source": "agent",
                }
            ],
        )

    def _server(self):
        return FakeServer(
            storages=[
                {"storage": "local-pbs", "server": "pbs-a", "datastore": "ds1", "namespace": "prod", "fingerprint": ""}
            ],
            snapshots=[
                {"snapshot_id": "local-pbs:backup/vm/100/2026-10-30T01:00:00Z", "backup_time": 10, "complete": True}
            ],
        )

    def test_production_endpoints(self):
        self.assertIn("pve.prod.example", orch.production_endpoints())

    def test_vm_backup_locations_from_events(self):
        locs = orch.vm_backup_locations(self.vm_id)
        self.assertEqual(locs, {"pbs:pbs-a/ds1/prod"})

    def test_matched_vms_by_location_finds_the_vm(self):
        # Regression: the comma-then-JOIN form put backup_events out of scope for
        # the vms join, so this threw and the diagnostic showed "no VM matched"
        # everywhere even while restores matched fine.
        matched = orch.matched_vms_by_location(["pbs:pbs-a/ds1/prod", "pbs:other/x"])
        self.assertEqual(matched, {"pbs:pbs-a/ds1/prod": ["Prod:web"]})

    def test_all_vm_backup_locations_lists_the_location(self):
        rows = orch.all_vm_backup_locations()
        self.assertEqual(rows, [{"location": "pbs:pbs-a/ds1/prod", "vms": 1, "sample": "Prod:web"}])

    def test_build_plan_matches_and_allocates(self):
        cand = orch._candidate_for_vm(self.vm_id)
        plan = orch.build_plan(cand, self._server(), CONFIG)
        self.assertIsNotNone(plan)
        self.assertEqual(plan.restore_vmid, 9000)
        self.assertEqual(plan.helper_vmid, 9001)
        self.assertEqual(plan.vlan_tag, 900)
        self.assertEqual(plan.mgmt_ip, "10.9.0.10")
        # Probe the fixed management-range address the helper's dnsmasq hands the
        # guest on the sandbox leg (mgmt .10 → guest .200), not the guest's own
        # config IP — independent of the restored VM.
        self.assertEqual(plan.primary_ip, "10.9.0.200")
        self.assertEqual(plan.target_storage, "zfs")
        # No per-NIC DHCP reservations any more: a single-address dynamic range.
        self.assertEqual(plan.reservations, [])

    def test_build_plan_without_target_storage_raises(self):
        # No ticked target storage must not reach PVE as an empty storage id.
        cand = orch._candidate_for_vm(self.vm_id)
        with self.assertRaises(orch.NotRunnable):
            orch.build_plan(cand, self._server(), {**CONFIG, "target_storages": []})

    def test_build_plan_not_restorable_raises(self):
        cand = orch._candidate_for_vm(self.vm_id)
        server = FakeServer(
            storages=[{"storage": "x", "server": "other", "datastore": "ds1", "namespace": "prod", "fingerprint": ""}],
            snapshots=[],
        )
        with self.assertRaises(orch.NotRunnable):
            orch.build_plan(cand, server, CONFIG)

    def test_enqueue_due_creates_run_for_restorable(self):
        n = orch.enqueue_due(self._server(), CONFIG)
        self.assertEqual(n, 1)
        self.assertEqual(db_restore.next_queued_run()["vm_id"], self.vm_id)


class _DrainServer:
    """Fake server: real matching/snapshot/free-space answers, no-op pipeline."""

    def __init__(self, avail, size_by_vmid):
        self._avail = avail
        self._size = size_by_vmid

    def list_pbs_storages(self):
        return [{"storage": "local-pbs", "server": "pbs-a", "datastore": "ds1", "namespace": "prod", "fingerprint": ""}]

    def list_snapshots(self, storage, vmid):
        return [
            {
                "snapshot_id": f"local-pbs:backup/vm/{vmid}/snap",
                "backup_time": 10,
                "size": self._size[vmid],
                "complete": True,
            }
        ]

    def free_resources(self):
        return {"ram_free": 10**12, "stores": [{"storage": "zfs", "avail": self._avail, "total": 10**13}]}

    def __getattr__(self, name):
        return lambda *a, **k: None


class DrainSkipTest(unittest.TestCase):
    def setUp(self):
        with db.connect() as conn, conn.cursor() as cur:
            cur.execute("TRUNCATE clusters RESTART IDENTITY CASCADE")
            cur.execute("TRUNCATE app_settings")
            conn.commit()
        db.create_cluster("Prod", "prod", "https://pve.prod.example:8006", "secret")
        self.cid = db.get_cluster_by_slug("prod")["id"]
        db.sync_cluster_inventory(
            self.cid,
            [{"name": "pve1", "status": "online"}],
            [
                {"node_name": "pve1", "vmid": 100, "name": "big", "backup_kind": "daily", "last_backup_at": None},
                {"node_name": "pve1", "vmid": 101, "name": "small", "backup_kind": "daily", "last_backup_at": None},
            ],
        )
        self.ids = {}
        with db.connect() as conn, conn.cursor() as cur:
            for vmid in (100, 101):
                cur.execute("SELECT id FROM vms WHERE vmid = %s", (vmid,))
                vid = cur.fetchone()["id"]
                self.ids[vmid] = vid
                cur.execute(
                    "INSERT INTO vm_policies (vm_id, owner_name, criticality, rpo_hours, auto_restore_enabled) "
                    "VALUES (%s, 'x', 'medium', 24, TRUE)",
                    (vid,),
                )
            for vid in self.ids.values():
                cur.execute(
                    "INSERT INTO backup_events (vm_id, started_at, status, size_bytes, source, locations) "
                    "VALUES (%s, NOW(), 'ok', 100, 'pbs', %s)",
                    (vid, ["pbs:pbs-a/ds1/prod"]),
                )
            conn.commit()
        for vmid in (100, 101):
            db_restore.set_network_interfaces(
                self.ids[vmid],
                [
                    {
                        "nic_index": 0,
                        "mac": "aa:bb",
                        "ip_addresses": [f"10.0.0.{vmid}"],
                        "prefix": 24,
                        "is_primary": True,
                        "source": "agent",
                    }
                ],
            )

    def test_too_big_run_is_skipped_small_one_runs(self):
        # avail 100: big (size 200 → needs 240) does NOT fit, small (50 → 60) fits.
        server = _DrainServer(avail=100, size_by_vmid={100: 200, 101: 50})
        big = db_restore.create_run(self.ids[100])  # queued first (FIFO)
        small = db_restore.create_run(self.ids[101])
        orch.drain(CONFIG, server)
        self.assertEqual(db_restore.get_run(big)["status"], "queued")  # skipped, still queued
        self.assertNotEqual(db_restore.get_run(small)["status"], "queued")  # executed


def test_target_storages_from_rows():
    from app.web.routes_restore import _target_storages_from_rows

    rows = _target_storages_from_rows(
        names=["zfs", "nfs", "dir"],
        classes=["enterprise-ssd", "sata-hdd", "standard-ssd"],
        used={"zfs", "dir"},
    )
    # Every row kept (disk class known everywhere); `use` marks targets, `order`
    # the fill order of the ticked ones.
    assert rows == [
        {"storage": "zfs", "disk_class": "enterprise-ssd", "use": True, "order": 0},
        {"storage": "nfs", "disk_class": "sata-hdd", "use": False, "order": 9999},
        {"storage": "dir", "disk_class": "standard-ssd", "use": True, "order": 1},
    ]
