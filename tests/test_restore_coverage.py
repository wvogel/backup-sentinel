import unittest

from app.db_governance import restore_test_coverage


def _row(status, tested=True, required=True, kind="daily"):
    return {
        "restore_required": required,
        "status_backup_kind": kind,
        "last_restore_tested_at": "2026-10-01" if tested else None,
        "restore_due_status": status,
    }


class RestoreCoverageTest(unittest.TestCase):
    def test_percent_is_truncated_never_rounded_up(self):
        rows = [_row("ok")] * 1999 + [_row("open", tested=False)]
        self.assertEqual(restore_test_coverage(rows)["pct"], 99)  # 99.95, not 100

    def test_heads_up_before_expiry_still_counts_as_covered(self):
        rows = [_row("ok")] * 84 + [_row("warning")] * 2 + [_row("open", tested=False)] * 2
        cov = restore_test_coverage(rows)
        self.assertEqual((cov["ok"], cov["required"], cov["overdue"]), (86, 88, 2))
        self.assertEqual(int(cov["pct"]), 97)

    def test_expired_and_untested_are_not_covered(self):
        rows = [_row("ok"), _row("critical"), _row("open", tested=False), _row("ok", required=False)]
        cov = restore_test_coverage(rows)
        self.assertEqual((cov["ok"], cov["required"], cov["overdue"]), (1, 3, 2))


if __name__ == "__main__":
    unittest.main()
