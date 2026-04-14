# ADR-0005: Server-side i18n with contextvars

## Status

Accepted

## Context

Backup Sentinel supports German and English with a one-click language toggle in the header. The translation function needs to work in:

1. Jinja2 templates: `{{ t("header.home") }}`
2. Python code: `t("common.logged_in")` (e.g. fallback user names, error messages)
3. JavaScript: `T("js.sync_complete")`

Options considered:
1. **Client-side i18n (i18next, formatjs)** — translations shipped as a JS bundle, applied after page load. Requires a build step and causes text flashing on load.
2. **URL-based locale routing** (`/en/settings`, `/de/settings`) — explicit but pollutes routes and breaks bookmarks.
3. **Per-user preference in the database** — requires user accounts (we have none, auth is external).
4. **Server-side with contextvars + cookie** — language determined by the `bsentinel-lang` cookie, stored in a per-request `ContextVar`.

## Decision

Implement i18n server-side using Python's `contextvars.ContextVar` for request-scoped language state:

- `app/i18n/__init__.py` exposes `t()`, `set_language()`, `get_language()`, `get_translations()`
- A `LanguageMiddleware` reads the `bsentinel-lang` cookie and calls `set_language()`
- Translations live as flat JSON files (`en.json`, `de.json`) with dot-notation keys
- Jinja2 globals expose `t` and `get_translations_json`
- The template injects `BSENTINEL_TRANSLATIONS` as a global JS object; `T(key)` looks up values client-side
- The language toggle button writes to both `localStorage` and the cookie, then reloads

## Consequences

**Positive:**
- Translations are rendered server-side — no flash of untranslated content
- The same `t()` function works in Python and Jinja2 (registered as global)
- Adding a language is a single JSON file — no code changes
- Fallback to English happens automatically if a key is missing in the active language
- `contextvars` are safe across async boundaries (FastAPI's threadpool workers)

**Negative:**
- All translations for the active language are inlined in each HTML response (~12 KB of JSON)
- Switching languages requires a full page reload
- Cookie-based detection means the first-ever request shows the default language until a cookie is set

**Neutral:**
- Plural forms and complex ICU rules are not supported — we use simple `{placeholder}` substitution. If we ever need plural handling, we can upgrade to the `Babel` library without changing the `t()` signature.
