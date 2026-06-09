# Changelog

All notable changes to Backup Sentinel are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [v2.2.10] — 2026-06-09

### Fixed

- **Setting a VM's backup policy to "auto" returned a raw JSON error** (`422 … backup_policy_override Field required`). The "auto" option submits an empty value, which the required `Form(...)` field rejected. The field now defaults to an empty string (`Form("")`); the handler already normalises `""` to "no override".
- **The Node column collapsed to ~5 lines when a restore "Warnung"/"Kritisch" badge appeared.** The badge widened the restore column, and the auto-sizer squeezed the first column with no lower bound (its `overflow-wrap: anywhere` let it shrink to a couple of characters). The wrappable text columns (Node, VM) now have a `min-width` floor, so they wrap to at most two lines and the restore badge drops below the date instead — a balanced layout. The mobile card view is unaffected.

## [v2.2.9] — 2026-05-29

### Documentation

- Updated the user guides (DE/EN) to cover the recent UI behaviour: the **restore-test due badge** and its interval-aware warning lead time (1 month for yearly, 2 weeks for quarterly intervals), and the **mobile card layout** of the VM table on narrow screens.

## [v2.2.8] — 2026-05-29

### Changed

- **Backup-status cell reads as a clean two-line card on mid-width screens (≤ 1100 px)** instead of a cramped 2×2 grid: the status badges (kind · severity · source) sit on the first row, the policy selector on its own row below. Wide screens keep the single-row layout; the mobile cards inherit the same tidy two-line arrangement.

## [v2.2.7] — 2026-05-29

### Fixed

- **Header wrapped onto a second row on medium screens** when the logged-in user's name was a long email. The user label is now hidden at ≤ 920 px (the logout icon stays) and the header actions never wrap, so the bar stays a single row at every width.
- **Backup-status pills overlapped** (`täglich`/`ok`, `PBS`/select) when the column was squeezed: the responsive 2-column grid used `minmax(0, …)` and could collapse below its content. Switched to `max-content` columns.

### Changed

- **Mobile cards are more compact.** Rich fields now sit horizontally (label left, value right) instead of label-on-top, so each card is shorter. The 30-day sparkline stretches to fill the row and is finally legible; the restore-test info stays on as few lines as possible.

## [v2.2.6] — 2026-05-29

### Changed

- **VM table becomes one card per row on phones (≤ 640 px).** Horizontal scrolling of a 7-column table hid most of each row; each row is now a labelled card (Node / VM / VMID / last backup / 30-day trend / backup / restore-test), so nothing is off-screen. Simple fields show label-left / value-right; rich cells (sparkline, backup controls, restore-test) put the label on top with the content below. The column auto-sizer stands down in this mode.

## [v2.2.5] — 2026-05-29

### Fixed

- **Header collapsed into a broken vertical column on narrow screens.** A `@media (max-width: 920px)` rule forced `.topbar`/`.topbar-actions`/`.topbar-user` to `flex-direction: column`, stacking the logo, nav icons, language/theme toggles and logout into a left-hand column. The header now stays a single horizontal bar at every width (its actions are compact icons); the user label is hidden below 640 px, leaving the logout icon.
- **VM table wrapped character-by-character on phones.** With the new column auto-sizer, very narrow viewports (e.g. 375 px) where even fully-wrapped columns can't fit caused names to break one letter per line. The auto-sizer now falls back to clean single-line cells with horizontal scroll when wrapping can't make the table fit.

## [v2.2.4] — 2026-05-29

### Fixed

- **Restore-test warning fired far too early.** The warning lead time was a flat 30 days before the due date regardless of interval. It is now interval-aware: **1 month** ahead for yearly intervals (≥ 180 days), **2 weeks** ahead for quarterly (or shorter) ones.
- **Missing translations `common.node` / `common.vm`.** The cluster VM-table headers used the singular keys, but only the plural `common.nodes` / `common.vms` existed, so the raw keys showed up. Added "Node" / "VM" in both locales.
- **Seven keys were missing from `en.json`** (`cluster.{pve,pbs}_sync_log_title`, `cluster.show_pbs_sync_log`, `cluster.{restore_tests,sync_log}_eyebrow`, `settings.{pve,pbs}_sync_log_title`). English users saw the raw keys because the fallback only resolves de→en. Both locales are now fully in sync (266 keys each).

### Changed

- **VM-table column layout now auto-sizes via JavaScript** to minimise wrapping. Every column is kept on a single line; only when the table is wider than its container do columns wrap, lowest-priority first (node → VM → restore-test), so as little wraps as necessary. Recomputed on load, resize, font load, and node/stat filtering.

## [v2.2.3] — 2026-05-29

### Fixed

