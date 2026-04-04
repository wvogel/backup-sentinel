import unittest
from datetime import UTC, datetime, timedelta

from app.services.backup_status import assess_backup_status


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


if __name__ == "__main__":
    unittest.main()
