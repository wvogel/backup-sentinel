"""Pure tests for the reachability-window countdown on the live restore card."""

from datetime import UTC, datetime, timedelta

from app.web.common import restore_probe_countdown

START = datetime(2026, 11, 15, 8, 0, tzinfo=UTC)


def _run(**over):
    base = {
        "status": "running",
        "phase": "probing",
        "probe_started_at": START,
        "probe_ends_at": START + timedelta(minutes=10),
    }
    base.update(over)
    return base


def test_counts_down_and_fills_the_bar():
    cd = restore_probe_countdown(_run(), now=START + timedelta(minutes=2, seconds=30))
    assert cd == {"remaining": 450, "total": 600, "text": "7:30", "pct": 25.0}


def test_also_runs_while_booting():
    assert restore_probe_countdown(_run(phase="booting"), now=START)["text"] == "10:00"


def test_hours_get_hh_mm_ss():
    run = _run(probe_ends_at=START + timedelta(hours=2))
    assert restore_probe_countdown(run, now=START + timedelta(seconds=1))["text"] == "1:59:59"


def test_never_below_zero():
    cd = restore_probe_countdown(_run(), now=START + timedelta(minutes=11))
    assert (cd["remaining"], cd["text"], cd["pct"]) == (0, "0:00", 100.0)


def test_none_outside_the_window():
    assert restore_probe_countdown(_run(phase="restoring"), now=START) is None
    assert restore_probe_countdown(_run(status="passed"), now=START) is None
    assert restore_probe_countdown(_run(probe_ends_at=None), now=START) is None
