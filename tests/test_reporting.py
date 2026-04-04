import unittest
from datetime import UTC, date, datetime

from app.services.reporting import (
    build_backup_period_report,
    build_compliance_summary,
    filter_reportable_backup_events,
    serialize_report_payload,
)


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
            {"restore_required": False, "restore_due_status": "ok"},
        ]
        restore_tests = [
            {"tested_at": datetime.now(UTC)},
            {"tested_at": datetime(2026, 1, 10, tzinfo=UTC)},
        ]

        summary = build_compliance_summary(governance_rows, restore_tests)

        self.assertEqual(summary["restore_required"], 2)
        self.assertEqual(summary["restore_overdue"], 2)
        self.assertEqual(summary["restore_critical"], 1)
        self.assertGreaterEqual(summary["tested_this_month"], 1)

    def test_filter_reportable_backup_events_keeps_recent_removed(self) -> None:
        reference = datetime(2026, 3, 29, 12, 0, tzinfo=UTC)
        events = [
            {"started_at": datetime(2026, 3, 10, 8, 0, tzinfo=UTC), "removed_at": date(2026, 3, 20), "status": "ok"},
            {"started_at": datetime(2026, 2, 20, 8, 0, tzinfo=UTC), "removed_at": date(2026, 3, 1), "status": "ok"},
            {"started_at": datetime(2026, 3, 28, 8, 0, tzinfo=UTC), "removed_at": None, "status": "ok"},
        ]

        filtered = filter_reportable_backup_events(events, reference)

        self.assertEqual(len(filtered), 2)
        self.assertEqual(filtered[0]["status_for_report"], "deleted")
        self.assertEqual(filtered[1]["status_for_report"], "ok")

    def test_build_backup_period_report_counts_deleted_separately(self) -> None:
        reference = datetime(2026, 3, 29, 12, 0, tzinfo=UTC)
        events = [
            {
                "cluster_name": "Cluster A",
                "vm_name": "VM 1",
                "node_name": "node1",
                "started_at": datetime(2026, 3, 28, 8, 0, tzinfo=UTC),
                "removed_at": None,
                "status": "ok",
            },
            {
                "cluster_name": "Cluster A",
                "vm_name": "VM 1",
                "node_name": "node1",
                "started_at": datetime(2026, 3, 5, 8, 0, tzinfo=UTC),
                "removed_at": date(2026, 3, 10),
                "status": "ok",
            },
        ]

        report = build_backup_period_report(datetime(2026, 1, 1, tzinfo=UTC), reference, events)

        self.assertEqual(report["totals"]["events"], 2)
        self.assertEqual(report["totals"]["ok"], 1)
        self.assertEqual(report["totals"]["deleted"], 1)
        self.assertEqual(report["rows"][0]["deleted_count"], 1)


if __name__ == "__main__":
    unittest.main()
