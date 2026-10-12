import unittest
from unittest import mock

from app.services import email_assets


class BrandLogoTests(unittest.TestCase):
    def test_local_svg_logo_becomes_png(self):
        png = email_assets.brand_logo_png("/static/logo.svg")
        self.assertTrue(png.startswith(b"\x89PNG"))

    def test_path_outside_static_is_refused(self):
        self.assertIsNone(email_assets.brand_logo_png("/static/../app/config.py"))

    def test_no_logo_configured(self):
        self.assertIsNone(email_assets.brand_logo_png(""))

    def test_remote_png_is_used_as_is(self):
        png = b"\x89PNG\r\n\x1a\nrest"
        with mock.patch.object(email_assets, "_read_logo", return_value=(png, "image/png")):
            self.assertEqual(email_assets.brand_logo_png("https://example.org/logo.png"), png)

    def test_unreachable_logo_is_left_out(self):
        with mock.patch.object(email_assets, "_read_logo", side_effect=OSError("timeout")):
            self.assertIsNone(email_assets.brand_logo_png("https://example.org/logo.svg"))

    def test_wordmark_ships_with_the_app(self):
        self.assertTrue(email_assets.wordmark_png().startswith(b"\x89PNG"))
