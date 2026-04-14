# Changelog

All notable changes to Backup Sentinel are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

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
