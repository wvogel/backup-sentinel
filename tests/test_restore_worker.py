"""Worker pipeline tests: pure helpers always; branching logic DB-gated with a
fake RestoreServer."""

import os
import unittest

from app.services.restore_worker import _progress_fields, fits_disk, fits_ram, ram_reserve_bytes

GIB = 1024**3


class ProgressFieldsTest(unittest.TestCase):
    def test_under_100_has_no_phase(self):
        self.assertEqual(_progress_fields({"percent": 42, "bytes_done": 5}), {"percent": 42, "bytes_done": 5})

    def test_100_percent_marks_finalizing(self):
        # Copy done but task still running (volume rescan / cache flush).
        self.assertEqual(_progress_fields({"percent": 100})["phase"], "finalizing")


class FitsDiskTest(unittest.TestCase):
    def test_unknown_sizes_fit(self):
        self.assertTrue(fits_disk(None, 10))
        self.assertTrue(fits_disk(10, None))

    def test_fit_with_safety_margin(self):
        self.assertTrue(fits_disk(130, 100))  # 100*1.2 = 120 <= 130
        self.assertFalse(fits_disk(110, 100))  # 120 > 110


class FitsRamTest(unittest.TestCase):
    def test_reserve_is_ten_percent_min_ten_gib(self):
        self.assertEqual(ram_reserve_bytes(500 * GIB), 50 * GIB)  # 10%
        self.assertEqual(ram_reserve_bytes(50 * GIB), 10 * GIB)  # floor at 10 GiB
        self.assertEqual(ram_reserve_bytes(None), 10 * GIB)

    def test_unknown_guest_ram_fits(self):
        self.assertTrue(fits_ram(100 * GIB, 256 * GIB, None))

    def test_leaves_host_reserve(self):
        # 256 GiB host, reserve 25.6 GiB; 100 GiB free, guest needs 80 → 100-25.6=74.4 < 80 → no
        self.assertFalse(fits_ram(100 * GIB, 256 * GIB, 80 * GIB))
        # guest needs 70 → 74.4 >= 70 → yes
        self.assertTrue(fits_ram(100 * GIB, 256 * GIB, 70 * GIB))


if os.environ.get("BSENTINEL_DB_TESTS") != "1":
    raise unittest.SkipTest("set BSENTINEL_DB_TESTS=1 to run database tests (they truncate all tables)")

from app import db, db_restore
from app.services.restore_worker import RunPlan, execute, promote_held, reconcile_on_startup, resume_run


def setUpModule() -> None:
    db.init_db()


class FakeServer:
    def __init__(self):
        self.calls = []

    def exists(self, vmid, is_lxc=False):
        self.calls.append("exists")
        return True

    def __getattr__(self, name):
        def rec(*a, **k):
            self.calls.append(name)
            if name == "restore" and "on_progress" in k and k["on_progress"]:
                k["on_progress"]({"percent": 100, "bytes_done": 5, "bytes_total": 5})

        return rec


def _plan(vm_id, **over):
    base = dict(
        vm_id=vm_id,
        restore_vmid=9000,
        helper_vmid=9001,
        vlan_tag=901,
        mgmt_ip="10.9.0.10",
        source={"storage": "pbs", "src_vmid": 100, "pbs": "pbs-a", "datastore": "ds", "namespace": "ns"},
        snapshot_id="snap",
        target_storage="zfs",
        reservations=[{"mac": "aa", "ip": "10.0.0.50", "prefix": 24}],
        probe_targets=[{"ip": "10.0.0.50", "ports": [443]}],
        primary_ip="10.0.0.50",
        require_icmp=True,
        tcp_ports=[443],
        retry_days=7,
        first_delay=0,
        interval=0,
        window=0,
    )
    base.update(over)
    return RunPlan(**base)


