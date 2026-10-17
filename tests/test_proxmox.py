import unittest
from datetime import UTC, datetime
from unittest import mock
from zoneinfo import ZoneInfo

from app.services import proxmox, proxmox_progress
from app.services.proxmox import (
    classify_schedule,
    fetch_backup_details,
    infer_backup_kind_by_vmid,
)
from app.services.proxmox_progress import _current_vm_log, _extract_vmid_from_task, _parse_running_backup_progress


class ProxmoxHelpersTests(unittest.TestCase):
    def test_classify_schedule_detects_daily_and_weekly_patterns(self) -> None:
        self.assertEqual(classify_schedule("daily"), "daily")
        self.assertEqual(classify_schedule("mon,wed,fri 03:00"), "daily")
        self.assertEqual(classify_schedule("sun 03:00"), "weekly")
        self.assertEqual(classify_schedule("*-*-* 03:00:00"), "daily")

    def test_infer_backup_kind_prefers_specific_weekly_over_catch_all(self) -> None:
        vms = [{"vmid": 100}, {"vmid": 101}]
        jobs = [
            {"id": "all-daily", "enabled": 1, "schedule": "daily", "vmid": "all"},
            {"id": "special-weekly", "enabled": 1, "schedule": "sun 03:00", "vmid": "101"},
        ]

        result = infer_backup_kind_by_vmid(vms, jobs)

        self.assertEqual(result[100], "daily")
        self.assertEqual(result[101], "weekly")

    def test_infer_backup_kind_prefers_specific_daily_over_specific_weekly(self) -> None:
        vms = [{"vmid": 101}]
        jobs = [
            {"id": "weekly", "enabled": 1, "schedule": "sun 03:00", "vmid": "101"},
            {"id": "daily", "enabled": 1, "schedule": "daily", "vmid": "101"},
        ]

        result = infer_backup_kind_by_vmid(vms, jobs)

        self.assertEqual(result[101], "daily")

    def test_parse_running_backup_progress_extracts_percent_and_eta(self) -> None:
        task = {"starttime": int(datetime(2026, 3, 29, 10, 0, tzinfo=UTC).timestamp())}
        lines = ["INFO: 86% (12.3 GiB of 14.3 GiB) in 120s, read: 100.0 MiB/s, write: 100.0 MiB/s"]

        progress = _parse_running_backup_progress(task, lines)

        self.assertEqual(progress["label"], "Backup 86%")
        self.assertEqual(progress["written_label"], "12.3 GiB")
        self.assertEqual(progress["target_label"], "14.3 GiB")
        self.assertIn("ETA:", progress["title"])

    def test_parse_running_backup_progress_extracts_percent_with_hour_minute_elapsed(self) -> None:
        task = {"starttime": int(datetime(2026, 3, 29, 6, 45, 33, tzinfo=UTC).timestamp())}
        lines = ["INFO: 53% (867.1 GiB of 1.6 TiB) in 2h 20m 40s, read: 127.6 MiB/s, write: 127.6 MiB/s"]

        progress = _parse_running_backup_progress(task, lines)

        self.assertEqual(progress["label"], "Backup 53%")
        self.assertEqual(progress["written_label"], "867.1 GiB")
        self.assertEqual(progress["target_label"], "1.6 TiB")
        self.assertIn("ETA:", progress["title"])

    def test_parse_running_backup_progress_extracts_dirty_bitmap_delta(self) -> None:
        task = {"starttime": int(datetime(2026, 3, 29, 10, 0, tzinfo=UTC).timestamp())}
        lines = [
            "INFO: using fast incremental mode (dirty-bitmap), 26.0 GiB dirty of 4.5 TiB total",
            "INFO: 2% (604.0 MiB of 26.0 GiB) in 3s, read: 201.3 MiB/s, write: 197.3 MiB/s",
        ]

        progress = _parse_running_backup_progress(task, lines)

        self.assertEqual(progress["delta_label"], "26.0 GiB")
        self.assertEqual(progress["full_label"], "4.5 TiB")
        self.assertIn("Delta: 26.0 GiB", progress["title"])


