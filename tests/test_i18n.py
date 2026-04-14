import unittest

from app.i18n import (
    DEFAULT_LANGUAGE,
    SUPPORTED_LANGUAGES,
    get_language,
    get_translations,
    set_language,
    t,
)


class I18nTests(unittest.TestCase):
    def setUp(self) -> None:
        # Record the language at test start so we can restore it.
        self._saved_lang = get_language()

    def tearDown(self) -> None:
        set_language(self._saved_lang)

    def test_t_returns_translated_string_for_current_language(self) -> None:
        set_language("de")
        self.assertEqual(t("header.home"), "Übersicht")
        set_language("en")
        self.assertEqual(t("header.home"), "Overview")

    def test_t_falls_back_to_english_when_key_missing_in_current_language(self) -> None:
        # Inject a key that exists only in the English translations table so we
        # can observe the English fallback when the current language is DE.
        from app.i18n import _load_lang

        en = _load_lang("en")
        de = _load_lang("de")

        sentinel_key = "__test_only_fallback_key__"
        en[sentinel_key] = "English fallback value"
        # Make sure DE does *not* have it
        de.pop(sentinel_key, None)
        try:
            set_language("de")
            self.assertEqual(t(sentinel_key), "English fallback value")
        finally:
            en.pop(sentinel_key, None)

    def test_t_returns_key_when_missing_everywhere(self) -> None:
        set_language("de")
        self.assertEqual(t("nonexistent.key.xyz"), "nonexistent.key.xyz")

    def test_t_substitutes_placeholders(self) -> None:
        set_language("de")
        # cluster.title contains the {name} placeholder
        result = t("cluster.title", name="pve-x")
        self.assertIn("pve-x", result)
        self.assertNotIn("{name}", result)

    def test_set_language_and_get_language_round_trip(self) -> None:
        set_language("en")
        self.assertEqual(get_language(), "en")
        set_language("de")
        self.assertEqual(get_language(), "de")

    def test_set_language_invalid_falls_back_to_default(self) -> None:
        set_language("fr")
        self.assertEqual(get_language(), DEFAULT_LANGUAGE)

    def test_supported_languages_contains_de_and_en(self) -> None:
        self.assertIn("de", SUPPORTED_LANGUAGES)
        self.assertIn("en", SUPPORTED_LANGUAGES)

    def test_get_translations_returns_non_empty_dict_for_current_lang(self) -> None:
        set_language("de")
        translations = get_translations()
        self.assertIsInstance(translations, dict)
        self.assertGreater(len(translations), 50)
        self.assertIn("header.home", translations)

    def test_get_translations_accepts_explicit_language(self) -> None:
        en_translations = get_translations("en")
        self.assertIsInstance(en_translations, dict)
        self.assertEqual(en_translations.get("header.home"), "Overview")


if __name__ == "__main__":
    unittest.main()