- **Restore-due hover tooltip showed raw placeholders** (`Intervall: {interval} Tage, Alter: {age} Tage`) on the cluster detail page. The `cluster.interval_days` / `cluster.age_days` strings used `{interval}` / `{age}`, but the template passes `days=…`, so nothing was substituted. Both strings now use `{days}` to match the call site.
- **Gotify "backup critical" notification said "Letztes Backup: seit 3 Tagen".** Corrected to the grammatically right "vor 3 Tagen".

## [v2.2.2] — 2026-05-04

### Fixed

- **Successful backups stuck as "failed"** for VMs whose in-progress stub had been marked failed by cleanup before the real PBS snapshot rolled in. The `ON CONFLICT` rule in `sync_backup_events_for_source` now lets a real `size_bytes > 1` event override a stale failed status, and migration #1 backfills the existing DB to flip those rows from `failed` → `ok`.
- **Restore-test save crashed with HTTP 500** when the operator pasted a multi-KB Proxmox task log into `evidence_note`. The Pydantic `max_length` was 500 — raised to 50 000 (the DB column is `TEXT` anyway). Validation errors now return HTTP 400 with a readable message instead of bubbling up as a 500.
- **Cluster detail page dumped the full restore-test log inline.** Each restore entry now shows only date / result / duration plus a small "Log anzeigen" / "Show log" pill that opens the existing sync-log panel.
- **`backup_kind_label` rendered hardcoded German on the EN locale.** "täglich" / "wöchentlich" / "nie" now go through `t("cluster.policy_*")`, so the EN UI sees "daily" / "weekly" / "never" / "auto".
- **Sparkline showed yesterday as the rightmost dot** when viewed in the morning. The day-axis is now wall-clock today; the 12 h backup-day offset still applies to event bucketing, not to the axis itself.
- **Restore-test inline log click handler** generalized — any element with `data-log` now opens `#sync-log-panel`.

### Added

- DB migrations framework now has its first migration (re-classify failed-but-large `backup_events` as `ok`).

## [v2.2.1] — 2026-04-19

### Fixed

- **Pages crashed with `unhashable type: 'dict'`** after upgrading to FastAPI 0.135.3 / Starlette 0.40+. The `TemplateResponse` signature now takes `request` as the first positional argument; all five call sites were updated.
- **No sparkline dot for long overnight jobs.** A backup job starting at 20:00 and finishing the next morning landed on the wrong day — or was marked `failed` mid-run by the stub cleanup. Default `BACKUP_DAY_OFFSET_HOURS` is now 12h (noon→noon "backup day"), and `cleanup_stale_inprogress_backups()` only deletes stubs that have a later successful event for the same VM, or marks them failed after 48h of no successor.
- **SSL / certificate errors were retried.** The API client no longer burns 4.5s of backoff on errors that will never succeed on retry.
- **Background sync task was not awaited on shutdown** — now drained cleanly via `await sync_task` after cancel.
- **`notify_sync_overdue()` type hint** tightened from `object` to `datetime | None`, with consistent timestamp formatting.

### Changed

- Dependencies bumped via Dependabot: `fastapi` 0.116.1 → 0.135.3, `uvicorn` 0.35.0 → 0.44.0, `psycopg` 3.2.9 → 3.3.3, `pydantic` 2.11.7 → 2.13.2, `reportlab` 4.4.3 → 4.4.10, `python-multipart` 0.0.20 → 0.0.26.
- Docker base image: `python:3.12-slim` → `python:3.14-slim`. Dependabot is now blocked from auto-bumping the Python base minor/major.

### Added

- GitHub community files: `CODE_OF_CONDUCT.md`, issue/PR templates, `FUNDING.yml`.
- GitHub Actions workflows: `ci.yml` (lint + tests), `codeql.yml` (weekly security scan), `release.yml` (auto-release from CHANGELOG on tag push).
- Extended README badges: CI, CodeQL, release, last commit, stars.

## [v2.2] — 2026-04-14

### Added

**Documentation & Community**
- `SECURITY.md` with vulnerability disclosure policy and supported versions
- README badges (License, Python, FastAPI, PostgreSQL, Docker, ruff)
- `.github/dependabot.yml` for weekly pip, docker, and github-actions updates
- Proxmox compatibility matrix (PVE 8.x/9.x, PBS 3.x/4.x) + required token permissions
- Plain Nginx reverse-proxy config snippet in admin docs (EN + DE)

**Audit**
- `db.append_audit_event()` helper for lifecycle events
- New audit events: `cluster.created`, `cluster.renamed`, `cluster.deleted`, `cluster.bootstrapped`, `pbs.added`, `pbs.deleted`, `pbs.bootstrapped`, `restore_test.added`
- Settings audit view increased from 20 to 50 entries