# One scheduled vzdump task backs up several VMs one after another.
MULTI_VM_LOG = [
    "INFO: starting new backup job: vzdump 100 103 --storage pbs",
    "INFO: Starting Backup of VM 100 (qemu)",
    "INFO: using fast incremental mode (dirty-bitmap), 1.0 GiB dirty of 32.0 GiB total",
    "INFO: 100% (1.0 GiB of 1.0 GiB) in 10s, read: 100.0 MiB/s, write: 100.0 MiB/s",
    "INFO: Finished Backup of VM 100 (00:00:12)",
    "INFO: Starting Backup of VM 103 (qemu)",
    "INFO: using fast incremental mode (dirty-bitmap), 4.0 GiB dirty of 64.0 GiB total",
]
MULTI_VM_TASK = {"upid": "UPID:pve1:0001:0002:6700:vzdump::root@pam:", "id": ""}


class MultiVMBackupJobTests(unittest.TestCase):
    def test_vmid_is_the_vm_currently_being_backed_up(self) -> None:
        self.assertEqual(_extract_vmid_from_task(MULTI_VM_TASK, MULTI_VM_LOG), 103)

    def test_progress_ignores_previous_vms_in_the_job(self) -> None:
        progress = _parse_running_backup_progress(MULTI_VM_TASK, _current_vm_log(MULTI_VM_LOG))
        self.assertNotIn("percent", progress)
        self.assertEqual(progress["delta_label"], "4.0 GiB")

    def test_single_vm_log_is_used_as_is(self) -> None:
        lines = ["INFO: 42% (1.0 GiB of 2.4 GiB) in 5s, read: 1 MiB/s, write: 1 MiB/s"]
        self.assertEqual(_current_vm_log(lines), lines)


class ProgressTimesTests(unittest.TestCase):
    # Real excerpt: 37 TiB VM, second VM of the job; progress lines only every ~30 min.
    JOB_START = int(datetime(2026, 10, 9, 18, 0, 1, tzinfo=UTC).timestamp())
    LOG = (
        "INFO: Starting Backup of VM 103 (qemu)",
        "INFO: Backup started at 2026-10-09 20:05:06",
        "INFO: creating Proxmox Backup Server archive 'vm/103/2026-10-09T18:05:06Z'",
        "INFO:  46% (17.2 TiB of 37.3 TiB) in 23h 27m 42s, read: 184.2 MiB/s, write: 184.2 MiB/s",
        "INFO:  47% (17.6 TiB of 37.3 TiB) in 1d 9m 56s, read: 154.5 MiB/s, write: 154.5 MiB/s",
    )

    def _progress(self, lines) -> dict:
        with mock.patch.object(proxmox_progress, "APP_TIMEZONE", ZoneInfo("Europe/Berlin")):
            return _parse_running_backup_progress({"starttime": self.JOB_START}, list(lines))

    def test_start_is_the_vm_start_from_the_pbs_archive_name(self) -> None:
        progress = self._progress(self.LOG)
        self.assertEqual(progress["started_at"], datetime(2026, 10, 9, 18, 5, 6, tzinfo=UTC))
        self.assertEqual(progress["started_at_label"], "09.10.2026 20:05")

    def test_eta_extrapolates_from_vm_start_and_runtime(self) -> None:
        # 1d 9m 56s = 86996 s for 47 % → 185098 s in total after the VM start.
        progress = self._progress(self.LOG)
        self.assertEqual(progress["eta"], datetime(2026, 10, 11, 21, 30, 4, tzinfo=UTC))
        self.assertEqual(progress["eta_label"], "11.10.2026 23:30")

    def test_backup_started_line_is_read_in_local_time_without_pbs_archive(self) -> None:
        lines = [line for line in self.LOG if "archive" not in line]
        self.assertEqual(self._progress(lines)["started_at_label"], "09.10.2026 20:05")

    def test_job_start_is_used_until_the_vm_logs_its_start(self) -> None:
        progress = self._progress([])
        self.assertEqual(progress["started_at_label"], "09.10.2026 20:00")
        self.assertNotIn("eta", progress)


