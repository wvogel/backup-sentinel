"""Pure tests for the running-restore ETA string."""

from datetime import UTC, datetime, timedelta

from app.web.common import restore_eta_text

START = datetime(2026, 11, 2, 8, 0, tzinfo=UTC)


def _run(**over):
    base = {"phase": "restoring", "percent": 25, "started_at": START}
    base.update(over)
    return base


def test_eta_from_elapsed_and_percent():
    # 10 min elapsed at 25% → 30 min remaining.
    now = START + timedelta(minutes=10)
    assert restore_eta_text(_run(percent=25), now=now) == "~30 min"


def test_eta_hours_format():
    # 30 min elapsed at 20% → 120 min (2 h) remaining.
    now = START + timedelta(minutes=30)
    assert restore_eta_text(_run(percent=20), now=now) == "~2 h 00 min"


def test_no_eta_outside_restoring_phase():
    now = START + timedelta(minutes=10)
    assert restore_eta_text(_run(phase="finalizing"), now=now) == ""
    assert restore_eta_text(_run(phase="probing"), now=now) == ""


def test_no_eta_without_progress_or_start():
    now = START + timedelta(minutes=10)
    assert restore_eta_text(_run(percent=0), now=now) == ""
    assert restore_eta_text(_run(percent=100), now=now) == ""
    assert restore_eta_text(_run(started_at=None), now=now) == ""


def test_no_eta_when_clock_not_advanced():
    assert restore_eta_text(_run(percent=25), now=START) == ""  # elapsed 0


def test_naive_started_at_is_treated_as_utc():
    now = START + timedelta(minutes=10)
    assert restore_eta_text(_run(started_at=START.replace(tzinfo=None)), now=now) == "~30 min"
