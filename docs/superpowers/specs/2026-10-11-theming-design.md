# Theming: smile eyes corporate design + configurable colours

Date: 2026-10-11 · Status: approved in chat, spec for review

## Goal

Backup Sentinel looks like a smile eyes product by default (official brand
palette and font), and every installation can switch to a neutral preset or
override four base colours in the settings — without ever producing an
unreadable UI.

## Decisions (from the conversation)

- Default theme for **all** installations: **smile eyes**. A **Neutral** preset
  keeps today's colours.
- Customisation: **preset + 4 base colours** (primary, accent, background,
  running). Everything else is derived.
- Font: **Instrument Sans** (SIL OFL 1.1). The smile eyes website ships it
  renamed as "SmileSans"; verified glyph-by-glyph (4 weights, ≤ 0.2 px at
  100 px). Self-hosted, no request to Google.
- Status colours (ok / warning / critical / failed / deleted) are functional,
  not brand colours; only their tone is tuned per preset.

## Brand palette (marketing)

| Name | Hex | Role in the app |
|---|---|---|
| Dark Blue | `#141450` | primary: topbar, ink, active filter, card outlines |
| Neon Yellow | `#FFFF00` | accent: the one primary action per page (sync, save), active filter in dark mode — always with Dark Blue text, never as text colour |
| Street Blue | `#4A7EB5` | running backups (status bar, row fill, spinner), links |
| Light Beige | `#F8F5F2` | page background (light) |
| Beige | `#EFE7DB` | borders, dividers, empty days in the 30-day bar |
| Light Sage | `#D8DDCE` | calm info surfaces ("offen", "gelöscht", hints) |

Dark mode is derived from Dark Blue (page ≈ `#0C0C2E`, cards ≈ `#121240`);
Neon Yellow stays the accent, Street Blue is lightened for contrast.

## Architecture

### Tokens

`static/style.css` keeps all CSS custom properties. Their **default values
become the smile eyes theme** (light under `:root`, dark under
`html[data-theme="dark"]`). Components keep reading tokens only; a few
hard-coded colours (e.g. `.node-filter-button.is-active` in dark mode, header
gradient) move to tokens.

New tokens: `--accent-fill` / `--accent-ink` (primary action), `--sage` /
`--sage-ink` (calm surfaces). `--running*` gets Street Blue.

### Theme module — `app/services/theme.py` (pure, no I/O)

- `PRESETS = {"smile_eyes": BaseColors(...), "neutral": BaseColors(...)}` —
  four hex colours each.
- `derive_tokens(base) -> dict[str, dict[str, str]]` — returns light and dark
  token maps (e.g. `--bg`, `--panel`, `--line`, `--chip`, `--header-bg`,
  `--running-soft`, …) computed by mixing the base colours in sRGB.
- `contrast_ratio(a, b) -> float` (WCAG 2.x relative luminance) and
  `check_contrast(base) -> list[Warning]`: ink on background ≥ 4.5,
  accent-ink on accent ≥ 4.5, header text on primary ≥ 4.5, running on
  background ≥ 3.
- `render_css(tokens) -> str` — `:root{…}` and `html[data-theme="dark"]{…}`.
- `normalize_hex(value) -> str | None` — validates `#rgb` / `#rrggbb`.

### Storage

`app_settings` keys: `theme_preset` (`smile_eyes` | `neutral`),
`theme_primary`, `theme_accent`, `theme_background`, `theme_running` (empty =
use preset). Saved with the existing `save_settings` + settings audit log.

### Rendering

`common_context()` resolves the active base colours (preset + overrides),
caches the rendered CSS per settings version, and `base.html` injects it as
`<style id="theme">` **after** `style.css`. Without stored settings nothing is
injected: the stylesheet defaults are already the smile eyes theme.

### Font

`static/fonts/instrument-sans-{400,500,600,700}.woff2` (latin + latin-ext) and
`static/fonts/OFL.txt`; `@font-face` in `style.css`, body font
`"Instrument Sans", system-ui, sans-serif`. The brand wordmark keeps its
current font. Downloaded once from the upstream release / Google Fonts, not
from smileeyes.de.

### Settings UI

New card **"Erscheinungsbild"** on the settings page: preset select, four
`<input type="color">` with hex text, "Auf Vorlage zurücksetzen", and a small
live preview (topbar, primary button, status badges, running bar) updated by
JS before saving. On save the server runs `check_contrast`; warnings are shown
with the measured ratio, saving is still allowed. DE/EN strings via i18n.

### PDF

`pdf_reports.py` replaces the hard-coded `C_HEADER` / `C_ACCENT` /
`C_CLUSTER` with the active theme (primary, accent, light tint of primary).

## Error handling

- Invalid hex input → field error, nothing saved.
- Unknown preset → fall back to `smile_eyes`.
- Theme resolution never raises during page render; on any error the page uses
  the stylesheet defaults.

## Testing

- Unit: `contrast_ratio` against known pairs (black/white = 21), `derive_tokens`
  determinism and token completeness, `check_contrast` flags yellow-on-white,
  `normalize_hex`, `render_css`.
- DB: save/load of theme settings, preset fallback.
- Manual (dev preview): smile eyes and neutral, light and dark, desktop and
  phone; settings preview and contrast warning; PDF header colours.

## Out of scope

- Editing status colours or per-token overrides.
- Per-user themes (one theme per installation).
- The commercial "condor" display font used on the website (Adobe Fonts).