class RunningBackupsForClustersTests(unittest.TestCase):
    def setUp(self) -> None:
        proxmox_progress._cache.clear()

    def test_page_loads_never_wait_and_refresh_in_the_background(self) -> None:
        import threading

        release = threading.Event()

        def fake_fetch(cluster, nodes, timeout=5.0):
            release.wait(2)
            if cluster["id"] == 1:
                return {103: {"percent": 48}}, True
            if cluster["id"] == 2:
                return {}, False  # a node's task list failed
            raise RuntimeError("boom")

        clusters = [({"id": i}, ["n1"]) for i in (1, 2, 3)]
        with mock.patch.object(proxmox_progress, "_fetch_running_backup_progress", side_effect=fake_fetch):
            # Nothing cached: answers at once, refreshes behind the scenes.
            self.assertEqual(proxmox_progress.cached_running_backups(clusters), {1: None, 2: None, 3: None})
            release.set()
            proxmox_progress._executor.submit(lambda: None).result()
            for _ in range(50):
                if not proxmox_progress._refreshing:
                    break
                threading.Event().wait(0.02)
            result = proxmox_progress.cached_running_backups(clusters)
        self.assertEqual(result, {1: {103: {"percent": 48}}, 2: None, 3: None})

    def test_cache_avoids_a_second_pve_call(self) -> None:
        fetch = mock.Mock(return_value=({101: {}}, True))
        with mock.patch.object(proxmox_progress, "_fetch_running_backup_progress", fetch):
            proxmox_progress.running_backup_progress_cached({"id": 7}, ["n1"])
            progress, complete = proxmox_progress.running_backup_progress_cached({"id": 7}, ["n1"])
        self.assertEqual(fetch.call_count, 1)
        self.assertEqual((progress, complete), ({101: {}}, True))


class ProgressThroughputTests(unittest.TestCase):
    def test_read_and_write_rates_from_the_latest_progress_line(self) -> None:
        lines = [
            "INFO:  46% (17.2 TiB of 37.3 TiB) in 23h 27m 42s, read: 184.2 MiB/s, write: 184.2 MiB/s",
            "INFO:  47% (17.6 TiB of 37.3 TiB) in 1d 9m 56s, read: 1.6 GiB/s, write: 154.5 MiB/s",
        ]
        progress = _parse_running_backup_progress({}, lines)
        self.assertEqual(progress["read_label"], "1.6 GiB/s")
        self.assertEqual(progress["write_label"], "155 MiB/s")

    def test_missing_rates_are_left_out(self) -> None:
        progress = _parse_running_backup_progress({}, ["INFO: 47% (17.6 TiB of 37.3 TiB) in 1d 9m 56s, done"])
        self.assertEqual(progress["percent"], 47)
        self.assertNotIn("read_label", progress)


class FetchFinishedBackupTasksTests(unittest.TestCase):
    def test_reads_only_new_finished_tasks(self) -> None:
        tasks = [
            {"upid": "UPID:new", "endtime": 1760040000},
            {"upid": "UPID:seen", "endtime": 1760040000},
            {"upid": "UPID:running"},  # no endtime yet
        ]
        log = [
            {"n": 1, "t": "INFO: Starting Backup of VM 100 (qemu)"},
            {"n": 2, "t": "INFO: creating Proxmox Backup Server archive 'vm/100/2026-10-09T18:00:01Z'"},
            {"n": 3, "t": "INFO: Finished Backup of VM 100 (00:05:05)"},
        ]
        cluster = {"api_url": "https://pve", "api_user": "u@pve", "token_id": "t", "token_secret": "s"}
        with (
            mock.patch.object(proxmox, "_pve_get_json", return_value=tasks),
            mock.patch.object(proxmox, "_pve_get", return_value=log) as get_log,
        ):
            result = proxmox.fetch_finished_backup_tasks(cluster, ["pve1"], {"UPID:seen"}, ZoneInfo("Europe/Berlin"))
        self.assertEqual([task.upid for task in result], ["UPID:new"])
        self.assertEqual(get_log.call_count, 1)
        self.assertEqual(result[0].runs[0].vmid, 100)
        self.assertEqual(result[0].runs[0].duration_seconds, 305)


class BackupLocationTaggingTests(unittest.TestCase):
    def setUp(self):
        self.config = [
            {"storage": "pbs-main", "type": "pbs", "server": "10.0.0.5", "datastore": "nas-b1", "fingerprint": "AA:BB"},
            {"storage": "nfs", "type": "nfs"},
        ]

    def fake(self, base_url, path, auth_header, **kwargs):
        if path == "/api2/json/storage":
            return self.config
        if path.endswith("/storage"):
            return [
                {"storage": "pbs-main", "content": "backup", "type": "pbs"},
                {"storage": "nfs", "content": "backup", "type": "nfs"},
            ]
        storage = path.split("/storage/")[1].split("/")[0]
        return [{"volid": f"{storage}:backup/vm/100/x", "vmid": 100, "ctime": 1_700_000_000, "size": 10}]

    def test_events_carry_their_location(self):
        with mock.patch.object(proxmox, "_pve_get_json", side_effect=self.fake):
            _latest, events, _errors = fetch_backup_details("https://pve", "a", [{"name": "pve1"}], [{"vmid": 100}])
        self.assertEqual(sorted(e["location"] for e in events), ["pbs:aabb/nas-b1", "pve:nfs"])
        # PBS storages keep their raw target so the sync can match the registered PBS.
        targets = [e["pbs_target"] for e in events if "pbs_target" in e]
        self.assertEqual([t["fingerprint"] for t in targets], ["AA:BB"])

    def test_pbs_storage_without_config_gets_no_location(self):
        # Without the storage config a PBS can't be identified; better no location than a fake 2nd copy.
        self.config = []
        with mock.patch.object(proxmox, "_pve_get_json", side_effect=self.fake):
            _latest, events, _errors = fetch_backup_details("https://pve", "a", [{"name": "pve1"}], [{"vmid": 100}])
        self.assertEqual(sorted(str(e["location"]) for e in events), ["None", "pve:nfs"])


