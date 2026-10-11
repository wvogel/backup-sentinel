# Changelog

All notable changes to Backup Sentinel are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [v2.9.0] — 2026-10-11

### Added

- **smile eyes corporate design as the default theme**, using the official palette: Dark Blue `#141450` (topbar, text), Neon Yellow `#FFFF00` (the main action per page — sync, save, PDF snapshot — always with Dark Blue text), Street Blue `#4A7EB5` (running backups), Light Beige `#F8F5F2` (background), Beige `#EFE7DB` (borders), Light Sage `#D8DDCE` (calm badges such as "offen"). Dark mode is derived from Dark Blue.
- **Font Instrument Sans** (SIL OFL 1.1) — the font smileeyes.de ships as "SmileSans" — self-hosted from `static/fonts/` with its license; no request to Google.
- **Settings → Erscheinungsbild:** presets *smile eyes* / *Neutral* (previous colours) plus four overridable base colours (primary, accent, background, running) with live preview, WCAG contrast warnings and reset. All other tones are derived; changes are audited.
- The **PDF report** follows the theme (banner, table heads, cluster headings); text on header surfaces is chosen by contrast so every theme stays readable.

## [v2.8.1] — 2026-10-11

### Changed

- **VM table rows are single-line again.** Backup badges and the policy dropdown sit side by side, the restore badge and interval picker too; both dropdowns are compact.
- **Narrower 30-day bars instead of wrapped cells:** 6 px per day with 2 px gaps (246 px). If the table doesn't fit on one line, the auto-sizer first narrows the bars (down to ~2 px per day) and only then lets name columns wrap. It also re-fits when the table's container changes width.

### Removed

- The "fremde Backups" badge from v2.8.0 — it also fired after a VM rename, which happens regularly. The guest name from backup notes is still stored but not shown.

## [v2.8.0] — 2026-10-11

### Added

- **VMID collisions are detected.** The guest name from each backup's notes (PVE `notes` / PBS `comment`, filled by vzdump's `--notes-template '{{guestname}}'`) is stored in `backup_events.guest_name`. A VM with backups of the last 31 days that belong to a different guest gets a **"fremde Backups"** badge with name and count — typically two clusters writing the same VMID into one PBS datastore without namespaces, or a renamed VM. Warn-only: such backups are still counted, so a rename never causes false "critical" alerts.

## [v2.7.0] — 2026-10-11

### Added

- **Restore interval per VM** via a picker next to the restore badge: 90 days, 180 days (default), 365 days or no test required (stored as `vm_policies.criticality` / `restore_required`). Weekly backups use at least 365 days.
- **Restore test for several VMs at once:** the form's VM list is a multi-select; all entries are validated before any is saved.

### Changed

- **Never-tested VMs are "offen" (open) instead of "kritisch".** They still count as due ("Restore-Tests fällig", metrics, reports) but no longer as critically overdue — "kritisch" now means an expired test. Previously 96 of 97 VMs showed "kritisch", which drowned out real problems.
- Default restore interval for daily backups is now 180 days (was 90); set a VM to "90 T" for the old behaviour.
- VM table: the backup-policy dropdown sits below the status badges and the restore picker below or next to the restore badge, so the 30-day bar keeps its full width (8 px per day).

### Fixed

- Changing a VM's backup policy silently reset its restore requirement; the two settings are now independent.

## [v2.6.0] — 2026-10-11

### Added

- **Slow backups are flagged.** Each VM's typical runtime is the median of its last 10 successful backups (from the task logs, needs at least 3). A running backup whose projected runtime (ETA, else runtime so far) exceeds 1.5× that and is at least 15 minutes longer shows "langsamer als üblich (sonst ~5 min)" in the status bar and row tooltip, and triggers a new **"Backup langsam"** notification once per backup run (Gotify, notification log, PDF).

## [v2.5.0] — 2026-10-11

### Added

- **Details from finished backup jobs.** Each sync reads the logs of vzdump tasks that finished since the last run (looking back 4 days, so multi-day jobs are caught once they end) and attaches per VM: runtime, data transferred, average throughput, share reused from the previous backup, and whether it was a full run (invalid or new dirty bitmap). They show in the 30-day bar's tooltip, e.g. "08.10.: ok · 42 GB · 5 min · ⌀ 43.6 MiB/s · 90 % wiederverwendet" or "… · Voll-Lauf". Best effort: unreadable logs never fail a sync. New columns `backup_events.transferred_bytes/throughput_bps/reused_pct/full_backup`, new table `processed_backup_tasks`.
- **Database tests** (`tests/test_db.py`) against a real PostgreSQL for deleted-VM handling, storages, task-log details and the 30-day data. They run in GitHub and GitLab CI (throwaway service DB, `BSENTINEL_DB_TESTS=1`); see CONTRIBUTING for running them locally.

## [v2.4.7] — 2026-10-11

### Added