**Database**
- Lightweight migrations framework in `app/db_migrations.py` with numbered ledger (`schema_migrations` table)
- Connection pool tuned: `min_size=2`, `max_size=15`, 10s acquire timeout, 5min max idle

**PDF Report**
- Full i18n support via `BSENTINEL_REPORT_LANGUAGE` (de / en)
- Language-specific date/number formatting (DE: 1.234, EN: 1,234)

**Frontend**
- `/notifications` click-to-expand rows reveal full title + message with monospace formatting

**Tests**
- `test_i18n.py` — translation fallback chain, placeholder substitution
- `test_footer_parsing.py` — `_parse_footer_links` edge cases
- `test_api_client.py` — retry status code classification
- `test_notification_pdf.py` — label dict completeness + section assembly

### Changed

- `static/app.js` (528 lines) split into 6 thematic modules under `static/js/`: `common.js`, `bootstrap.js`, `lightboxes.js`, `tables.js`, `sync.js`, `settings.js`
- Admin docs updated with `BSENTINEL_REPORT_LANGUAGE` env var

## [v2.1] — 2026-04-14

### Added

**Notifications**
- `/notifications` page with paginated 30-day log, color-coded type badges, channel pills
- Notification log persistence — every Gotify / Email send is recorded in the database
- Notification filters (type / cluster / channel) on `/notifications`
- CSV export via `/notifications.csv` (preserves active filters)
- Persistent notification dedup cache (survives restarts, atomic across threads)
- Notification section appended to the monthly PDF report (NIS2 Art. 21 / 23 evidence)
- Bell icon in header nav linking to `/notifications`

**Observability & Security**
- Prometheus-compatible metrics at `/metrics` (cluster sync health, severity counts, unencrypted backups, restore overdue)
- `X-Request-ID` middleware for log correlation
- Security headers (CSP, X-Frame-Options, X-Content-Type-Options, Referrer-Policy, Permissions-Policy)
- API retries with exponential backoff for transient 5xx / network errors
- Non-root Docker user (uid 1000)
- Startup refuses to start if `BSENTINEL_SECRET_KEY` is missing or invalid
- Direct `/favicon.ico` route

**Documentation**
- `CONTRIBUTING.md` with development setup, testing, and PR guidelines
- `CHANGELOG.md` following Keep a Changelog format
- Architecture Decision Records (ADRs) in `docs/adr/`
- PNG versions of architecture diagrams for blog posts

**Tooling**
- Pre-commit hooks and ruff linter configuration (`pyproject.toml`)
- GitLab CI lint and test stages before deploy
- Tests for i18n, footer parsing, API retry logic, and notification PDF generation

### Changed

- `docker-compose.yml` now loads `.env` for the app container via `env_file`
- `BSENTINEL_INSECURE_SSL` default in `docker-compose.yml` is now `false` (was `true`)
- All dependencies in `requirements.txt` are pinned to exact versions
- Monthly report generation is now self-healing — generates missed reports on first run after month-end, using the configured timezone instead of UTC
- OAuth2-Proxy skip-auth routes default now includes `/healthz` and `/metrics`

## [v2.0] — 2026-04

The first open-source-ready release.

### Added
- Full internationalization (DE / EN) via cookie-based language switcher
- Light / Dark / Auto theme toggle (persisted in localStorage, no flash on load)
- Inline SVG icons throughout (replaces custom icon font)
- Fixed header navigation with active-state indicators (Home, Reports, Notifications, Settings)
- Architecture diagrams in German and English (`docs/architecture-{en,de}.svg`)
- Bilingual user and admin documentation (`docs/user-docs-{en,de}.md`, `docs/admin-docs-{en,de}.md`)
- `backup.sh` helper for PostgreSQL database backups
- `FOOTER_LINKS` environment variable (format: `Label1|URL1,Label2|URL2`)
- `COPYRIGHT_TEXT` environment variable

### Changed
- Credentials externalized from `docker-compose.yml` — all defaults are now placeholders
- CI/CD configuration uses only three variables (`DEPLOY_USER`, `DEPLOY_HOST`, `DEPLOY_PATH`)
- `APP_VERSION` hardcoded in `app/config.py` instead of built from git SHA
- Dockerfile simplified — single stage, no git-info step
- All hardcoded German text converted to translation keys (~170 keys)
- Dark mode driven by `html[data-theme="dark"]` instead of `@media (prefers-color-scheme)`
- CSS button variables (`--btn-bg`, `--btn-text`, `--btn-border`) now adapt per theme

### Removed
- Custom icon font (`static/icons.woff2`)
- `@media (prefers-color-scheme)` rules (replaced by theme toggle)
- Company-specific branding, URLs, and references from tracked files