class FetchBackupDetailsTests(unittest.TestCase):
    def _fake_pve(self, base_url, path, auth_header, **kwargs):
        if path.endswith("/storage"):
            return [{"storage": "nfs", "content": "backup", "type": "nfs"}]
        return [
            {"volid": "nfs:backup/vzdump-qemu-100.vma", "vmid": 100, "ctime": 1_700_000_000, "size": 10},
            {"volid": "nfs:backup/vzdump-qemu-200.vma", "vmid": 200, "ctime": 1_700_000_000, "size": 10},
            {"volid": "nfs:backup/vzdump-qemu-300.vma", "vmid": 300, "ctime": 1_700_000_000, "size": 10},
        ]

    def test_includes_backups_of_known_but_deleted_vms(self) -> None:
        nodes = [{"name": "pve1", "status": "online"}]
        vms = [{"vmid": 100, "name": "alive"}]
        with mock.patch.object(proxmox, "_pve_get_json", side_effect=self._fake_pve):
            latest, events, errors = fetch_backup_details("https://pve", "auth", nodes, vms, extra_vmids={200})
        self.assertEqual({e["vmid"] for e in events}, {100, 200})
        self.assertEqual(set(latest), {100, 200})
        self.assertEqual(errors, [])
        self.assertEqual({e["storage"] for e in events}, {"nfs"})

    def test_reports_unreachable_storage_as_error(self) -> None:
        def fake(base_url, path, auth_header, **kwargs):
            if path.endswith("/storage"):
                return [{"storage": "nfs", "content": "backup", "type": "nfs"}]
            raise proxmox.ProxmoxSyncError("timeout")

        nodes = [{"name": "pve1", "status": "online"}]
        with mock.patch.object(proxmox, "_pve_get_json", side_effect=fake):
            _latest, events, errors = fetch_backup_details("https://pve", "auth", nodes, [{"vmid": 100}])
        self.assertEqual(events, [])
        self.assertEqual(len(errors), 1)


class PbsStorageTargetTests(unittest.TestCase):
    def test_only_enabled_pbs_storages(self) -> None:
        config = [
            {
                "storage": "pbs-main",
                "type": "pbs",
                "server": "10.0.0.5",
                "datastore": "nas-b1",
                "fingerprint": "AA",
                "namespace": "x",
            },
            {"storage": "pbs-old", "type": "pbs", "server": "10.0.0.6", "datastore": "old", "disable": 1},
            {"storage": "nfs", "type": "nfs"},
        ]
        self.assertEqual(
            proxmox.pbs_storage_targets(config),
            [
                {
                    "storage_id": "pbs-main",
                    "server": "10.0.0.5",
                    "fingerprint": "AA",
                    "datastore": "nas-b1",
                    "namespace": "x",
                }
            ],
        )


class RateLabelTests(unittest.TestCase):
    def test_rates_are_rounded(self) -> None:
        self.assertEqual(proxmox_progress._round_rate("187.7 MiB/s"), "188 MiB/s")
        self.assertEqual(proxmox_progress._round_rate("1.6 GiB/s"), "1.6 GiB/s")
        self.assertEqual(proxmox_progress._round_rate("9.0 MiB/s"), "9 MiB/s")


