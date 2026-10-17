import unittest
from datetime import date

from app.i18n import set_language
from app.web.common import spark_tooltip


class SparkTooltipTests(unittest.TestCase):
    def setUp(self) -> None:
        set_language("de")

    def test_incremental_day_with_all_details(self) -> None:
        day = {
            "date": date(2026, 10, 8),
            "status": "ok",
            "size_bytes": 42 * 1024**3,
            "duration_seconds": 305,
            "throughput_bps": int(43.6 * 1024**2),
            "reused_pct": 90,
            "full_backup": False,
        }
        self.assertEqual(spark_tooltip(day), "08.10.: ok · 42 GiB · 5 min · ⌀ 44 MiB/s · 90 % wiederverwendet")

    def test_full_run_and_long_duration(self) -> None:
        day = {"date": date(2026, 10, 9), "status": "ok", "duration_seconds": 185000, "full_backup": True}
        self.assertEqual(spark_tooltip(day), "09.10.: ok · 51 h · Voll-Lauf")

    def test_day_without_backup(self) -> None:
        self.assertEqual(spark_tooltip({"date": date(2026, 10, 1), "status": "none"}), "01.10.: kein Backup")


if __name__ == "__main__":
    unittest.main()
