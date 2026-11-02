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


class RestoreStatusTooltipTests(unittest.TestCase):
    def test_lists_every_probed_ip_with_per_check_detail(self):
        from app.web.common import restore_status_tooltip

        run = {
            "result": "limited",
            "checks": {
                "10.121.101.50": {"icmp": True, "ports": {"80": True, "443": False}},
                "10.121.203.10": {"icmp": True, "ports": {"80": True, "443": True}},
            },
        }
        tip = restore_status_tooltip(run)
        lines = tip.split("\n")
        assert lines[0] == "limited"
        assert "10.121.101.50: ICMP ✓ · :80 ✓ · :443 ✗" in lines
        assert "10.121.203.10: ICMP ✓ · :80 ✓ · :443 ✓" in lines

    def test_error_run_shows_the_error(self):
        from app.web.common import restore_status_tooltip

        tip = restore_status_tooltip({"result": "error", "checks": {"error": "boom"}})
        assert tip == "error\nboom"

    def test_no_checks_is_just_the_word(self):
        from app.web.common import restore_status_tooltip

        assert restore_status_tooltip({"status": "queued", "checks": None}) == "queued"


def test_restore_check_chips_union_across_ips():
    from app.web.common import restore_check_chips

    checks = {
        "10.0.0.5": {"icmp": False, "arp": True, "ports": {"3389": False, "445": True}},
        "10.0.1.5": {"icmp": True, "arp": False, "ports": {"3389": True}},
    }
    assert restore_check_chips(checks) == [
        {"label": "ICMP", "ok": True},
        {"label": "ARP", "ok": True},
        {"label": "TCP 3389", "ok": True},
        {"label": "TCP 445", "ok": True},
    ]


def test_restore_check_chips_skips_unrequired_icmp_and_non_probe_results():
    from app.web.common import restore_check_chips

    assert restore_check_chips({"10.0.0.5": {"icmp": None, "arp": False, "ports": {}}}) == [
        {"label": "ARP", "ok": False}
    ]
    assert restore_check_chips({"error": "boom"}) == []
    assert restore_check_chips(None) == []
