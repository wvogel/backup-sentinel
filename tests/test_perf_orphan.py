"""A performance test whose app process restarted under it (a deploy) must not
stay "running" for ever: nobody could cancel it, and no new test could start."""

import json
import threading

import pytest

from app.services import restore_perf


@pytest.fixture
def state(monkeypatch):
    settings: dict[str, str] = {}
    cleaned: list[str] = []
    monkeypatch.setattr(
        restore_perf, "get_settings_dict", lambda prefix="": {k: v for k, v in settings.items() if k.startswith(prefix)}
    )
    monkeypatch.setattr(restore_perf, "save_settings", settings.update)
    monkeypatch.setattr(restore_perf, "_cleanup_host", cleaned.append)
    monkeypatch.setattr(restore_perf, "_worker", None)
    monkeypatch.setattr(restore_perf, "involved_storages", lambda: set())
    return settings, cleaned


def _orphan(settings, storage="tank"):
    settings[restore_perf._RUNNING_KEY] = storage
    settings[restore_perf._PROGRESS_KEY] = json.dumps({"storage": storage, "pct": 33})
    settings[restore_perf._PREFIX + storage] = json.dumps({"bps": 600_000_000, "at": "2026-11-01T10:00:00+00:00"})


def _wait_for(cleaned):
    for thread in threading.enumerate():
        if thread.name.startswith("perf-cleanup-"):
            thread.join(timeout=5)
    return cleaned


def test_orphaned_test_is_released_and_the_host_cleaned_up(state):
    settings, cleaned = state
    _orphan(settings)
    assert restore_perf.recover_orphaned() == "tank"
    assert restore_perf.running_storage() == ""
    assert restore_perf.get_progress() == {}
    assert _wait_for(cleaned) == ["tank"]
    # The last measurement stays: an interrupted test is not a failed one.
    assert restore_perf.get_perf()["tank"]["bps"] == 600_000_000
    assert restore_perf.recover_orphaned() == ""  # nothing left to release


def test_a_test_this_process_runs_is_left_alone(state, monkeypatch):
    settings, cleaned = state
    _orphan(settings)
    stop = threading.Event()
    worker = threading.Thread(target=stop.wait, daemon=True)
    worker.start()
    monkeypatch.setattr(restore_perf, "_worker", worker)
    try:
        assert restore_perf.recover_orphaned() == ""
        assert restore_perf.running_storage() == "tank"
        assert restore_perf.cancel("tank") == (True, "")
        assert settings[restore_perf._CANCEL_KEY] == "tank"  # the worker is asked to stop
    finally:
        stop.set()
        worker.join(timeout=5)
    assert cleaned == []


def test_cancel_ends_an_orphaned_test_at_once(state):
    settings, cleaned = state
    _orphan(settings)
    assert restore_perf.cancel("tank") == (True, "")
    assert restore_perf.running_storage() == ""
    assert _wait_for(cleaned) == ["tank"]


def test_an_orphaned_test_does_not_block_the_next_one(state, monkeypatch):
    settings, cleaned = state
    _orphan(settings, "tank")
    ran = threading.Event()
    monkeypatch.setattr(restore_perf, "_run", lambda storage: ran.set())
    assert restore_perf.start("fast") == (True, "")
    assert ran.wait(timeout=5)
    assert restore_perf.running_storage() == "fast"
    assert _wait_for(cleaned) == ["tank"]


