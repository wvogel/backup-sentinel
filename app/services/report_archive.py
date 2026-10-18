"""Write a backup report as PDF and JSON and archive it — shared by the button and the monthly run."""

from __future__ import annotations

import json
import logging
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

from app import db
from app.config import REPORT_DIR, REPORT_LANGUAGE
from app.db_notifications import list_notification_logs_for_period
from app.services.pdf_reports import write_backup_period_pdf
from app.services.reporting import build_backup_period_report, build_month_comparison, serialize_report_payload
from app.services.theme_store import active_theme

logger = logging.getLogger(__name__)


def generate_archived_report(
    month: date, period_label: str, notifications_until: datetime, trigger_mode: str, now: datetime
) -> Path:
    """Report over all recoverable backups, compared with `month` and the month before; returns the PDF path."""
    events = db.list_all_report_backup_events()
    report = build_backup_period_report(datetime.min.replace(tzinfo=UTC), now, events)
    report["period_label"] = period_label
    try:
        prev_month = (month - timedelta(days=1)).replace(day=1)
        report["comparison"] = build_month_comparison(
            db.backup_events_for_month(month.year, month.month),
            db.backup_events_for_month(prev_month.year, prev_month.month),
            now,
        )
    except Exception as exc:
        logger.warning("Month comparison failed: %s", exc)
    try:
        notifications = list_notification_logs_for_period(
            datetime(month.year, month.month, 1, tzinfo=UTC), notifications_until
        )
    except Exception:
        notifications = []

    stem = f"backup-report-{month:%Y-%m}-{report['generated_at']:%Y%m%d-%H%M%S}"
    pdf_path = REPORT_DIR / f"{stem}.pdf"
    json_path = REPORT_DIR / f"{stem}.json"
    write_backup_period_pdf(
        report, pdf_path, notifications=notifications, lang=REPORT_LANGUAGE, theme_colors=active_theme()[1]
    )
    json_path.write_text(json.dumps(serialize_report_payload(report), ensure_ascii=False, indent=2), encoding="utf-8")
    db.archive_monthly_report(month, trigger_mode, pdf_path, json_path, report)
    return pdf_path
