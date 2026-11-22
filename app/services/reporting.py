from __future__ import annotations

from datetime import UTC, date, datetime

from app.services.backup_status import RESTORE_DUE_STATUSES


def serialize_report_payload(value):
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, dict):
        return {key: serialize_report_payload(item) for key, item in value.items()}
    if isinstance(value, list):
        return [serialize_report_payload(item) for item in value]
    return value


def build_compliance_summary(governance_rows: list[dict], restore_tests: list[dict]) -> dict:
    now = datetime.now(UTC)
    return {
        "restore_required": sum(1 for row in governance_rows if row["restore_required"]),
        "restore_overdue": sum(1 for row in governance_rows if row["restore_due_status"] in RESTORE_DUE_STATUSES),
        "restore_critical": sum(1 for row in governance_rows if row["restore_due_status"] == "critical"),
        "tested_this_month": sum(
            1 for row in restore_tests if (row["tested_at"].year, row["tested_at"].month) == (now.year, now.month)
        ),
    }
