"""Pure tests for the running-restore ETA (a point in time) and the disk-bar fills."""

from datetime import UTC, datetime, timedelta

from app.i18n import t
from app.web.common import restore_disk_fills, restore_eta_text

START = datetime(2026, 11, 2, 8, 0, tzinfo=UTC)


def _run(**over):
    base = {"phase": "restoring", "percent": 25, "started_at": START}
    base.update(over)
    return base


def _until(hhmm):
    return t("cluster.running_until", time=hhmm)


def test_eta_is_a_point_in_time():
    # 10 min elapsed at 25% → 30 min remaining → 08:40 UTC = 09:40 in Berlin.
    now = START + timedelta(minutes=10)
    assert restore_eta_text(_run(percent=25), now=now) == _until("09:40")


def test_eta_on_another_day_names_the_day():
    # 30 min elapsed at 2% → 1470 min remaining → next day 09:00 UTC = 10:00 Berlin.
    now = START + timedelta(minutes=30)
    assert restore_eta_text(_run(percent=2), now=now) == _until("03.11. 10:00")


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
    assert restore_eta_text(_run(started_at=START.replace(tzinfo=None)), now=now) == _until("09:40")


DISKS = [{"dev": "scsi0", "size_bytes": 100}, {"dev": "scsi1", "size_bytes": 300}]


def test_disk_fills_follow_the_per_disk_progress():
    run = {"status": "running", "phase": "restoring", "disks": DISKS, "disk_progress": {"scsi0": 100, "scsi1": 42}}
    assert restore_disk_fills(run) == {"scsi0": 100, "scsi1": 42}
    # A disk not reached yet is empty.
    assert restore_disk_fills({**run, "disk_progress": {"scsi0": 60}}) == {"scsi0": 60, "scsi1": 0}


def test_disk_fills_single_disk_falls_back_to_the_run_percent():
    run = {"status": "running", "phase": "restoring", "percent": 23, "disks": DISKS[:1]}
    assert restore_disk_fills(run) == {"scsi0": 23}


def test_disk_fills_are_full_after_the_copy_and_absent_when_not_running():
    run = {"status": "running", "phase": "probing", "disks": DISKS}
    assert restore_disk_fills(run) == {"scsi0": 100, "scsi1": 100}
    assert restore_disk_fills({**run, "status": "passed"}) is None


def test_timings_from_marks():
    from app.services.restore_worker import timings_from_marks

    marks = [
        ["preflight", (START + timedelta(seconds=10)).isoformat()],
        ["restoring", (START + timedelta(seconds=40)).isoformat()],
        ["booting", (START + timedelta(seconds=100)).isoformat()],
        ["probing", (START + timedelta(seconds=130)).isoformat()],
    ]
    end = START + timedelta(seconds=200)
    assert timings_from_marks(START, marks, end) == {
        "setup": 10,
        "preflight": 30,
        "restoring": 60,
        "booting": 30,
        "probing": 70,
    }
    # Never green: booting unknown, the wait counts as probing (as in execute).
    assert timings_from_marks(START, marks[:3], end) == {
        "setup": 10,
        "preflight": 30,
        "restoring": 60,
        "probing": 100,
        "booting": None,
    }
    assert timings_from_marks(None, marks, end) is None
    assert timings_from_marks(START, [], end) is None
