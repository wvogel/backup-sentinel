import unittest
from datetime import UTC, datetime

from app.services.proxmox import (
    classify_schedule,
    infer_backup_kind_by_vmid,
)
from app.services.proxmox_progress import _parse_running_backup_progress


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


if __name__ == "__main__":
    unittest.main()
