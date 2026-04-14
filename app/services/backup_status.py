from __future__ import annotations

from datetime import UTC, datetime


def assess_backup_status(
    backup_kind: str, last_backup_at: datetime | None, now: datetime | None = None
) -> tuple[str, int | None]:
    if backup_kind == "none":
        return "ok", None

    if backup_kind == "unconfigured":
        backup_kind = "daily"

    if last_backup_at is None:
        return "critical", None

    reference = now or datetime.now(UTC)
    delta = reference - last_backup_at
    age_hours = delta.total_seconds() / 3600
    age_days = max(delta.days, 0)

    if backup_kind == "daily":
        if age_hours >= 48:  # 2 Tage → kritisch
            return "critical", age_days
        if age_hours >= 36:  # 36 Stunden → Warnung
            return "warning", age_days
        return "ok", age_days

    if backup_kind == "weekly":
        if age_hours >= 14 * 24 + 12:  # 14 Tage + 12h → kritisch
            return "critical", age_days
        if age_hours >= 7 * 24 + 12:  # 7 Tage + 12h → Warnung
            return "warning", age_days
        return "ok", age_days

    return "unknown", age_days