class QDeviceTests(unittest.TestCase):
    def test_qdevice_status(self) -> None:
        status = {"State": "Connected", "QNetd host": "10.0.0.9:5403", "Model": "Net", "Algorithm": "Fifty-Fifty split"}
        with mock.patch.object(proxmox, "_pve_get_dict", return_value=status) as get:
            self.assertEqual(
                proxmox.fetch_qdevice("https://pve", "a"),
                {"state": "Connected", "host": "10.0.0.9:5403", "model": "Net", "algorithm": "Fifty-Fifty split"},
            )
        self.assertEqual(get.call_args.args[1], "/api2/json/cluster/config/qdevice")

    def test_no_qdevice_is_an_empty_answer(self) -> None:
        with mock.patch.object(proxmox, "_pve_get_dict", return_value={}):
            self.assertEqual(proxmox.fetch_qdevice("https://pve", "a"), {})

    def test_unreadable_is_unknown(self) -> None:
        with mock.patch.object(proxmox, "_pve_get_dict", side_effect=proxmox.ProxmoxSyncError("403")):
            self.assertIsNone(proxmox.fetch_qdevice("https://pve", "a"))


class HaTests(unittest.TestCase):
    def fetch(self, resources, groups=None, rules=None):
        def fake(base_url, path, auth_header, **kwargs):
            if path.endswith("/ha/resources"):
                return resources
            if path.endswith("/ha/groups"):
                if groups is None:
                    raise proxmox.ProxmoxSyncError("501 not implemented")
                return groups
            if path.endswith("/ha/rules"):
                if rules is None:
                    raise proxmox.ProxmoxSyncError("501 not implemented")
                return rules
            raise AssertionError(path)

        with mock.patch.object(proxmox, "_pve_get_json", side_effect=fake):
            return proxmox.fetch_ha("https://pve", "a")

    def test_strict_node_affinity_rule(self) -> None:
        ha = self.fetch(
            [
                {"sid": "vm:100", "state": "started"},
                {"sid": "ct:101", "state": "started"},
                {"sid": "vm:102", "state": "stopped"},
            ],
            rules=[
                {
                    "rule": "db",
                    "type": "node-affinity",
                    "resources": "vm:100,ct:101",
                    "nodes": "pve1:2,pve2",
                    "strict": 1,
                },
                {"rule": "off", "type": "node-affinity", "resources": "vm:102", "nodes": "pve3", "disable": 1},
                {"rule": "sep", "type": "resource-affinity", "resources": "vm:100,vm:102", "affinity": "negative"},
            ],
        )
        self.assertEqual(ha[100], {"state": "started", "placement": "only", "nodes": ["pve1 (2)", "pve2"]})
        self.assertEqual(ha[101]["placement"], "only")
        self.assertEqual(ha[102], {"state": "stopped", "placement": None, "nodes": []})

    def test_preferred_and_avoided_nodes(self) -> None:
        ha = self.fetch(
            [{"sid": "vm:100", "state": "started"}, {"sid": "vm:101", "state": "started"}],
            rules=[
                {"rule": "a", "type": "node-affinity", "resources": "vm:100", "nodes": "pve1", "strict": 0},
                {
                    "rule": "b",
                    "type": "node-affinity",
                    "resources": "vm:101",
                    "nodes": "pve3",
                    "affinity": "negative",
                    "strict": 1,
                },
            ],
        )
        self.assertEqual((ha[100]["placement"], ha[100]["nodes"]), ("prefer", ["pve1"]))
        self.assertEqual((ha[101]["placement"], ha[101]["nodes"]), ("never", ["pve3"]))

    def test_groups_on_older_pve(self) -> None:
        ha = self.fetch(
            [
                {"sid": "vm:100", "state": "started", "group": "g1"},
                {"sid": "vm:101", "state": "started", "group": "g2"},
            ],
            groups=[{"group": "g1", "nodes": "pve1,pve2", "restricted": 1}, {"group": "g2", "nodes": "pve2:1"}],
        )
        self.assertEqual((ha[100]["placement"], ha[100]["nodes"]), ("only", ["pve1", "pve2"]))
        self.assertEqual((ha[101]["placement"], ha[101]["nodes"]), ("prefer", ["pve2 (1)"]))

    def test_unreadable_is_unknown(self) -> None:
        with mock.patch.object(proxmox, "_pve_get_json", side_effect=proxmox.ProxmoxSyncError("403")):
            self.assertIsNone(proxmox.fetch_ha("https://pve", "a"))


JOB_VMS = [
    {"vmid": 100, "node_name": "pve1", "pool": None},
    {"vmid": 101, "node_name": "pve2", "pool": "db"},
    {"vmid": 102, "node_name": "pve2", "pool": None},
    {"vmid": 103, "node_name": "pve1", "pool": None},
]


