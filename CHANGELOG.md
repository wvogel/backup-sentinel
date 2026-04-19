# Changelog

All notable changes to Backup Sentinel are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

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
