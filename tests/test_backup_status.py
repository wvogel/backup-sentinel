import unittest
from datetime import UTC, datetime, timedelta

from app.services.backup_status import (
    assess_backup_status,
    assess_slow_backup,
    format_duration,
    overall_status,
    restore_summary,
    sync_freshness,
)


class AssessBackupStatusTests(unittest.TestCase):
    def test_daily_backup_is_warning_after_36_hours(self) -> None:
        now = datetime(2026, 3, 28, 12, 0, tzinfo=UTC)
        last_backup = now - timedelta(hours=36)

        severity, age_days = assess_backup_status("daily", last_backup, now)

        self.assertEqual(severity, "warning")
        self.assertEqual(age_days, 1)

    def test_weekly_backup_is_critical_after_14_days(self) -> None:
        now = datetime(2026, 3, 28, 12, 0, tzinfo=UTC)
        last_backup = now - timedelta(days=14, hours=12, minutes=1)

        severity, age_days = assess_backup_status("weekly", last_backup, now)

        self.assertEqual(severity, "critical")
        self.assertEqual(age_days, 14)

    def test_weekly_backup_is_warning_before_14_days_12_hours(self) -> None:
        now = datetime(2026, 3, 28, 12, 0, tzinfo=UTC)
        last_backup = now - timedelta(days=14, hours=11, minutes=59)

        severity, age_days = assess_backup_status("weekly", last_backup, now)

        self.assertEqual(severity, "warning")
        self.assertEqual(age_days, 14)

    def test_missing_backup_is_critical(self) -> None:
        severity, age_days = assess_backup_status("daily", None, datetime(2026, 3, 28, tzinfo=UTC))

        self.assertEqual(severity, "critical")
        self.assertIsNone(age_days)

    def test_backup_kind_none_is_not_critical_without_backup(self) -> None:
        severity, age_days = assess_backup_status("none", None, datetime(2026, 3, 28, tzinfo=UTC))

        self.assertEqual(severity, "ok")
        self.assertIsNone(age_days)

    def test_unconfigured_backup_is_critical_without_backup(self) -> None:
        severity, age_days = assess_backup_status("unconfigured", None, datetime(2026, 3, 28, tzinfo=UTC))

        self.assertEqual(severity, "critical")
        self.assertIsNone(age_days)


class SlowBackupTests(unittest.TestCase):
    NOW = datetime(2026, 10, 11, 12, 0, tzinfo=UTC)

    def _progress(self, started_min_ago: int, eta_in_min: int | None = None) -> dict:
        progress = {"started_at": self.NOW - timedelta(minutes=started_min_ago)}
        if eta_in_min is not None:
            progress["eta"] = self.NOW + timedelta(minutes=eta_in_min)
        return progress

    def test_eta_far_beyond_typical_is_slow(self) -> None:
        # Usually 1 h; started 30 min ago, ETA in 90 min → 2 h projected.
        result = assess_slow_backup(self._progress(30, 90), 3600, self.NOW)
        self.assertEqual(result, (3600, 7200))

    def test_normal_runtime_is_not_slow(self) -> None:
        self.assertIsNone(assess_slow_backup(self._progress(30, 40), 3600, self.NOW))

    def test_short_backups_need_15_minutes_extra(self) -> None:
        # Usually 5 min, now 12 min projected: 2.4× but only +7 min → not slow.
        self.assertIsNone(assess_slow_backup(self._progress(10, 2), 300, self.NOW))
        self.assertEqual(assess_slow_backup(self._progress(25), 300, self.NOW), (300, 1500))

    def test_unknown_typical_runtime(self) -> None:
        self.assertIsNone(assess_slow_backup(self._progress(600), None, self.NOW))


class FormatDurationTests(unittest.TestCase):
    def test_formats(self) -> None:
        self.assertEqual(format_duration(20), "1 min")
        self.assertEqual(format_duration(305), "5 min")
        self.assertEqual(format_duration(3 * 3600 + 7 * 60 + 10), "3 h 07 min")
        self.assertEqual(format_duration(2 * 3600 - 10), "2 h 00 min")
        # From 10 hours on the minutes are noise.
        self.assertEqual(format_duration(10 * 3600 - 20), "10 h")
        self.assertEqual(format_duration(185000), "51 h")


class OverallStatusTests(unittest.TestCase):
    def test_worst_of_backup_copies_and_verify(self) -> None:
        self.assertEqual(overall_status("ok", "ok", "ok", vm_gone=False), "ok")
        self.assertEqual(overall_status("ok", "warning", "ok", vm_gone=False), "warning")
        self.assertEqual(overall_status("warning", "ok", "critical", vm_gone=False), "critical")
        self.assertEqual(overall_status("critical", None, None, vm_gone=False), "critical")

    def test_single_backup_location_is_info(self) -> None:
        self.assertEqual(overall_status("ok", "single", "ok", vm_gone=False), "info")
        self.assertEqual(overall_status("warning", "single", "ok", vm_gone=False), "warning")
        self.assertEqual(overall_status("ok", "single", "critical", vm_gone=False), "critical")

    def test_missing_copy_and_verify_info_counts_as_ok(self) -> None:
        self.assertEqual(overall_status("ok", None, None, vm_gone=False), "ok")

    def test_deleted_vm_is_gone(self) -> None:
        self.assertEqual(overall_status("critical", None, None, vm_gone=True), "gone")


class RestoreSummaryTests(unittest.TestCase):
    def test_not_required(self) -> None:
        self.assertEqual(restore_summary("ok", None, None, required=False), ("none", None))

    def test_never_tested_is_open(self) -> None:
        self.assertEqual(restore_summary("open", None, 90, required=True), ("open", None))

    def test_due_in_days(self) -> None:
        self.assertEqual(restore_summary("ok", 20, 90, required=True), ("ok", 70))
        self.assertEqual(restore_summary("warning", 80, 90, required=True), ("warning", 10))

    def test_overdue_by_days(self) -> None:
        self.assertEqual(restore_summary("critical", 100, 90, required=True), ("critical", 10))


if __name__ == "__main__":
    unittest.main()


class SyncFreshnessTests(unittest.TestCase):
    NOW = datetime(2026, 10, 17, 12, 0, tzinfo=UTC)

    def fresh(self, minutes_ago, last_ok=True):
        last = self.NOW - timedelta(minutes=minutes_ago) if minutes_ago is not None else None
        return sync_freshness(last, last_ok, self.NOW, interval_minutes=60)

    def test_levels(self) -> None:
        self.assertEqual(self.fresh(30), "ok")
        self.assertEqual(self.fresh(129), "ok")  # one missed run is tolerated
        self.assertEqual(self.fresh(131), "warning")
        self.assertEqual(self.fresh(30, last_ok=False), "warning")  # latest attempt failed
        self.assertEqual(self.fresh(23 * 60), "warning")
        self.assertEqual(self.fresh(25 * 60), "critical")
        self.assertEqual(self.fresh(None), "critical")
