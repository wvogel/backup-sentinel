"""Pure tests for the per-phase timing breakdown shown on a finished run."""

from app.services.human_format import format_rate
from app.web.common import restore_phase_timings


def test_empty_when_no_timings():
    assert restore_phase_timings({}) == []
    assert restore_phase_timings({"phase_timings": None}) == []
    assert restore_phase_timings({"phase_timings": {}}) == []


def test_orders_by_pipeline_and_formats():
    run = {
        "phase_timings": {
            "probing": 25,
            "restoring": 7325,
            "helper": 42,
            "preflight": 3,
            "setup": 2,
            "network": 1,
            "booting": 95,
        }
    }
    got = restore_phase_timings(run)
    assert [p["phase"] for p in got] == [
        "setup",
        "preflight",
        "helper",
        "restoring",
        "network",
        "booting",
        "probing",
    ]
    by = {p["phase"]: p["text"] for p in got}
    assert by["setup"] == "2 s"
    assert by["preflight"] == "3 s"
    assert by["helper"] == "42 s"
    assert by["restoring"] == "2:02:05"  # H:MM:SS once past an hour
    assert by["booting"] == "1:35"  # M:SS below an hour
    assert by["probing"] == "25 s"  # plain seconds below a minute


def test_restoring_rate_from_bytes_done():
    run = {"phase_timings": {"restoring": 100}, "bytes_done": 500_000_000}
    item = next(p for p in restore_phase_timings(run) if p["phase"] == "restoring")
    assert item["rate_text"] == format_rate(5_000_000)  # 500 MB / 100 s


def test_rate_falls_back_to_disk_bytes_when_no_bytes_done():
    run = {"phase_timings": {"restoring": 100}, "disk_bytes": 500_000_000}
    item = restore_phase_timings(run)[0]
    assert item["rate_text"] == format_rate(5_000_000)


def test_no_rate_without_bytes_or_with_zero_seconds():
    assert "rate_text" not in restore_phase_timings({"phase_timings": {"restoring": 0}, "bytes_done": 1})[0]
    assert "rate_text" not in restore_phase_timings({"phase_timings": {"restoring": 100}})[0]


def test_only_restoring_carries_a_rate():
    run = {"phase_timings": {"booting": 100}, "bytes_done": 500_000_000}
    assert "rate_text" not in restore_phase_timings(run)[0]


def test_unknown_phase_kept_after_known_ones():
    got = restore_phase_timings({"phase_timings": {"weird": 5, "setup": 1}})
    assert [p["phase"] for p in got] == ["setup", "weird"]


def test_non_numeric_entries_skipped():
    got = restore_phase_timings({"phase_timings": {"setup": "x", "helper": 3}})
    assert [p["phase"] for p in got] == ["helper"]
