import pathlib
import re
import unittest

from app.services import theme


class ColorTests(unittest.TestCase):
    def test_normalize_hex(self):
        self.assertEqual(theme.normalize_hex(" #FFF "), "#ffffff")
        self.assertEqual(theme.normalize_hex("#3b82f6"), "#3b82f6")
        self.assertIsNone(theme.normalize_hex("red"))
        self.assertIsNone(theme.normalize_hex("#12345"))
        self.assertIsNone(theme.normalize_hex(None))

    def test_contrast_ratio(self):
        self.assertAlmostEqual(theme.contrast_ratio("#000000", "#ffffff"), 21.0, places=1)
        self.assertAlmostEqual(theme.contrast_ratio("#ffffff", "#ffffff"), 1.0, places=2)
        self.assertGreater(theme.contrast_ratio("#1a237e", "#facc15"), 4.5)


class ResolveTests(unittest.TestCase):
    def test_unknown_preset_falls_back(self):
        _preset, base = theme.resolve("nonsense", {})
        self.assertEqual(base, theme.PRESETS["neutral"].base)

    def test_overrides_replace_only_valid_values(self):
        _, base = theme.resolve("neutral", {"primary": "#000000", "accent": "nope", "running": ""})
        self.assertEqual(base.primary, "#000000")
        self.assertEqual(base.accent, "#2563eb")
        self.assertEqual(base.running, "#0d9488")


class TokenTests(unittest.TestCase):
    def test_default_light_tokens_use_the_preset_colors(self):
        preset = theme.PRESETS["neutral"]
        tokens = theme.derive_tokens(preset, preset.base)
        light = tokens["light"]
        self.assertEqual(light["--bg"], "#f5f7fa")
        self.assertEqual(light["--header-bg"], "#1e293b")
        self.assertEqual(light["--accent-fill"], "#2563eb")
        self.assertEqual(light["--accent-ink"], "#ffffff")
        self.assertEqual(light["--running"], "#0d9488")
        self.assertEqual(light["--line"], "#dde3ec")
        self.assertEqual(light["--sage"], "#e8edf3")
        self.assertEqual(set(tokens["light"]), set(tokens["dark"]))

    def test_accent_ink_never_matches_a_light_accent(self):
        preset = theme.PRESETS["neutral"]
        base = theme.BaseColors(primary="#ffffff", accent="#ffffff", background="#f5f7fa", running="#3b82f6")
        ink = theme.derive_tokens(preset, base)["light"]["--accent-ink"]
        self.assertGreaterEqual(theme.contrast_ratio(ink, "#ffffff"), 4.5)


class ContrastCheckTests(unittest.TestCase):
    def test_presets_pass(self):
        for preset in theme.PRESETS.values():
            self.assertEqual(theme.check_contrast(preset.base), [])

    def test_yellow_as_background_is_flagged(self):
        base = theme.BaseColors(primary="#1a237e", accent="#facc15", background="#facc15", running="#ffff66")
        keys = {w.key for w in theme.check_contrast(base)}
        self.assertIn("running", keys)


class RenderTests(unittest.TestCase):
    def test_render_css(self):
        css = theme.render_css({"light": {"--bg": "#fff"}, "dark": {"--bg": "#000"}})
        self.assertIn(":root{--bg:#fff;}", css)
        self.assertIn('html[data-theme="dark"]{--bg:#000;}', css)


class StylesheetSyncTests(unittest.TestCase):
    """style.css defaults must equal the default theme (no inline style needed)."""

    def _block(self, css: str, selector: str) -> dict[str, str]:
        body = css.split(selector + " {", 1)[1].split("\n}", 1)[0]
        return dict(re.findall(r"(--[\w-]+):\s*([^;]+);", body))

    def test_defaults_match_the_default_preset(self):
        css = pathlib.Path("static/style.css").read_text()
        preset = theme.PRESETS["neutral"]
        tokens = theme.derive_tokens(preset, preset.base)
        for selector, mode in ((":root", "light"), ('html[data-theme="dark"]', "dark")):
            block = self._block(css, selector)
            for key, value in tokens[mode].items():
                self.assertEqual(block.get(key, "").lower(), value.lower(), f"{mode} {key}")


class DarkModeReadabilityTests(unittest.TestCase):
    def test_dark_text_stays_readable_for_mid_luminance_primaries(self):
        preset = theme.PRESETS["neutral"]
        for primary in ("#3b82f6", "#808080", "#1a237e", "#8b1e3f"):
            base = theme.BaseColors(primary=primary, accent="#facc15", background="#f5f7fa", running="#3b82f6")
            dark = theme.derive_tokens(preset, base)["dark"]
            for ink, surface in (("--btn-text", "--btn-bg"), ("--ink", "--panel"), ("--header-link", "--header-bg")):
                ratio = theme.contrast_ratio(dark[ink], dark[surface])
                self.assertGreaterEqual(ratio, 4.5, f"{primary}: {ink} on {surface} = {ratio:.2f}")

    def test_dark_surfaces_are_only_moderately_tinted(self):
        # A saturated primary (a dark blue) must not turn the dark
        # mode into a saturated navy page: surfaces keep its hue, capped saturation.
        # Hand-tuned dark_surfaces overrides are exempt (they are tested visually).
        import colorsys

        preset = theme.PRESETS["neutral"]
        overrides = set((preset.dark_surfaces or {}).keys())
        for primary in ("#1a237e", "#3b82f6", "#8b1e3f"):
            base = theme.BaseColors(primary=primary, accent="#facc15", background="#f5f7fa", running="#3b82f6")
            dark = theme.derive_tokens(preset, base)["dark"]
            for key in ("--bg", "--panel", "--panel-alt", "--chip", "--header-bg", "--line", "--btn-bg", "--sage"):
                if key in overrides:
                    continue
                r, g, b = (int(dark[key][i : i + 2], 16) / 255 for i in (1, 3, 5))
                _h, _l, sat = colorsys.rgb_to_hls(r, g, b)
                self.assertLessEqual(sat, 0.35, f"{primary}: {key} = {dark[key]} saturation {sat:.2f}")


class ThemeCssFallbackTests(unittest.TestCase):
    def test_settings_failure_falls_back_to_stylesheet_defaults(self):
        from unittest import mock

        from app import db
        from app.web import common

        with (
            mock.patch.object(db, "get_settings_dict", side_effect=RuntimeError("db down")),
            self.assertLogs("app.web.common", level="WARNING"),
        ):
            self.assertEqual(common.theme_css(), "")


class CustomCssTests(unittest.TestCase):
    def test_custom_css_is_kept_but_cannot_close_the_style_element(self):
        self.assertEqual(theme.normalize_custom_css("  :root { --line: #eee; }\r\n"), ":root { --line: #eee; }")
        self.assertEqual(theme.normalize_custom_css(None), "")
        self.assertIsNone(theme.normalize_custom_css("a{}</style><script>alert(1)</script>"))
        self.assertIsNone(theme.normalize_custom_css("a" * (theme.CUSTOM_CSS_MAX + 1)))
