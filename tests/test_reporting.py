import unittest
from datetime import UTC, date, datetime

from app.services.reporting import build_compliance_summary, serialize_report_payload


class ReportingTests(unittest.TestCase):
    def test_serialize_report_payload_converts_nested_dates(self) -> None:
        payload = {
            "generated_at": datetime(2026, 3, 28, 10, 15, tzinfo=UTC),
            "report_month": date(2026, 3, 1),
            "rows": [{"started_at": datetime(2026, 3, 27, 9, 0, tzinfo=UTC)}],
        }

        serialized = serialize_report_payload(payload)

        self.assertEqual(serialized["generated_at"], "2026-03-28T10:15:00+00:00")
        self.assertEqual(serialized["report_month"], "2026-03-01")
        self.assertEqual(serialized["rows"][0]["started_at"], "2026-03-27T09:00:00+00:00")

    def test_build_compliance_summary_counts_expected_rows(self) -> None:
        governance_rows = [
            {"restore_required": True, "restore_due_status": "warning"},
            {"restore_required": True, "restore_due_status": "critical"},
            {"restore_required": True, "restore_due_status": "open"},
            {"restore_required": False, "restore_due_status": "ok"},
        ]
        restore_tests = [
            {"tested_at": datetime.now(UTC)},
            {"tested_at": datetime(2026, 1, 10, tzinfo=UTC)},
        ]

        summary = build_compliance_summary(governance_rows, restore_tests)

        self.assertEqual(summary["restore_required"], 3)
        # The heads-up ("warning", due in N days) is not due yet.
        self.assertEqual(summary["restore_overdue"], 2)
        self.assertEqual(summary["restore_critical"], 1)
        self.assertGreaterEqual(summary["tested_this_month"], 1)

    def test_tests_from_the_same_month_last_year_do_not_count(self) -> None:
        now = datetime.now(UTC)
        restore_tests = [{"tested_at": now}, {"tested_at": now.replace(year=now.year - 1, day=1)}]

        summary = build_compliance_summary([], restore_tests)

        self.assertEqual(summary["tested_this_month"], 1)


if __name__ == "__main__":
    unittest.main()
