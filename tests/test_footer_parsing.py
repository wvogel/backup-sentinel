import unittest

from app.web.common import _parse_footer_links


class ParseFooterLinksTests(unittest.TestCase):
    def test_empty_string_returns_empty_list(self) -> None:
        self.assertEqual(_parse_footer_links(""), [])

    def test_single_entry_returns_single_element_list(self) -> None:
        result = _parse_footer_links("Impressum|https://example.com/impressum")
        self.assertEqual(
            result,
            [{"label": "Impressum", "url": "https://example.com/impressum"}],
        )

    def test_multiple_entries_are_split_on_comma(self) -> None:
        result = _parse_footer_links("Impressum|https://example.com/impressum,Datenschutz|https://example.com/privacy")
        self.assertEqual(
            result,
            [
                {"label": "Impressum", "url": "https://example.com/impressum"},
                {"label": "Datenschutz", "url": "https://example.com/privacy"},
            ],
        )

    def test_whitespace_around_labels_and_urls_is_stripped(self) -> None:
        result = _parse_footer_links("  Label  |  https://example.com  ")
        self.assertEqual(result, [{"label": "Label", "url": "https://example.com"}])

    def test_entries_without_pipe_are_skipped(self) -> None:
        result = _parse_footer_links("broken,Good|https://example.com,also-broken")
        self.assertEqual(result, [{"label": "Good", "url": "https://example.com"}])

    def test_url_with_embedded_pipe_keeps_only_first_split(self) -> None:
        # split with maxsplit=1 means extra pipes end up in the URL
        result = _parse_footer_links("Label|https://example.com/?x=1|y=2")
        self.assertEqual(result, [{"label": "Label", "url": "https://example.com/?x=1|y=2"}])


if __name__ == "__main__":
    unittest.main()