- **Read/write throughput for running backups** in the status bar and row tooltip (e.g. "Lesen 154.5 MiB/s · Schreiben 154.5 MiB/s"), taken from vzdump's latest progress line — an average since the previous line, not an instant value.
- **Tooltip per day in the 30-day bar:** date, status and size (e.g. "08.10.: ok · 42 GB"). The bar is now built from HTML elements instead of SVG so it can use the app's instant tooltip; size and colours are unchanged.
- `/healthz` returns the running `version`, so a deploy can be confirmed with a single request.

### Changed

- The `svglib` pin moved to `requirements-nodeps.txt`, used by Dockerfile, GitHub CI and GitLab CI; local setup (README, CONTRIBUTING) installs it the same way, so the PDF tests also run locally.

## [v2.4.6] — 2026-10-10

### Changed

- The compact backup-policy badges (`d` / `w` / `–`) show their full name (täglich / wöchentlich / nie) in the app's instant hover tooltip instead of the delayed native browser tooltip.

## [v2.4.5] — 2026-10-10

### Changed

- **VM table no longer has its own scroll box.** It was limited to the window height and scrolled inside the page; with the running-backups bar and taller rows only about a third of the VMs fit, and the rest hid behind an easy-to-miss inner scrollbar. The table now grows with its content and the page scrolls; on screens ≥ 1200 px the table header sticks right below the topbar.

### Fixed

- Sticky elements assumed a 52 px topbar, which no longer matched the slimmer header; the topbar height is now measured (`--topbar-h`).

## [v2.4.4] — 2026-10-10

### Changed

- **Calmer 30-day sparkline colours in dark mode.** The bars used the bright pastel text colours (`#8dd3b0` green, `#f4a3ad` red), which glare as large fills. They now have their own muted dark-mode colours (green `#2e7356`, orange `#8a6428`, red `#9b3a46`); text, badges and light mode are unchanged.

## [v2.4.3] — 2026-10-10

### Fixed

- **Dashboard missed long-running backups.** The "N Backups laufen" badge was based on the sync's in-progress markers, which are treated as aborted after 6 hours — a 37 TiB backup running for over a day disappeared from the overview while the cluster page (live) still showed it. The dashboard now asks PVE live for all clusters in parallel (max. 6 s); a cluster that is unreachable or too slow falls back to the markers.

### Changed

- Live running-backup data is cached for 20 seconds per cluster and language and shared by dashboard, cluster page and status endpoint.

## [v2.4.2] — 2026-10-10

### Changed

- **Compact backup-policy badges** in the VM table: `d` (daily), `w` (weekly), `–` (never); the full label is in the tooltip. The freed width goes to the policy dropdown, which now shows "wöchentlich" in full.

### Fixed

- The accent bar of a running backup's row covered the start of the node name; the first cell is now indented in running rows.

## [v2.4.1] — 2026-10-10

### Changed

- **"Last backup" column shows only the date.** The running-backup chip is gone; percentage, data written and ETA live in the status bar above the table, the row fill shows progress, and hovering the row shows start, ETA and data volume.
- **Much larger 30-day sparkline:** 8 × 22 px per day with 3 px gaps. It takes the freed width and scales with its column (200–338 px), so the table never scrolls horizontally.

### Fixed

- Badges and pills no longer wrap mid-word when a column gets narrow (e.g. "kritisc/h" in the restore column).

## [v2.4.0] — 2026-10-10

### Added

