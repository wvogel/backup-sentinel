import pathlib
import re
import unittest

from app.services import theme


class ColorTests(unittest.TestCase):
    def test_normalize_hex(self):
        self.assertEqual(theme.normalize_hex(" #FFF "), "#ffffff")
        self.assertEqual(theme.normalize_hex("#4A7EB5"), "#4a7eb5")
        self.assertIsNone(theme.normalize_hex("red"))
        self.assertIsNone(theme.normalize_hex("#12345"))
        self.assertIsNone(theme.normalize_hex(None))

    def test_contrast_ratio(self):
        self.assertAlmostEqual(theme.contrast_ratio("#000000", "#ffffff"), 21.0, places=1)
        self.assertAlmostEqual(theme.contrast_ratio("#ffffff", "#ffffff"), 1.0, places=2)
        self.assertGreater(theme.contrast_ratio("#141450", "#ffff00"), 4.5)


class ResolveTests(unittest.TestCase):
    def test_unknown_preset_falls_back(self):
        _preset, base = theme.resolve("nonsense", {})
        self.assertEqual(base, theme.PRESETS["smile_eyes"].base)

    def test_overrides_replace_only_valid_values(self):
        _, base = theme.resolve("smile_eyes", {"primary": "#000000", "accent": "nope", "running": ""})
        self.assertEqual(base.primary, "#000000")
        self.assertEqual(base.accent, "#ffff00")
        self.assertEqual(base.running, "#4a7eb5")


class TokenTests(unittest.TestCase):
    def test_smile_eyes_light_tokens_use_brand_colors(self):
        preset = theme.PRESETS["smile_eyes"]
        tokens = theme.derive_tokens(preset, preset.base)
        light = tokens["light"]
        self.assertEqual(light["--bg"], "#f8f5f2")
        self.assertEqual(light["--header-bg"], "#141450")
        self.assertEqual(light["--accent-fill"], "#ffff00")
        self.assertEqual(light["--accent-ink"], "#141450")
        self.assertEqual(light["--running"], "#4a7eb5")
        self.assertEqual(light["--line"], "#efe7db")
        self.assertEqual(light["--sage"], "#d8ddce")
        self.assertEqual(set(tokens["light"]), set(tokens["dark"]))

    def test_accent_ink_never_matches_a_light_accent(self):
        preset = theme.PRESETS["smile_eyes"]
        base = theme.BaseColors(primary="#ffffff", accent="#ffffff", background="#f8f5f2", running="#4a7eb5")
        ink = theme.derive_tokens(preset, base)["light"]["--accent-ink"]
        self.assertGreaterEqual(theme.contrast_ratio(ink, "#ffffff"), 4.5)


class ContrastCheckTests(unittest.TestCase):
    def test_presets_pass(self):
        for preset in theme.PRESETS.values():
            self.assertEqual(theme.check_contrast(preset.base), [])

    def test_yellow_as_background_is_flagged(self):
        base = theme.BaseColors(primary="#141450", accent="#ffff00", background="#ffff00", running="#ffff66")
        keys = {w.key for w in theme.check_contrast(base)}
        self.assertIn("running", keys)


class RenderTests(unittest.TestCase):
    def test_render_css(self):
        css = theme.render_css({"light": {"--bg": "#fff"}, "dark": {"--bg": "#000"}})
        self.assertIn(":root{--bg:#fff;}", css)
        self.assertIn('html[data-theme="dark"]{--bg:#000;}', css)


class StylesheetSyncTests(unittest.TestCase):
    """style.css defaults must equal the smile eyes theme (no inline style needed)."""

    def _block(self, css: str, selector: str) -> dict[str, str]:
        body = css.split(selector + " {", 1)[1].split("\n}", 1)[0]
        return dict(re.findall(r"(--[\w-]+):\s*([^;]+);", body))

    def test_defaults_match_smile_eyes(self):
        css = pathlib.Path("static/style.css").read_text()
        preset = theme.PRESETS["smile_eyes"]
        tokens = theme.derive_tokens(preset, preset.base)
        for selector, mode in ((":root", "light"), ('html[data-theme="dark"]', "dark")):
            block = self._block(css, selector)
            for key, value in tokens[mode].items():
                self.assertEqual(block.get(key, "").lower(), value.lower(), f"{mode} {key}")
