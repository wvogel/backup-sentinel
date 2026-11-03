import unittest

from app.i18n import set_language
from app.services.human_format import (
    format_bitrate,
    format_count,
    format_rate,
    format_size,
    format_size_coarse,
    short_number,
)

GIB = 1024**3
TIB = 1024**4


class FormatSizeTests(unittest.TestCase):
    def test_datastore_tooltip_values(self) -> None:
        # The PBS datastore from the bug report: 5466 GiB of 38929 GiB.
        self.assertEqual(format_size(5466 * GIB, "de"), "5,3 TiB")
        self.assertEqual(format_size(38929 * GIB, "de"), "38 TiB")
        self.assertEqual(format_size(5466 * GIB, "en"), "5.3 TiB")

    def test_one_decimal_below_ten_whole_numbers_above(self) -> None:
        self.assertEqual(format_size(1.25 * GIB, "de"), "1,2 GiB")
        self.assertEqual(format_size(9.94 * GIB, "en"), "9.9 GiB")
        self.assertEqual(format_size(9.96 * GIB, "en"), "10 GiB")
        self.assertEqual(format_size(187.7 * 1024**2, "en"), "188 MiB")
        self.assertEqual(format_size(4 * GIB, "de"), "4 GiB")

    def test_unit_switches_before_four_digits(self) -> None:
        self.assertEqual(format_size(999 * GIB, "de"), "999 GiB")
        self.assertEqual(format_size(999.6 * GIB, "de"), "1 TiB")
        self.assertEqual(format_size(1000 * GIB, "en"), "1 TiB")
        self.assertEqual(format_size(1023 * GIB, "en"), "1 TiB")
        self.assertEqual(format_size(1536 * GIB, "de"), "1,5 TiB")

    def test_small_and_huge_values(self) -> None:
        self.assertEqual(format_size(0, "de"), "0 B")
        self.assertEqual(format_size(512, "de"), "512 B")
        self.assertEqual(format_size(1000, "de"), "1 KiB")
        self.assertEqual(format_size(3 * 1024**7, "de"), "3.072 EiB")
        self.assertEqual(format_size(3 * 1024**7, "en"), "3,072 EiB")

    def test_unknown_values(self) -> None:
        for value in (None, "", "abc", float("nan"), float("inf")):
            self.assertEqual(format_size(value, "de"), "–")

    def test_language_defaults_to_request(self) -> None:
        set_language("en")
        self.assertEqual(format_size(1.5 * TIB), "1.5 TiB")
        set_language("de")
        self.assertEqual(format_size(1.5 * TIB), "1,5 TiB")
        self.assertEqual(format_size(1.5 * TIB, "EN"), "1.5 TiB")


class FormatSizeCoarseTests(unittest.TestCase):
    def test_two_sig_figs_rounded_down(self) -> None:
        set_language("de")
        self.assertEqual(format_size_coarse(436 * GIB), "430 GB")
        self.assertEqual(format_size_coarse(int(1.92 * TIB)), "1,9 TB")
        self.assertEqual(format_size_coarse(int(36.72 * TIB)), "36 TB")

    def test_english_decimal_point(self) -> None:
        set_language("en")
        self.assertEqual(format_size_coarse(int(1.92 * TIB)), "1.9 TB")
        set_language("de")

    def test_unknown_is_dash(self) -> None:
        self.assertEqual(format_size_coarse(None), "–")
        self.assertEqual(format_size_coarse(-5), "–")


class FormatBitrateTests(unittest.TestCase):
    # Link rates come in as bytes/s (link_bps), shown in decimal bits like a NIC
    # is rated: 1 Gb = 1000 Mb. 10GbE → link_bps 1_250_000_000 → "10,0 Gb/s".
    def test_gigabit_one_decimal_only_when_it_carries_information(self) -> None:
        set_language("de")
        self.assertEqual(format_bitrate(1_250_000_000), "10 Gb/s")  # 10GbE, no ,0
        self.assertEqual(format_bitrate(125_000_000), "1 Gb/s")  # 1GbE
        self.assertEqual(format_bitrate(312_500_000), "2,5 Gb/s")  # 2.5GbE keeps it
        self.assertEqual(format_bitrate(3_125_000_000), "25 Gb/s")  # 25GbE

    def test_below_one_gigabit_is_whole_megabit(self) -> None:
        set_language("de")
        self.assertEqual(format_bitrate(12_500_000), "100 Mb/s")
        self.assertEqual(format_bitrate(117_500_000), "940 Mb/s")
        self.assertEqual(format_bitrate(0), "0 Mb/s")

    def test_throughput_bytes_become_bits(self) -> None:
        set_language("de")
        self.assertEqual(format_bitrate(310_000_000), "2,5 Gb/s")  # 310 MB/s in → ~2,48 Gb/s
        self.assertEqual(format_bitrate(400_000_000), "3,2 Gb/s")  # 400 MB/s out

    def test_english_decimal_point(self) -> None:
        set_language("en")
        self.assertEqual(format_bitrate(312_500_000), "2.5 Gb/s")
        self.assertEqual(format_bitrate(1_250_000_000), "10 Gb/s")
        set_language("de")

    def test_unknown_is_dash(self) -> None:
        for value in (None, "", "abc", float("nan"), float("inf"), -5):
            self.assertEqual(format_bitrate(value), "–")


class FormatRateTests(unittest.TestCase):
    def test_rate(self) -> None:
        self.assertEqual(format_rate(187.7 * 1024**2, "de"), "188 MiB/s")
        self.assertEqual(format_rate(1.6 * GIB, "de"), "1,6 GiB/s")
        self.assertEqual(format_rate(1.6 * GIB, "en"), "1.6 GiB/s")
        self.assertEqual(format_rate(None, "de"), "–")


class ShortNumberTests(unittest.TestCase):
    def test_one_decimal_below_ten(self) -> None:
        self.assertEqual(short_number(5.25), "5.2")
        self.assertEqual(short_number(9.0), "9")
        self.assertEqual(short_number(9.96), "10")
        self.assertEqual(short_number(187.7), "188")

    def test_grouping(self) -> None:
        self.assertEqual(short_number(1234.4), "1,234")
        self.assertEqual(short_number(1234.4, grouping=False), "1234")


class FormatCountTests(unittest.TestCase):
    def test_thousands_separator_per_language(self) -> None:
        self.assertEqual(format_count(1234567, "de"), "1.234.567")
        self.assertEqual(format_count(1234567, "en"), "1,234,567")
        self.assertEqual(format_count(12, "de"), "12")
        self.assertEqual(format_count(None, "de"), "None")


class RunningBackupLabelTests(unittest.TestCase):
    def test_banner_uses_rounded_sizes_and_rates(self) -> None:
        from app.web.routes_clusters import running_backup_info

        progress = {
            "percent": 53,
            "written_bytes": int(867.1 * GIB),
            "target_bytes": int(1.6 * TIB),
            "read_label": "1.6 GiB/s",
            "write_label": "154.5 MiB/s",
        }
        set_language("de")
        details = running_backup_info(progress)["details"]
        self.assertIn("867 GiB", details[0])
        self.assertIn("1,6 TiB", details[0])
        self.assertIn("1,6 GiB/s", details[1])
        self.assertIn("155 MiB/s", details[1])


if __name__ == "__main__":
    unittest.main()