- **Running backups stand out.** A status bar above the VM table lists every running backup with progress bar, data written and expected finish; clicking an entry scrolls to the VM (resetting the node filter if needed) and highlights the row. The row itself is filled up to the current percentage with an accent bar on the left, and its chip reads e.g. "47 % · bis 23:30" (tooltip: start, ETA, data volume). Replaces the small pulsing badge.
- **Live refresh.** Running backups update every 30 seconds (every 2 minutes when idle) without reloading: new backups appear, finished ones drop out.
- **Dashboard:** each cluster card shows "N Backups laufen" (from the last sync's in-progress markers; tooltip lists the VMs).

### Changed

- **Larger 30-day sparkline** (6 × 16 px per day instead of 3 × 8 px); empty days are slightly more visible.

### Fixed

- Several JS texts on the cluster page referenced translation keys that don't exist and showed the raw key (`js.backup_running`, `js.sync_completed`, `js.data_refreshing`, …); they now use the existing keys.
- On phones, the last-backup date next to a running-backup chip wrapped character by character.

## [v2.3.4] — 2026-10-10

### Fixed

- **Backup progress start time was off for large VMs** (e.g. 20:35 instead of 20:05). v2.3.3 derived the VM start as "now − elapsed", assuming the latest progress line had just been written — but large VMs log progress only every ~30 minutes. The VM start is now read from its own log section: the PBS archive name (`vm/103/2026-10-09T18:05:06Z`, UTC) or, for non-PBS targets, `Backup started at …` (node local time). The ETA is VM start + elapsed × 100 / percent and no longer depends on when the page is viewed.

## [v2.3.3] — 2026-10-10

### Fixed

- **Backup progress tooltip showed a start time and ETA in the past.** "Start" was the start of the whole vzdump job and the ETA was job start + the current VM's extrapolated runtime — wrong for every VM but the first in a multi-VM job (e.g. "Start 19:30, ETA 19:36" while the backup was still at 25 % at 21:3x). Start and ETA now refer to the VM currently being backed up (start = now − its runtime, ETA = now + remaining time). The job start is still shown until the VM reports its first progress line.
- Tooltip times and the "sync failing for 24h" notification are now formatted explicitly in `BSENTINEL_DEFAULT_TIMEZONE`, like the rest of the UI, instead of relying on the container's local time zone.

## [v2.3.2] — 2026-10-10

### Fixed

- **Running-backup indicator showed the wrong VM.** A scheduled vzdump job backs up several VMs one after another in one task; the VMID was taken from the *first* "Starting Backup of VM …" line, so e.g. VM 100 was shown as running while VM 103 was being backed up. Progress, delta and ETA were also read from the whole job log and could show a finished VM's 100 %. Only the log section of the VM currently running is evaluated now.

### Added

- **Deleted VMs show where their remaining backups are.** The "gelöscht"/"deleted" badge tooltip lists the storages still holding a backup (e.g. `Backups noch auf: pbs-fh-muc`). Each backup now records the set of storages it was seen on in the last sync (new column `backup_events.storages`) — one backup can sit on several storages, such as a PBS datastore plus an offsite sync copy attached in PVE.
- **Notifications and sync logs in English.** Gotify/email alerts, the monthly report email, the PVE/PBS sync logs and the auto-generated report title follow `BSENTINEL_REPORT_LANGUAGE` (`de`/`en`), which now acts as the server-side language for everything generated without a browser. Request-bound texts (Gotify/email test results, backup progress tooltip, report page title) follow the user's UI language.

## [v2.3.1] — 2026-10-10

### Fixed

- **Wrong plural in the dashboard cluster badge** ("2 Warnung" → "2 Warnungen", "2 warning" → "2 warnings"); the stat tiles below the numbers now read "Warnungen"/"warnings".
- **Untranslated or inconsistent UI texts**: German UI no longer shows "Copy", "Copied", "stale" or "pending"; consistent hyphenation ("PBS-Sync-Log", "Live-Sync-Log", "Restore-Tests", "PBS-Bootstrap"); hardcoded template strings (copy buttons, stale badge, PBS bootstrap heading, SSL/STARTTLS options, log heading) now go through i18n; smoother English wording ("Delete cluster?", "Refreshing page …", "Updating data …").
- **Sparkline label** said "14 days" although the chart shows 30 days.
- **German notifications**: "vor 1 Tag" / "1 Tag überfällig" instead of "Tagen"/"Tage"; "Backup-Größenanomalie"; "Zugangsdaten" instead of "Credentials".
- **German PDF report** used the English column abbreviations "Encrypted"/"Verify"; now "Verschl."/"Verif.".

### Documentation

- German user and admin guides now use real umlauts and ß instead of ae/oe/ue/ss transliterations; table-of-contents anchors updated accordingly.

## [v2.3.0] — 2026-10-10

### Added

- **VMs deleted from Proxmox are now detected.** Each sync flags VMs that PVE no longer reports (new column `vms.missing_since`; cleared again if the VMID reappears). While backups of such a VM still exist it stays in the cluster table, greyed out with a **gelöscht**/**deleted** badge; it no longer counts as critical, triggers no backup-critical notifications and needs no restore tests.
- **Deleted VMs without any remaining backup are removed from the database** at the end of the sync (cascading to policies, restore tests and backup events). The cleanup is skipped — with the reason in the sync log — if PVE returned 0 VMs or any PVE node, PVE backup storage, PBS connection, PBS datastore list or PBS datastore could not be read in that sync. As a second safeguard, such VMs are also filtered out of all views.

### Fixed

- **Backups of deleted VMs were wrongly flagged as removed.** Both the PVE storage scan and the PBS snapshot scan only considered VMIDs currently present in PVE, so snapshots of deleted VMs that still existed on a storage or PBS were marked `removed_at` on every sync. All VMIDs ever seen in the cluster are now included; affected backups are restored on the next sync.

### Changed

- A PBS connection whose datastore list or any single datastore fails is now reported as failed instead of OK.
- Slimmer header bar and smaller brand logo.

## [v2.2.11] — 2026-06-09

### Changed

- **Restore-Test column in the cluster overview now shows just a status badge** (`ok` / `Warnung` / `kritisch`) instead of the full result icon, recovery-type icon, date and duration. That detail was too noisy for an at-a-glance table; the date, duration, recovery type, interval and age are now in the badge's hover tooltip, and the full history remains in the "Letzte Restore-Tests" panel below.

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
