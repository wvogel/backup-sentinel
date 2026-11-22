"""NIS2 evidence metrics — pure functions, no DB (spec: 2026-11-22-nis2-report-design).

Two headline KPIs:

* **Backup-Lückenlosigkeit** — share of guest-time within the RPO. A *gap* only
  exists when the stretch between two successful backups exceeds RPO + 12 h
  (schedule drift is no gap); then the whole overrun counts, measured from the RPO.
* **Restore-Nachweis** — share of restore-scope guests with a valid restore test.

Percentages are whole numbers, truncated (99.97 → 99): "100" only when nothing
was missed. Colours are judged on that printed number.
"""

from __future__ import annotations

import bisect
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from zoneinfo import ZoneInfo

from app.services.backup_status import cadence_days

TOLERANCE_HOURS = 12
DEFAULT_RPO_HOURS = 24
# Weekly-backed-up guests are near stateless: one successful backup in the
# month is enough. While the month runs, the last one may be this old.
MONTHLY_GRACE_DAYS = 31
WEEKLY_MIN_RESTORE_DAYS = 365

# (green from, amber from) — below amber is red. Whole percent.
RPO_TARGETS = (99, 98)
RESTORE_TARGETS = (90, 80)
DAY_TARGETS = (100, 97)

_RANK = {"ok": 0, "warn": 1, "critical": 2}


def whole_pct(part: int, whole: int) -> int | None:
    """Whole percent, truncated — never rounded up. None for an empty scope."""
    if whole <= 0:
        return None
    return (100 * part) // whole


