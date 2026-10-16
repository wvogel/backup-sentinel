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


if __name__ == "__main__":
    unittest.main()


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
