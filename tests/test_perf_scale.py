"""The performance bar of the target storages (Settings → Restore server)."""

from pathlib import Path

from app.web.routes_settings import PERF_SCALE_MIN, perf_scale


def test_scale_is_one_gigabyte_per_second_at_least():
    assert perf_scale([]) == PERF_SCALE_MIN
    assert perf_scale([{"perf": None}, {"perf": {"error": "x"}}, {}]) == PERF_SCALE_MIN
    assert perf_scale([{"perf": {"bps": 300_000_000}}, {"perf": {"bps": 700_000_000}}]) == PERF_SCALE_MIN


def test_scale_grows_with_the_fastest_storage():
    rows = [{"perf": {"bps": 300_000_000}}, {"perf": {"bps": 2_500_000_000}}]
    assert perf_scale(rows) == 2_500_000_000  # the fastest fills the bar, nobody beyond it


def test_storage_rows_render_with_both_meters_and_a_test_button():
    from app.web.common import templates

    templates.env.get_template("settings.html")  # compiles
    macro_source = Path("templates/settings.html").read_text()
    assert "PREVIEW-ONLY" not in macro_source
    for needle in (
        "store-cap-bar",
        "store-perf-bar",
        "restore.perf_per_tb",
        "js-perf-test",
        "restore.st_used_free",
        "js-perf-cancel",
        "perf-spark-zero",
        "js-perf-tip",
        "data-scale",
    ):
        assert needle in macro_source


def test_rate_curve_starts_at_zero():
    """The y-axis of the throughput curve starts at 0: never scaled between the
    lowest and the highest sample."""
    js = Path("static/js/settings.js").read_text()
    spark = js[js.index("function sparkY") : js.index("function renderProgress")]
    assert "Math.min.apply" not in spark and "- min" not in spark
    assert "SPARK_ZERO" in spark


def test_track_dot_shows_the_average_of_a_running_test():
    js = Path("static/js/settings.js").read_text()
    assert "meanRate(box._series.slice(warmN))" in js  # the warm-up is left out


def test_time_per_terabyte():
    from app.web.common import time_per_tb

    assert time_per_tb(1024**4 / 600) == "10 min"
    assert time_per_tb(1024**4 / 3900) == "1 h 05 min"
    assert time_per_tb(0) == "" and time_per_tb(None) == ""