class JobStorageTests(unittest.TestCase):
    def test_direct_backup_targets_per_guest(self) -> None:
        jobs = [
            # Like `vzdump --all 1 --exclude 103 --storage pbs-main`.
            {"id": "main", "all": 1, "exclude": "103", "storage": "pbs-main", "schedule": "daily"},
            {"id": "side", "vmid": "103", "storage": "pbs-side", "schedule": "daily"},
            {"id": "pool", "pool": "db", "storage": "pbs-db", "schedule": "sun 03:00"},
            {"id": "node", "all": 1, "node": "pve2", "storage": "nfs-pve2", "schedule": "daily"},
            {"id": "off", "all": 1, "storage": "old", "enabled": 0},
        ]
        result = proxmox.job_storages_by_vmid(JOB_VMS, jobs)
        self.assertEqual(
            result,
            {
                100: ["pbs-main"],
                101: ["nfs-pve2", "pbs-db", "pbs-main"],
                102: ["nfs-pve2", "pbs-main"],
                103: ["pbs-side"],
            },
        )


class TemplateTests(unittest.TestCase):
    def test_templates_are_flagged(self) -> None:
        resources = [
            {"type": "node", "node": "pve1", "status": "online"},
            {"type": "qemu", "vmid": 9000, "node": "pve1", "name": "tpl", "template": 1},
            {"type": "lxc", "vmid": 101, "node": "pve1", "name": "ct"},
        ]

        def fake(base_url, path, auth_header, **kwargs):
            return resources if path.endswith("/cluster/resources") else []

        with (
            mock.patch.object(proxmox, "_pve_get_json", side_effect=fake),
            mock.patch.object(proxmox, "fetch_backup_details", return_value=({}, [], [])),
            mock.patch.object(proxmox, "fetch_qdevice", return_value={}),
            mock.patch.object(proxmox, "fetch_ha", return_value={}),
        ):
            inventory = proxmox.fetch_cluster_inventory(
                {"api_url": "https://pve", "api_user": "u", "token_id": "t", "token_secret": "s"}
            )
        self.assertEqual({vm["vmid"]: vm["is_template"] for vm in inventory.vms}, {9000: True, 101: False})


class HaCallTests(unittest.TestCase):
    def test_pve9_rules_mean_no_groups_call_and_no_retries(self) -> None:
        calls = []

        def fake(base_url, path, auth_header, **kwargs):
            calls.append((path.rsplit("/", 1)[-1], kwargs.get("retries")))
            return []

        with mock.patch.object(proxmox, "_pve_get_json", side_effect=fake):
            proxmox.fetch_ha("https://pve", "a")
        self.assertEqual(calls, [("resources", None), ("rules", 0)])


class BulkFetchNodeTests(unittest.TestCase):
    def test_asks_a_node_where_the_storage_is_active_first(self) -> None:
        asked = []

        def fake(base_url, path, auth_header, **kwargs):
            if path.endswith("/storage"):
                active = 0 if "/pve1/" in path else 1
                return [{"storage": "pbs", "content": "backup", "type": "pbs", "active": active}]
            asked.append((path.split("/")[4], kwargs.get("retries")))
            return []

        nodes = [{"name": "pve1"}, {"name": "pve2"}]
        with mock.patch.object(proxmox, "_pve_get_json", side_effect=fake):
            _latest, _events, errors = fetch_backup_details(
                "https://pve", "a", nodes, [{"vmid": 100}], storage_config=[]
            )
        self.assertEqual(asked, [("pve2", 0)])
        self.assertEqual(errors, [])

    def test_failing_node_falls_through_without_waiting(self) -> None:
        asked = []

        def fake(base_url, path, auth_header, **kwargs):
            if path.endswith("/storage"):
                return [{"storage": "nfs", "content": "backup", "type": "nfs"}]
            asked.append((path.split("/")[4], kwargs.get("retries")))
            if "/pve1/" in path:
                raise proxmox.ProxmoxSyncError("storage 'nfs' is not available on node 'pve1'")
            return []

        nodes = [{"name": "pve1"}, {"name": "pve2"}]
        with mock.patch.object(proxmox, "_pve_get_json", side_effect=fake):
            fetch_backup_details("https://pve", "a", nodes, [{"vmid": 100}], storage_config=[])
        self.assertEqual(asked, [("pve1", 0), ("pve2", 2)])


if __name__ == "__main__":
    unittest.main()