def exact_pct(part: int, whole: int) -> float | None:
    """Percent truncated to two decimals — for drawing trend lines, never printed."""
    if whole <= 0:
        return None
    return (10000 * part // whole) / 100


def _tone(pct: int | None, targets: tuple[int, int]) -> str | None:
    if pct is None:
        return None
    good, warn = targets
    return "ok" if pct >= good else ("warn" if pct >= warn else "critical")


def rpo_tone(pct: int | None) -> str | None:
    return _tone(pct, RPO_TARGETS)


def restore_tone(pct: int | None) -> str | None:
    return _tone(pct, RESTORE_TARGETS)


def day_tone(pct: int | None) -> str | None:
    return _tone(pct, DAY_TARGETS)


def verdict(rpo_pct: int | None, restore_pct: int | None, *, open_gap: bool) -> str:
    """Worse of the two KPIs; a guest outside its RPO at the end forces at least amber."""
    tones = [t for t in (rpo_tone(rpo_pct), restore_tone(restore_pct)) if t]
    if open_gap:
        tones.append("warn")
    return max(tones, key=_RANK.__getitem__, default="ok")


def effective_rpo_hours(rpo_hours: int | None, backup_kind: str) -> int:
    """RPO of a guest measured by the hour (weekly guests use monthly_coverage)."""
    return rpo_hours or DEFAULT_RPO_HOURS


def restore_interval_days(cadence: str, backup_kind: str) -> int:
    days = cadence_days(cadence)
    return max(days, WEEKLY_MIN_RESTORE_DAYS) if backup_kind == "weekly" else days


def restore_state(test_dates: list[date], interval_days: int, at: date) -> str:
    """ "valid", "overdue" or "never" at date `at` (tests after `at` don't count).
    Same rule as the governance due-status: age >= interval is overdue."""
    past = [d for d in test_dates if d <= at]
    if not past:
        return "never"
    return "valid" if (at - max(past)).days < interval_days else "overdue"


@dataclass(frozen=True)
class Gap:
    start: datetime  # first uncovered moment inside the window
    end: datetime  # last uncovered moment inside the window (exclusive)
    prev_backup: datetime | None  # None: never backed up before
    next_backup: datetime | None  # None: still no backup ("offen")
    over_seconds: int  # whole overrun beyond the RPO, also outside the window
    rpo_hours: int  # 0 for a monthly gap
    monthly: bool = False  # weekly guest: no backup in the whole month

    @property
    def open(self) -> bool:
        return self.next_backup is None


@dataclass
class Coverage:
    window_seconds: int = 0
    uncovered_seconds: int = 0
    gaps: list[Gap] = field(default_factory=list)


def overlap_seconds(a0: datetime, a1: datetime, b0: datetime, b1: datetime) -> int:
    return max(0, int((min(a1, b1) - max(a0, b0)).total_seconds()))


def guest_coverage(
    backups: list[datetime],
    rpo_hours: int,
    win_start: datetime,
    win_end: datetime,
    *,
    tolerance_hours: int = TOLERANCE_HOURS,
) -> Coverage:
    """Coverage of one guest in [win_start, win_end).

    `backups`: start times of successful backups (pruned ones included), any
    order; the latest one before the window and the first one after it matter
    too. A guest without a backup before the window starts its first stretch at
    `win_start` (it was first seen then). A stretch still open is judged by its
    age at `win_end`.
    """
    cov = Coverage()
    if win_end <= win_start:
        return cov
    cov.window_seconds = int((win_end - win_start).total_seconds())
    pts = sorted(set(backups))
    rpo = timedelta(hours=rpo_hours)
    limit = rpo + timedelta(hours=tolerance_hours)

    i = bisect.bisect_right(pts, win_start)
    prev: datetime | None = pts[i - 1] if i else None
    anchor = prev or win_start
    for nxt in [*pts[i:], None]:
        stretch_end = nxt or win_end
        if (stretch_end - anchor) > limit:
            start, end = max(anchor + rpo, win_start), min(stretch_end, win_end)
            if end > start:
                cov.uncovered_seconds += int((end - start).total_seconds())
                cov.gaps.append(
                    Gap(start, end, prev, nxt, int((stretch_end - anchor - rpo).total_seconds()), rpo_hours)
                )
        if nxt is None or nxt >= win_end:
            break
        prev = anchor = nxt
    return cov


def monthly_coverage(
    backups: list[datetime],
    win_start: datetime,
    win_end: datetime,
    *,
    interim: bool,
    grace_days: int = MONTHLY_GRACE_DAYS,
) -> Coverage:
    """Coverage of a weekly-backed-up guest: the whole window counts as covered
    when it has a successful backup — otherwise it is one gap over the window.
    While the month still runs (`interim`), a last backup no older than
    `grace_days` also counts: the month can still bring one."""
    cov = Coverage()
    if win_end <= win_start:
        return cov
    cov.window_seconds = int((win_end - win_start).total_seconds())
    pts = sorted(set(backups))
    if any(win_start <= b < win_end for b in pts):
        return cov
    before = [b for b in pts if b < win_start]
    prev = before[-1] if before else None
    if interim and prev is not None and win_end - prev <= timedelta(days=grace_days):
        return cov
    nxt = next((b for b in pts if b >= win_end), None)
    cov.uncovered_seconds = cov.window_seconds
    cov.gaps.append(Gap(win_start, win_end, prev, nxt, cov.window_seconds, 0, monthly=True))
    return cov


def month_bounds(month: date, tz_name: str) -> tuple[datetime, datetime]:
    """[first of month, first of next month) in local time, as UTC."""
    tz = ZoneInfo(tz_name)
    start = datetime(month.year, month.month, 1, tzinfo=tz)
    nxt = date(month.year + (month.month == 12), month.month % 12 + 1, 1)
    end = datetime(nxt.year, nxt.month, 1, tzinfo=tz)
    return start.astimezone(UTC), end.astimezone(UTC)


def last_months(month: date, n: int) -> list[date]:
    """The n calendar months ending with `month` (firsts of month), oldest first."""
    out = []
    y, mo = month.year, month.month
    for _ in range(n):
        out.append(date(y, mo, 1))
        y, mo = (y, mo - 1) if mo > 1 else (y - 1, 12)
    return out[::-1]
