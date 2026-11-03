"""The per-VM "Timeout (Min.)" field: minutes → probe_window_seconds, clamped
to the model's 30 s–24 h range, blank/zero/garbage → None (use the default)."""

import unittest

from app.web.routes_restore import _timeout_minutes_to_seconds as conv


class TimeoutMinutesToSecondsTests(unittest.TestCase):
    def test_minutes_become_seconds(self) -> None:
        self.assertEqual(conv("5"), 300)
        self.assertEqual(conv("1"), 60)
        self.assertEqual(conv("10"), 600)
        self.assertEqual(conv(" 15 "), 900)

    def test_blank_zero_and_garbage_are_none(self) -> None:
        for raw in ("", "   ", "0", "abc", "-5", "3.5"):
            self.assertIsNone(conv(raw))

    def test_clamped_to_model_range(self) -> None:
        self.assertEqual(conv("99999"), 86400)  # 24 h ceiling


if __name__ == "__main__":
    unittest.main()