class WorkerTest(unittest.TestCase):
    def setUp(self):
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
            cur.execute("SELECT id FROM vms WHERE vmid = 100")
            self.vm_id = cur.fetchone()["id"]
            cur.execute(
                "INSERT INTO vm_policies (vm_id, owner_name, criticality, rpo_hours, auto_restore_enabled) "
                "VALUES (%s, 'x', 'medium', 24, TRUE)",
                (self.vm_id,),
            )
            conn.commit()

    def _run(self, **over):
        return db_restore.create_run(self.vm_id)

    def _restore_test_count(self):
        with db.connect() as conn, conn.cursor() as cur:
            cur.execute("SELECT COUNT(*) AS n FROM restore_tests WHERE vm_id = %s", (self.vm_id,))
            return cur.fetchone()["n"]

    def _next_run(self):
        with db.connect() as conn, conn.cursor() as cur:
            cur.execute("SELECT next_run_at FROM vm_policies WHERE vm_id = %s", (self.vm_id,))
            return cur.fetchone()["next_run_at"]

    def test_passed_writes_test_row_and_tears_down(self):
        run_id = self._run()
        srv = FakeServer()
        res = execute(
            run_id,
            _plan(self.vm_id),
            srv,
            production_hosts=set(),
            now=lambda: 0.0,
            sleep=lambda s: None,
            probe_fn=lambda s, hv, t: {"10.0.0.50": {"icmp": True, "ports": {"443": True}}},
        )
        self.assertEqual(res, "passed")
        self.assertEqual(self._restore_test_count(), 1)
        self.assertIn("destroy", srv.calls)
        self.assertIsNone(self._next_run())

    def test_cancel_tears_down_and_records_cancelled(self):
        run_id = self._run()
        srv = FakeServer()
        res = execute(
            run_id,
            _plan(self.vm_id, hold_on_failure=True),  # hold must NOT keep a cancelled env
            srv,
            production_hosts=set(),
            now=lambda: 0.0,
            sleep=lambda s: None,
            probe_fn=lambda s, hv, t: {},
            should_cancel=lambda: True,
        )
        self.assertEqual(res, "cancelled")
        self.assertEqual(self._restore_test_count(), 0)  # no test row for a cancel
        self.assertIn("destroy", srv.calls)  # environment torn down
        self.assertEqual(db_restore.get_run(run_id)["status"], "cancelled")

    def test_failed_with_hold_keeps_vm_discards_helper(self):
        run_id = self._run()
        srv = FakeServer()
        res = execute(
            run_id,
            _plan(self.vm_id, hold_on_failure=True),
            srv,
            production_hosts=set(),
            now=lambda: 0.0,
            sleep=lambda s: None,
            probe_fn=lambda s, hv, t: {"10.0.0.50": {"icmp": False, "ports": {"443": False}}},
        )
        self.assertEqual(res, "held")
        self.assertEqual(self._restore_test_count(), 0)
        self.assertEqual(db_restore.get_run(run_id)["status"], "held")
        # The helper is discarded (its mgmt IP freed) but the VM is kept: the run
        # no longer reserves a helper_vmid / mgmt_ip.
        self.assertIn("destroy", srv.calls)
        run = db_restore.get_run(run_id)
        self.assertIsNone(run["helper_vmid"])
        self.assertIsNone(run["mgmt_ip"])

    def test_failed_without_hold_tears_down_and_schedules_retry(self):
        run_id = self._run()
        srv = FakeServer()
        res = execute(
            run_id,
            _plan(self.vm_id, hold_on_failure=False),
            srv,
            production_hosts=set(),
            now=lambda: 0.0,
            sleep=lambda s: None,
            probe_fn=lambda s, hv, t: {"10.0.0.50": {"icmp": False, "ports": {"443": False}}},
        )
        self.assertEqual(res, "failed")
        self.assertEqual(self._restore_test_count(), 0)
        self.assertIn("destroy", srv.calls)
        self.assertIsNotNone(self._next_run())  # retry armed

    def test_limited_when_no_primary_ip(self):
        run_id = self._run()
        srv = FakeServer()
        res = execute(
            run_id,
            _plan(self.vm_id, primary_ip=None),
            srv,
            production_hosts=set(),
            now=lambda: 0.0,
            sleep=lambda s: None,
            probe_fn=lambda s, hv, t: {},
        )
        self.assertEqual(res, "limited")
        self.assertEqual(self._restore_test_count(), 1)

    def test_probe_discovers_ip_via_guest_agent_when_none_recorded(self):
        run_id = self._run()

        class Srv(FakeServer):
            def guest_agent_ips(self, vmid):
                self.calls.append("guest_agent_ips")
                return ["10.0.0.77"]

        srv = Srv()
        res = execute(
            run_id,
            _plan(self.vm_id, primary_ip=None),
            srv,
            production_hosts=set(),
            now=lambda: 0.0,
            sleep=lambda s: None,
            probe_fn=lambda s, ip, t: {"10.0.0.77": {"icmp": True, "ports": {"443": True}}},
        )
        self.assertEqual(res, "passed")  # IP discovered from the agent, then probed
        self.assertIn("guest_agent_ips", srv.calls)

    def test_host_identity_failure_is_error(self):
        run_id = self._run()
        srv = FakeServer()

        def boom(hosts):
            raise RuntimeError("overlap")

        srv.verify_host_identity = boom
        res = execute(run_id, _plan(self.vm_id), srv, production_hosts={"x"}, now=lambda: 0.0, sleep=lambda s: None)
        self.assertEqual(res, "error")
        self.assertEqual(self._restore_test_count(), 0)

    def test_promote_held_writes_passed_and_reaps(self):
        run_id = self._run()
        db_restore.update_run(run_id, status="held", result="failed", restore_vmid=9000, helper_vmid=9001)
        srv = FakeServer()
        promote_held(run_id, _plan(self.vm_id), srv)
        self.assertEqual(self._restore_test_count(), 1)
        self.assertEqual(db_restore.get_run(run_id)["result"], "passed")
        self.assertIn("destroy", srv.calls)

    def test_reconcile_marks_running_as_error(self):
        # No restore server configured in the test DB → a running run can't be
        # resumed or reached, so it is marked error.
        run_id = self._run()
        db_restore.update_run(run_id, status="running")
        reconcile_on_startup()
        self.assertEqual(db_restore.get_run(run_id)["status"], "error")

    def test_resume_reattaches_to_task_and_finalizes(self):
        run_id = self._run()
        db_restore.update_run(
            run_id, status="running", restore_vmid=9000, helper_vmid=9001, task_upid="UPID:restore:abcd"
        )
        srv = FakeServer()
        res = resume_run(
            run_id,
            _plan(self.vm_id),
            srv,
            "UPID:restore:abcd",
            now=lambda: 0.0,
            sleep=lambda s: None,
            probe_fn=lambda s, hv, t: {"10.0.0.50": {"icmp": True, "ports": {"443": True}}},
        )
        self.assertEqual(res, "passed")
        self.assertIn("poll_task", srv.calls)  # re-attached to the running task
        self.assertIn("provision_helper", srv.calls)  # tail re-run idempotently
        self.assertEqual(db_restore.get_run(run_id)["result"], "passed")

    def test_teardown_skips_shutdown_when_vm_absent(self):
        from app.services.restore_worker import _teardown

        class Absent(FakeServer):
            def exists(self, vmid, is_lxc=False):
                self.calls.append("exists")
                return False

        srv = Absent()
        _teardown(srv, _plan(self.vm_id))
        self.assertNotIn("shutdown", srv.calls)  # no blind shutdown of a VM that isn't there
        self.assertNotIn("destroy", srv.calls)
        self.assertIn("reap", srv.calls)  # the reaper still runs as the safety net
