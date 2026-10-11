import unittest

from app.services import pdf_reports
from app.services.theme import BaseColors

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

    def test_palette_is_only_active_while_writing(self) -> None:
        before = pdf_reports.C_HEADER.hexval()
        with pdf_reports._applied_palette(pdf_reports.palette_for(BLACK_THEME)):
            self.assertEqual(pdf_reports.C_HEADER.hexval(), "0x000000")
        self.assertEqual(pdf_reports.C_HEADER.hexval(), before)


if __name__ == "__main__":
    unittest.main()
