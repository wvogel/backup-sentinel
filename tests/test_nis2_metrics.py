import unittest
from datetime import UTC, date, datetime, timedelta

from app.services import nis2_metrics as m

S = datetime(2026, 10, 1, tzinfo=UTC)
E = datetime(2026, 11, 1, tzinfo=UTC)
H = timedelta(hours=1)


def daily(start: datetime, end: datetime, hour: int = 21, skip: set[date] | None = None) -> list[datetime]:
    out, d = [], start.replace(hour=hour, minute=0)
    while d < end:
        if not skip or d.date() not in skip:
            out.append(d)
        d += timedelta(days=1)
    return out


class WholePctTest(unittest.TestCase):
    def test_truncates_never_rounds_up(self):
        self.assertEqual(m.whole_pct(9997, 10000), 99)
        self.assertEqual(m.whole_pct(9999999, 10000000), 99)
        self.assertEqual(m.whole_pct(10, 10), 100)
        self.assertEqual(m.whole_pct(0, 10), 0)

    def test_empty_scope_is_none(self):
        self.assertIsNone(m.whole_pct(0, 0))
        self.assertIsNone(m.exact_pct(0, 0))

    def test_exact_pct_truncates_to_hundredths(self):
        self.assertEqual(m.exact_pct(99999, 100000), 99.99)
        self.assertEqual(m.exact_pct(2, 3), 66.66)


class EffectiveRpoTest(unittest.TestCase):
    def test_daily_and_unconfigured_use_rpo(self):
        self.assertEqual(m.effective_rpo_hours(24, "daily"), 24)
        self.assertEqual(m.effective_rpo_hours(None, "unconfigured"), 24)


class MonthlyCoverageTest(unittest.TestCase):
    """Weekly-backed-up guests: one successful backup in the month is enough."""

    def test_one_backup_in_the_month_covers_it_all(self):
        cov = m.monthly_coverage([S - 20 * 24 * H, S + 25 * 24 * H], S, E, interim=False)
        self.assertEqual((cov.uncovered_seconds, cov.gaps), (0, []))
        self.assertEqual(cov.window_seconds, int((E - S).total_seconds()))

    def test_no_backup_in_the_month_is_one_gap_over_the_whole_month(self):
        prev, nxt = S - 3 * 24 * H, E + 2 * 24 * H
        cov = m.monthly_coverage([prev, nxt], S, E, interim=False)
        self.assertEqual(cov.uncovered_seconds, int((E - S).total_seconds()))
        self.assertEqual(len(cov.gaps), 1)
        g = cov.gaps[0]
        self.assertEqual((g.start, g.end, g.prev_backup, g.next_backup), (S, E, prev, nxt))
        self.assertTrue(g.monthly)
        self.assertFalse(g.open)

    def test_never_backed_up_weekly_guest_is_an_open_gap(self):
        cov = m.monthly_coverage([], S, E, interim=False)
        self.assertTrue(cov.gaps[0].open)

    def test_interim_without_backup_yet_is_fine_while_the_last_is_within_31_days(self):
        now = S + 10 * 24 * H
        self.assertEqual(m.monthly_coverage([S - 15 * 24 * H], S, now, interim=True).gaps, [])
        late = m.monthly_coverage([S - 25 * 24 * H], S, now, interim=True)
        self.assertEqual(len(late.gaps), 1)


class GuestCoverageTest(unittest.TestCase):
    def test_daily_backups_with_drift_have_no_gap(self):
        # 21:00, next day 21:04 ... drift within tolerance is no gap
        backups = [S - timedelta(hours=3)] + [b + timedelta(minutes=4 * i) for i, b in enumerate(daily(S, E))]
        cov = m.guest_coverage(backups, 24, S, E)
        self.assertEqual(cov.gaps, [])
        self.assertEqual(cov.uncovered_seconds, 0)
        self.assertEqual(cov.window_seconds, int((E - S).total_seconds()))

    def test_30h_apart_is_not_a_gap(self):
        b0 = S - 2 * H
        backups = [b0, b0 + 30 * H, *daily(S + 24 * H, E)]
        self.assertEqual(m.guest_coverage(backups, 24, S, E).gaps, [])

    def test_46h_apart_is_a_22h_gap_counted_from_rpo(self):
        prev = datetime(2026, 10, 11, 22, 0, tzinfo=UTC)
        nxt = prev + 46 * H
        backups = sorted([b for b in daily(S - 24 * H, E, 22) if not prev < b < nxt] + [nxt])
        cov = m.guest_coverage(backups, 24, S, E)
        self.assertEqual(len(cov.gaps), 1)
        g = cov.gaps[0]
        self.assertEqual((g.prev_backup, g.next_backup), (prev, nxt))
        self.assertEqual(g.start, prev + 24 * H)
        self.assertEqual(g.end, nxt)
        self.assertEqual(g.over_seconds, 22 * 3600)
        self.assertFalse(g.open)
        self.assertEqual(cov.uncovered_seconds, 22 * 3600)

    def test_gap_from_previous_month_is_clipped_to_window(self):
        prev = S - 30 * H  # gap starts 6 h before S
        nxt = S + 20 * H
        backups = [prev, nxt, *daily(S + 24 * H, E)]
        cov = m.guest_coverage(backups, 24, S, E)
        self.assertEqual(len(cov.gaps), 1)
        self.assertEqual(cov.gaps[0].start, S)
        self.assertEqual(cov.uncovered_seconds, 20 * 3600)
        # hours over RPO are the whole overrun, also the part before S
        self.assertEqual(cov.gaps[0].over_seconds, 26 * 3600)

    def test_open_gap_at_period_end(self):
        last = E - 51 * H
        backups = [b for b in daily(S - 24 * H, E) if b <= last]
        self.assertEqual(backups[-1], last)
        cov = m.guest_coverage(backups, 24, S, E)
        self.assertEqual(len(cov.gaps), 1)
        self.assertTrue(cov.gaps[0].open)
        self.assertIsNone(cov.gaps[0].next_backup)
        self.assertEqual(cov.gaps[0].end, E)
        self.assertEqual(cov.uncovered_seconds, 27 * 3600)

    def test_open_stretch_within_tolerance_is_no_gap(self):
        backups = daily(S - 24 * H, E - 26 * H)  # last one 27 h before E: within RPO + 12 h
        last = backups[-1]
        self.assertLess(E - last, 36 * H)
        self.assertEqual(m.guest_coverage(backups, 24, S, E).gaps, [])

    def test_never_backed_up_guest_is_uncovered_from_first_seen(self):
        seen = S + 10 * 24 * H
        cov = m.guest_coverage([], 24, seen, E)
        self.assertEqual(cov.window_seconds, int((E - seen).total_seconds()))
        self.assertEqual(cov.uncovered_seconds, int((E - seen - 24 * H).total_seconds()))
        self.assertTrue(cov.gaps[0].open)
        self.assertIsNone(cov.gaps[0].prev_backup)

    def test_backups_after_window_end_are_used_only_as_next(self):
        last = E - 27 * H  # 21:00 two days before the end
        backups = [b for b in daily(S - 24 * H, E) if b <= last] + [E + 10 * H]
        self.assertEqual(backups[-2], last)
        cov = m.guest_coverage(backups, 24, S, E)
        self.assertEqual(len(cov.gaps), 1)
        self.assertEqual(cov.gaps[0].end, E)
        self.assertFalse(cov.gaps[0].open)
        self.assertEqual(cov.gaps[0].next_backup, E + 10 * H)
        self.assertEqual(cov.uncovered_seconds, 3 * 3600)

    def test_empty_window(self):
        cov = m.guest_coverage([S], 24, E, E)
        self.assertEqual((cov.window_seconds, cov.uncovered_seconds, cov.gaps), (0, 0, []))


