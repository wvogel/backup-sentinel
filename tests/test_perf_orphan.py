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
