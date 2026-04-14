import unittest
from datetime import UTC, datetime

from reportlab.lib.styles import ParagraphStyle
from reportlab.platypus import PageBreak, Paragraph, Table

from app.services.pdf_reports import (
    _NOTIFICATION_TYPE_LABELS,
    _notifications_section,
    _styles,
)


def _styles_with_cell() -> dict:
    """_styles() does not define a 'cell' key, but _notifications_section uses it.
    We supply one so the tests can exercise the function with real notifications."""
    st = _styles()
    st["cell"] = ParagraphStyle("cell", fontName="Helvetica", fontSize=8, leading=10)
    return st


class NotificationTypeLabelsTest(unittest.TestCase):
    def test_all_six_types_have_labels(self) -> None:
        for notification_type in (
            "backup_critical",
            "size_anomaly",
            "sync_failed",
            "sync_overdue",
            "restore_overdue",
            "unencrypted_backups",
        ):
            self.assertIn(notification_type, _NOTIFICATION_TYPE_LABELS)

    def test_backup_critical_has_expected_label(self) -> None:
        self.assertEqual(_NOTIFICATION_TYPE_LABELS["backup_critical"], "Backup kritisch")

    def test_size_anomaly_has_expected_label(self) -> None:
        self.assertEqual(_NOTIFICATION_TYPE_LABELS["size_anomaly"], "Größenanomalie")


class NotificationsSectionTest(unittest.TestCase):
    def test_section_contains_page_break_paragraph_spacer_and_table(self) -> None:
        st = _styles_with_cell()
        notifications = [
            {
                "created_at": datetime(2026, 4, 1, 12, 0, tzinfo=UTC),
                "notification_type": "backup_critical",
                "title": "Test",
                "message": "Something broke",
                "cluster_name": "pve-test",
                "channel": "gotify",
            }
        ]

        flowables = _notifications_section(notifications, st)

        self.assertGreater(len(flowables), 0)
        type_names = [type(f).__name__ for f in flowables]
        self.assertIn("PageBreak", type_names)
        self.assertIn("Paragraph", type_names)
        self.assertIn("Spacer", type_names)
        self.assertIn("Table", type_names)

    def test_empty_notifications_returns_header_flowables_without_row_data(self) -> None:
        # In practice the caller guards with "if notifications:", but the
        # function itself should still produce a valid flowable list with an
        # empty table body (just the header row).
        st = _styles_with_cell()
        flowables = _notifications_section([], st)

        self.assertGreater(len(flowables), 0)
        self.assertIsInstance(flowables[0], PageBreak)
        # Last flowable is the Table built from the header row only.
        table = flowables[-1]
        self.assertIsInstance(table, Table)

    def test_known_type_label_appears_in_output(self) -> None:
        st = _styles_with_cell()
        notifications = [
            {
                "created_at": datetime(2026, 4, 1, 12, 0, tzinfo=UTC),
                "notification_type": "sync_failed",
                "title": "t",
                "message": "m",
                "cluster_name": "c",
                "channel": "email",
            }
        ]
        flowables = _notifications_section(notifications, st)
        table = next(f for f in flowables if isinstance(f, Table))
        # Extract the text out of Paragraph cells so we can assert the label.
        rendered_cells: list[str] = []
        for row in table._cellvalues:  # type: ignore[attr-defined]
            for cell in row:
                if isinstance(cell, Paragraph):
                    rendered_cells.append(cell.text)
        self.assertTrue(
            any("Sync fehlgeschlagen" in c for c in rendered_cells),
            f"expected 'Sync fehlgeschlagen' label in table cells, got: {rendered_cells}",
        )

    def test_unknown_type_falls_back_to_raw_type_string(self) -> None:
        st = _styles_with_cell()
        notifications = [
            {
                "created_at": datetime(2026, 4, 1, 12, 0, tzinfo=UTC),
                "notification_type": "totally_unknown_kind",
                "title": "t",
                "message": "m",
                "cluster_name": "c",
                "channel": "gotify",
            }
        ]
        flowables = _notifications_section(notifications, st)
        table = next(f for f in flowables if isinstance(f, Table))
        rendered_cells: list[str] = []
        for row in table._cellvalues:  # type: ignore[attr-defined]
            for cell in row:
                if isinstance(cell, Paragraph):
                    rendered_cells.append(cell.text)
        self.assertTrue(
            any("totally_unknown_kind" in c for c in rendered_cells),
            f"expected raw type name to appear as label, got: {rendered_cells}",
        )

    def test_long_message_is_truncated(self) -> None:
        st = _styles_with_cell()
        long_msg = "x" * 300
        notifications = [
            {
                "created_at": datetime(2026, 4, 1, 12, 0, tzinfo=UTC),
                "notification_type": "backup_critical",
                "title": "t",
                "message": long_msg,
                "cluster_name": "c",
                "channel": "gotify",
            }
        ]
        flowables = _notifications_section(notifications, st)
        table = next(f for f in flowables if isinstance(f, Table))
        rendered_cells: list[str] = []
        for row in table._cellvalues:  # type: ignore[attr-defined]
            for cell in row:
                if isinstance(cell, Paragraph):
                    rendered_cells.append(cell.text)
        self.assertTrue(
            any(c.endswith("...") and len(c) <= 120 for c in rendered_cells),
            f"expected a truncated message ending with '...', got: {rendered_cells}",
        )


if __name__ == "__main__":
    unittest.main()
