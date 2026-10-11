import unittest

from app.services import pdf_reports
from app.services.theme import PRESETS, BaseColors, contrast_ratio

BLACK_THEME = BaseColors(primary="#000000", accent="#ffff00", background="#f8f5f2", running="#4a7eb5")


class PdfPaletteTests(unittest.TestCase):
    def test_palette_follows_theme(self) -> None:
        palette = pdf_reports.palette_for(BLACK_THEME)
        self.assertEqual(palette.header.hexval(), "0x000000")
        self.assertEqual(palette.accent.hexval(), "0x4a7eb5")
        self.assertEqual(palette.banner_label.hexval(), "0xffff00")
        self.assertEqual(palette.cluster.hexval(), "0xe0e0e0")

    def test_default_palette_is_smile_eyes(self) -> None:
        self.assertEqual(pdf_reports.palette_for(None).header.hexval(), "0x141450")

    def test_text_on_the_header_is_readable_for_every_preset_and_light_primaries(self) -> None:
        light = BaseColors(primary="#f8f5f2", accent="#ffff00", background="#ffffff", running="#4a7eb5")
        for base in [*(p.base for p in PRESETS.values()), light]:
            palette = pdf_reports.palette_for(base)
            header = "#" + palette.header.hexval()[2:]
            for ink in (palette.banner_label, palette.header_ink):
                self.assertGreaterEqual(contrast_ratio("#" + ink.hexval()[2:], header), 4.5, base)

    def test_palette_is_only_active_while_writing(self) -> None:
        before = pdf_reports.C_HEADER.hexval()
        with pdf_reports._applied_palette(pdf_reports.palette_for(BLACK_THEME)):
            self.assertEqual(pdf_reports.C_HEADER.hexval(), "0x000000")
        self.assertEqual(pdf_reports.C_HEADER.hexval(), before)


if __name__ == "__main__":
    unittest.main()