class DayCoverageTest(unittest.TestCase):
    def test_overlap_seconds(self):
        d0 = datetime(2026, 10, 12, tzinfo=UTC)
        self.assertEqual(m.overlap_seconds(d0, d0 + 24 * H, d0 - H, d0 + 2 * H), 2 * 3600)
        self.assertEqual(m.overlap_seconds(d0, d0 + H, d0 + 2 * H, d0 + 3 * H), 0)

    def test_day_tone(self):
        self.assertEqual(m.day_tone(100), "ok")
        self.assertEqual(m.day_tone(99), "warn")
        self.assertEqual(m.day_tone(97), "warn")
        self.assertEqual(m.day_tone(96), "critical")
        self.assertIsNone(m.day_tone(None))


class RestoreStateTest(unittest.TestCase):
    def test_states(self):
        at = date(2026, 10, 31)
        self.assertEqual(m.restore_state([], 182, at), "never")
        self.assertEqual(m.restore_state([date(2026, 6, 1)], 182, at), "valid")
        self.assertEqual(m.restore_state([date(2026, 4, 1)], 182, at), "overdue")
        # age == interval is overdue (same rule as the governance due-status)
        self.assertEqual(m.restore_state([at - timedelta(days=182)], 182, at), "overdue")

    def test_tests_after_reference_date_do_not_count(self):
        self.assertEqual(m.restore_state([date(2026, 11, 5)], 182, date(2026, 10, 31)), "never")
        self.assertEqual(m.restore_state([date(2026, 1, 5), date(2026, 11, 5)], 182, date(2026, 10, 31)), "overdue")

    def test_restore_interval(self):
        self.assertEqual(m.restore_interval_days("half_year", "daily"), 182)
        self.assertEqual(m.restore_interval_days("month", "weekly"), 365)


class ToneAndVerdictTest(unittest.TestCase):
    def test_tone_thresholds(self):
        self.assertEqual(m.rpo_tone(100), "ok")
        self.assertEqual(m.rpo_tone(99), "ok")
        self.assertEqual(m.rpo_tone(98), "warn")
        self.assertEqual(m.rpo_tone(97), "critical")
        self.assertEqual(m.restore_tone(90), "ok")
        self.assertEqual(m.restore_tone(89), "warn")
        self.assertEqual(m.restore_tone(80), "warn")
        self.assertEqual(m.restore_tone(79), "critical")
        self.assertIsNone(m.rpo_tone(None))

    def test_verdict_is_worst(self):
        self.assertEqual(m.verdict(100, 95, open_gap=False), "ok")
        self.assertEqual(m.verdict(99, 85, open_gap=False), "warn")
        self.assertEqual(m.verdict(98, 70, open_gap=False), "critical")
        self.assertEqual(m.verdict(None, None, open_gap=False), "ok")

    def test_open_gap_forces_at_least_warn(self):
        self.assertEqual(m.verdict(100, 100, open_gap=True), "warn")
        self.assertEqual(m.verdict(100, 50, open_gap=True), "critical")


class MonthTest(unittest.TestCase):
    def test_month_bounds_in_local_time(self):
        s, e = m.month_bounds(date(2026, 10, 1), "Europe/Berlin")
        self.assertEqual(s, datetime(2026, 9, 30, 22, tzinfo=UTC))
        self.assertEqual(e, datetime(2026, 10, 31, 23, tzinfo=UTC))  # DST ends 25.10.

    def test_last_months(self):
        self.assertEqual(m.last_months(date(2026, 2, 1), 3), [date(2025, 12, 1), date(2026, 1, 1), date(2026, 2, 1)])


if __name__ == "__main__":
    unittest.main()
