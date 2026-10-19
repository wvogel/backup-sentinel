import unittest
from datetime import UTC, datetime, timedelta

from app.db_governance import _protection_status

NOW = datetime(2026, 10, 19, 8, 0, tzinfo=UTC)


def row(**overrides):
    base = {"vm_gone": False, "copy_count": 2, "has_pbs": True, "last_verified_at": None, "verify_failed_at": None}
    return base | overrides


def verify_status(**overrides):
    return _protection_status(row(**overrides), NOW)[1]


class VerifyStatusTests(unittest.TestCase):
    def test_failed_verify_without_a_later_success_is_critical(self) -> None:
        # srv01-mar-lej: 16.10. verified, 17.10. failed, 18.10. not verified yet.
        self.assertEqual(
            verify_status(last_verified_at=NOW - timedelta(days=3), verify_failed_at=NOW - timedelta(days=2)),
            "critical",
        )

    def test_failed_verify_without_any_success_is_critical(self) -> None:
        self.assertEqual(verify_status(verify_failed_at=NOW - timedelta(days=2)), "critical")

    def test_later_successful_verify_clears_an_earlier_failure(self) -> None:
        self.assertEqual(
            verify_status(last_verified_at=NOW - timedelta(days=1), verify_failed_at=NOW - timedelta(days=2)),
            "ok",
        )

    def test_same_backup_failed_on_one_pbs_and_verified_on_the_other_is_critical(self) -> None:
        both = NOW - timedelta(days=1)
        self.assertEqual(verify_status(last_verified_at=both, verify_failed_at=both), "critical")

    def test_old_successful_verify_is_a_warning(self) -> None:
        self.assertEqual(verify_status(last_verified_at=NOW - timedelta(days=31)), "warning")

    def test_recent_successful_verify_is_ok(self) -> None:
        self.assertEqual(verify_status(last_verified_at=NOW - timedelta(days=1)), "ok")

    def test_no_pbs_has_no_verify_status(self) -> None:
        self.assertIsNone(verify_status(has_pbs=False, verify_failed_at=NOW))
