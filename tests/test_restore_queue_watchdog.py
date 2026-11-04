"""The restore-queue self-heal decision: kick a drain only when runs are queued,
nothing is executing, and the queue isn't paused — so a queue that stalled with
work waiting recovers on its own (no manual button), without double-starting a
running restore or overriding a pause."""

import unittest

from app.web.sync import _should_drain_now


class ShouldDrainNowTests(unittest.TestCase):
    def test_kicks_when_queued_idle_and_not_paused(self) -> None:
        self.assertTrue(_should_drain_now(has_active=False, paused=False, has_queued=True))

    def test_never_kicks_while_a_restore_runs(self) -> None:
        self.assertFalse(_should_drain_now(has_active=True, paused=False, has_queued=True))

    def test_never_kicks_while_paused(self) -> None:
        self.assertFalse(_should_drain_now(has_active=False, paused=True, has_queued=True))

    def test_nothing_to_do_without_queued_runs(self) -> None:
        self.assertFalse(_should_drain_now(has_active=False, paused=False, has_queued=False))


if __name__ == "__main__":
    unittest.main()