def test_warmup_samples_are_counted_apart(state):
    """The first minute is written but not measured: its samples are marked."""
    from datetime import UTC, datetime, timedelta

    settings, _cleaned = state
    now = datetime.now(UTC)

    def progress(started_seconds_ago):
        settings[restore_perf._RUNNING_KEY] = "tank"
        prog = restore_perf.get_progress() or {"storage": "tank", "series": [], "warmup_n": 0}
        prog.update(started_at=(now - timedelta(seconds=started_seconds_ago)).isoformat(), warmup=60)
        settings[restore_perf._PROGRESS_KEY] = json.dumps(prog)

    progress(10)
    restore_perf._store_progress("tank", {"pct": 3, "rate_bps": 900})
    progress(40)
    restore_perf._store_progress("tank", {"pct": 11, "rate_bps": 880})
    assert restore_perf.get_progress()["warming"] is True
    assert restore_perf.get_progress()["warmup_n"] == 2
    progress(70)
    restore_perf._store_progress("tank", {"pct": 20, "rate_bps": 700})
    after = restore_perf.get_progress()
    assert after["warming"] is False
    assert after["warmup_n"] == 2 and after["series"] == [900, 880, 700]


def test_fio_warms_up_before_it_measures():
    from app.services.restore_server import RestoreServer

    commands = []

    class Ssh:
        def run(self, command):
            commands.append(command)
            if command.startswith("pvesm alloc"):
                return "successfully created 'tank:vm-999999-disk-0'"
            if command.startswith("pvesm path"):
                return "/dev/tank/vm-999999-disk-0"
            if "grep -q bw_bytes" in command:
                return "DONE"
            if command.startswith("cat "):
                return '{"jobs":[{"write":{"bw_bytes":123456789}}]}'
            return ""

    server = RestoreServer({}, pve=None, ssh=Ssh())
    assert server.benchmark_storage_write("tank", ramp=60, sleep=lambda s: None) == 123456789
    fio = next(c for c in commands if "fio --name=" in c)
    assert "--ramp_time=60" in fio and "--runtime=300" in fio


def test_progress_comes_from_the_clock_not_from_fio():
    """While fio warms up its status line carries no percentage: the test must
    still be seen to move, with the rate when the line has one."""
    from app.services.restore_server import RestoreServer, clock_progress, parse_fio_rate

    assert clock_progress(0, 360) == {"pct": 0.0, "eta": "06m:00s"}
    assert clock_progress(90, 360) == {"pct": 25.0, "eta": "04m:30s"}
    assert clock_progress(999, 360)["pct"] == 99.0  # 100 only with the result
    assert parse_fio_rate("Jobs: 1 (f=1): [/(1)][-.-%][w=700MiB/s][w=175 IOPS][eta 05m:55s]") == 700 * 1024**2
    assert parse_fio_rate("Jobs: 1 (f=1): [/(1)][-.-%][eta 05m:55s]") is None

    lines = iter(
        [
            "Jobs: 1 (f=1): [/(1)][-.-%][w=700MiB/s][w=175 IOPS][eta 05m:55s]",
            "Jobs: 1 (f=1): [/(1)][-.-%][eta 05m:50s]",
            "Jobs: 1 (f=1): [w(1)][20.0%][w=600MiB/s][w=150 IOPS][eta 04m:40s]",
            "DONE",
        ]
    )

    class Ssh:
        def run(self, command):
            if command.startswith("pvesm alloc"):
                return "successfully created 'tank:vm-999999-disk-0'"
            if command.startswith("pvesm path"):
                return "/dev/tank/vm-999999-disk-0"
            if "grep -q bw_bytes" in command:
                return next(lines)
            if command.startswith("cat "):
                return '{"jobs":[{"write":{"bw_bytes":600000000}}]}'
            return ""

    clock = [0.0]

    def sleep(seconds):
        clock[0] += 30

    samples = []
    server = RestoreServer({}, pve=None, ssh=Ssh())
    server.benchmark_storage_write(
        "tank", ramp=60, runtime=300, progress_cb=samples.append, now=lambda: clock[0], sleep=sleep
    )
    assert [s["pct"] for s in samples[:3]] == [8.3, 16.7, 25.0]  # moves from the first poll on
    assert [s.get("rate_bps") for s in samples[:3]] == [700 * 1024**2, None, 600 * 1024**2]
    assert samples[0]["eta"] == "05m:30s"
    assert samples[-1] == {"pct": 100.0, "rate_bps": 600000000}
