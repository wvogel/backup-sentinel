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


def format_duration(seconds: int) -> str:
    """Compact duration: "5 min" or "51 h 23 min"."""
    if seconds < 3600:
        return f"{max(1, round(seconds / 60))} min"
    hours, rest = divmod(seconds, 3600)
    return f"{hours} h {rest // 60:02d} min"


SLOW_FACTOR = 1.5
SLOW_MIN_EXTRA_SECONDS = 15 * 60


def assess_slow_backup(
    progress: dict, typical_seconds: int | None, now: datetime | None = None
) -> tuple[int, int] | None:
    """Return (typical, projected) seconds if a running backup is unusually slow.

    Projected runtime is ETA − start when known, else the runtime so far. Slow
    means more than 1.5× the typical runtime AND at least 15 minutes longer, so
    short backups don't trip over a few seconds of jitter.
    """
    started_at = progress.get("started_at")
    if not typical_seconds or started_at is None:
        return None
    reference = now or datetime.now(UTC)
    elapsed = (reference - started_at).total_seconds()
    eta = progress.get("eta")
    projected = max(elapsed, (eta - started_at).total_seconds() if eta else 0)
    threshold = max(typical_seconds * SLOW_FACTOR, typical_seconds + SLOW_MIN_EXTRA_SECONDS)
    return (typical_seconds, int(projected)) if projected > threshold else None
