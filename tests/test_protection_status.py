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


def copy_status(**overrides):
    return _protection_status(row(**overrides), NOW)[0]


class CopyStatusTests(unittest.TestCase):
    def test_two_current_copies_are_ok(self) -> None:
        self.assertEqual(copy_status(copy_count=2, copy_current=2), "ok")

    def test_stale_second_copy_still_counts_but_warns(self) -> None:
        self.assertEqual(copy_status(copy_count=2, copy_current=1), "warning")

    def test_one_server_is_single(self) -> None:
        self.assertEqual(copy_status(copy_count=1, copy_current=1), "single")

    def test_no_copy_has_no_status(self) -> None:
        self.assertIsNone(copy_status(copy_count=0, copy_current=0))


class RestoreDotTests(unittest.TestCase):
    def dot(self, **row):
        from app.db_governance import restore_dot_result

        return restore_dot_result(row)

    def test_documented_test_after_a_cancelled_run_is_green(self) -> None:
        # zeiss-aboe-muc: cancelled 06.11., documented by hand as passed 15.11.
        self.assertEqual(
            self.dot(
                last_restore_run_result="cancelled",
                last_restore_run_at=datetime(2026, 11, 6, 22, 42, tzinfo=UTC),
                last_restore_result="passed",
                last_restore_tested_at=datetime(2026, 11, 15).date(),
            ),
            "passed",
        )

    def test_failed_run_after_the_last_test_shows_the_failure(self) -> None:
        self.assertEqual(
            self.dot(
                last_restore_run_result="failed",
                last_restore_run_at=datetime(2026, 11, 10, tzinfo=UTC),
                last_restore_result="passed",
                last_restore_tested_at=datetime(2026, 11, 3).date(),
            ),
            "failed",
        )

    def test_only_a_cancelled_run_leaves_the_dot_open(self) -> None:
        self.assertIsNone(
            self.dot(last_restore_run_result="cancelled", last_restore_run_at=datetime(2026, 11, 6, tzinfo=UTC))
        )
