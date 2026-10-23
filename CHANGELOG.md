# Changelog

All notable changes to Backup Sentinel are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [v2.41.21] — 2026-10-23

### Fixed

- **Duplicate PBS events: keep the correct (smaller) size.** Migrations 3 and 4 deleted the row with the higher DB id — which was the direct-PBS event with the correct snapshot size. The PVE-via-PBS event (lower id, inserted first) carried the inflated chunk-reference size, so the wrong value survived. Migration 5 now keeps the row with the smaller `size_bytes` and widens the match window from 90 s to 120 s.

## [v2.41.20] — 2026-10-23

### Fixed

- **Duplicate backup events now physically deleted instead of marked as removed.** Migration 3 used `removed_at` to hide duplicates, but `removed_at` means "deleted by retention" — causing the sparkline to show false deletions and the history query to still count them. Migration 4 replaces this with a DELETE.

## [v2.41.19] — 2026-10-23

### Fixed

- **Duplicate backup events from PVE-via-PBS and direct PBS fetch.** PVE's storage content API and the direct PBS query both returned the same snapshots. When their timestamps differed by up to a minute (PVE uses `ctime`, PBS `backup-time`), the UPSERT created two rows instead of one — showing "2 Backups" on days that only had one, and sometimes inflated sizes (PVE can report the total referenced chunk size instead of the snapshot size). Events are now deduplicated before the DB sync; a migration marks existing duplicates as removed.

## [v2.41.18] — 2026-10-20

### Fixed

- **"Keine Backups in den letzten 30 Tagen" appeared after a failed API fetch.** When the history endpoint was unreachable (e.g. briefly after a deploy), the catch block showed the empty-state message instead of an error. Now it says "Verlauf konnte nicht geladen werden" / "Could not load history", and non-ok HTTP responses (502 etc.) are caught explicitly.

## [v2.41.17] — 2026-10-20

### Fixed

- **Architecture diagram uses the updated dark mode palette.** Surface colors (BG, panel, line) now match the deep-blue tones from v2.41.16's dark_surfaces preset.
- **Screenshot script improvements**: random CDP port avoids collisions, configurable color scheme parameter, better error handling.

## [v2.41.16] — 2026-10-19

### Fixed

- **A failed verify no longer stays red after a later backup verified OK.** Any failed verify in the last 7 days used to mark the VM critical, even when a newer backup had been verified successfully since. Now a failure counts only until a later backup verifies OK. A newer backup that is not verified yet does not clear it — it shares most chunks with the failed one — and the tooltip now says so ("No later backup verified yet").

## [v2.41.15] — 2026-10-18

### Fixed

- **Bootstrap scripts could not be downloaded** (`/bootstrap/proxmox-agent.sh`, `/bootstrap/pbs-agent.sh` answered 404) since v2.41.13, so no cluster or PBS could be added or re-bootstrapped. The cleanup that removed the unused connection-status endpoint took the two routes after it along. They are back, with a test.

## [v2.41.14] — 2026-10-18

### Security

- **The API accounts get far fewer rights.** On PBS the bootstrap script now grants the read-only role `Audit` instead of `Admin`. On PVE the custom role `BackupSentinelBackupList` (`Datastore.AllocateSpace`, `VM.Backup`) replaces `PVEDatastoreAdmin` on `/storage`, next to `PVEAuditor`: PVE lists backup volumes only with one of these two, and the new one can neither change the storage configuration nor delete VM disks or ISOs. A re-run of the bootstrap command takes the old roles back — run it once per cluster and once per PBS (see the Admin Guide).
- **PBS certificate fingerprint from the TLS connection** instead of PBS's certificates API, which needs `Sys.Modify`.

### Fixed

- **"Sync complete" dialog on the cluster page.** Its markup only existed on the settings page, so the cluster page reloaded without it.

### Changed

- **One parser for backup logs.** Sizes, start lines and archive lines of vzdump logs are read by one set of functions instead of two copies; units are accepted in any spelling.
- **One sync poll.** The cluster page used its own copy of the live-sync polling; it now uses the shared script, which also picks up a sync that is already running when the page loads.
- **Error messages in the chosen language:** invalid date, duplicate cluster name and an already registered cluster. Remaining German log messages are now English.
- Removed the unused connection-test helpers.

## [v2.41.13] — 2026-10-18

### Fixed

- **Restore tests "this month" counted the same month of earlier years** too; now only the current month of the current year.
- **Report archive showed "adhoc"** as the mode of reports made with "PDF snapshot"; they now say "manual" (older entries too).
- **Delete-cluster dialog** nested the cluster name in two bold tags.
- **Confirm dialog** had no background (it used colour variables that don't exist); it now uses the card colours.
- **PBS sync log** no longer starts with a raw "DEBUG:" line of PBS JSON.
- **Gotify description in the settings** now lists everything that is sent, not only syncs, critical backups and anomalies.

### Changed

- **Cleanup.** Removed the unused `/api/connection-status` endpoint, five unused texts, an unused PDF label and unused data on the overview. Manual and monthly reports are written by one shared function instead of two copies. The header links and toggles take their colours from the theme, so custom header colours reach them. The stylesheet lost about 90 lines of duplicate, dead and overridden rules and needless `!important`s and got tokens for pill radius and monospace font — the pages look the same, checked by pixel comparison.
- **Documentation brought in line with the code**, in German and English: CI/CD (webhook deploy, no SSH), the rights the bootstrap scripts really grant (PBS: Admin), the routes without login, all environment variables with their real defaults, the external Docker networks, which alerts go by Gotify and which by email, quiet hours, the notifications page, the report archive, appearance settings, theme and language, `backup.sh`, the Prometheus metrics and the dev setup. `.env.example` no longer suggests an invalid secret key.

## [v2.41.12] — 2026-10-18

### Fixed

- **Running backups no longer count as unencrypted.** While a backup runs, PBS has no manifest for it yet and lists its files without an encryption mode. That was read as "unencrypted", so every running backup lowered the encryption tile and raised the "unencrypted backups" warning and notification. A missing encryption mode now means "not known yet"; the state is filled in once the backup has finished.

## [v2.41.11] — 2026-10-18

### Changed

- **Documentation brought up to date.** README features list (topology diagram, live progress, log dialog, weekly report, Prometheus metrics); user guides in German and English (overview rows with sync symbols, KPI names, cluster page sync symbols and log dialog, replication progress per sync line); admin guides (version format). New screenshots of the overview and the cluster page in English and German, from neutral demo data. A new architecture diagram in both languages: sync worker and live progress against the PVE and PBS APIs, Prometheus, reports volume — no line crossing a box any more.
- **Reproducible doc assets:** `scripts/architecture_diagram.py` generates the diagram, `scripts/docs_demo_data.sql` and `scripts/docs_screenshot.mjs` produce the screenshots (see `docs/dev-local.md`).

## [v2.41.10] — 2026-10-18

### Changed

- **Settings page: coloured PVE and PBS symbols instead of dots,** as on the overview and the cluster page (same colours, tooltip with the last sync; a click opens the log).
- **Cluster page: each running replication's progress in its own sync line.** The progress rings sat side by side at the right of the datastore, without showing which sync they belong to. Each running sync line now starts with its own small ring, one under the other; "running since" is in the ring's tooltip instead of the line. Once a sync finishes, its line shows the tick and "just finished" again. Sync lines name the source namespace too ("← nas-b1/mar-lej").

## [v2.41.9] — 2026-10-17

### Fixed

- **Overview rows of different heights.** At about 1000 px, a cluster with a warning badge, a running backup and a running replication had no room for them next to its name: they wrapped onto three lines and that row grew (126–148 instead of 92 px), with the name moving down. The status badge ("ok", "2 Warnungen", "1 kritisch") is gone — the "kritisch" and "Warnungen" tiles show the same, highlighted — so only what is running right now stays next to the name. Short of room, those badges may also use the symbols' line; two lines still fit the tiles' height, and the name keeps its place.

## [v2.41.8] — 2026-10-17

### Changed

- **Cluster page: PVE symbol and name in one line.** In the diagram the cluster's box had its symbol above the name, while the PBS boxes have it in the name's line. It now matches them: same symbol size, gap, font size and weight.

## [v2.41.7] — 2026-10-17

### Changed

- **Cluster page: no "PVE" label next to the PVE symbol.** The cluster's box in the diagram (and the header when there is no diagram yet) showed "PVE" under the name although the coloured server symbol already says it.

## [v2.41.6] — 2026-10-17

### Changed

- **Logs open over the page.** A click on a sync symbol (cluster page, settings) or on a restore test's note used to show the log in a panel at the bottom of the page, to be scrolled to, selected by hand and closed with a button. It now opens in a dialog in the middle of the screen, scrolled to the end, with a copy symbol (copies the whole log; a tick confirms it) and ×; Esc or a click beside it closes it too. A running sync's live log still streams into it.

## [v2.41.5] — 2026-10-17

### Changed

- **Cluster page: coloured PVE and PBS symbols instead of green dots.** In the diagram the cluster's server symbol and each PBS's datastore symbol now show the sync state themselves, coloured as on the overview (green, amber, red; grey before a PBS's first sync); the separate dots are gone. The tooltip names the state and the last sync, and a click still opens the sync log. Overview and cluster page share one rule for the colours. The settings page keeps its dots.

## [v2.41.4] — 2026-10-17

### Changed

- **Overview key figures in one line.** "Restore-Test-Abdeckung" wrapped onto two lines at about 1000 px and made all four tiles taller (115 instead of 96 px). It now reads "Restore-Abdeckung" ("Restore coverage" in English), and the ring next to it is slightly smaller (48 instead of 56 px).

## [v2.41.3] — 2026-10-17

### Changed

- **Cluster rows on the overview as low as before v2.41.2.** Status, running backups and replications are back on the cluster name's line, in a column next to it; the sync symbols stay under the name. The key-figure tiles keep their own height again (92 px rows at 1920 px, as in v2.41.1).

## [v2.41.2] — 2026-10-17

### Changed

- **Cluster rows on the overview: name, sync symbols, current state.** The single green dot before the name is replaced by a line of small symbols under it: one for PVE and one per PBS, each coloured by how current its own sync is (green, amber, red; grey for a PBS not synced yet). A failed PBS sync is visible again on the overview — the cluster strip removed in v2.41.1 had shown it. One tooltip covers all symbols (PVE state and last successful sync, error state, each PBS's last sync). Status, running backups and replications follow in the third line. The key-figure tiles now fill the row's height, centred.

## [v2.41.1] — 2026-10-17

### Removed

- **The cluster strip at the top of the overview.** It listed each cluster with a status dot, which the cluster rows right below already show (name, sync dot with last sync and errors, warnings). The overview now starts with the key figures. Its CSS, texts and the helper that estimated failed syncs for it are gone too.

## [v2.41.0] — 2026-10-17

### Fixed

- **Progress of the wrong VM in long backup jobs.** The live progress read only the first 5,000 lines of a running job's task log. A job that backs up many VMs writes more than that, and then the running VM's start and progress lines were cut off: the badge could show a VM that had already finished. Now the log's line count is read and, when the log is long, only its end back to the running VM's "Starting Backup" line.

### Changed

- **Sizes, rates and durations without false precision.** One shared formatter (`app/services/human_format.py`) for the web pages, the VM history charts, the PDF report and the size-anomaly notification: the unit is scaled so the number stays below 1000, with one decimal below 10 and whole numbers above, in binary units like the Proxmox GUI. German uses a decimal comma, English a decimal point. The datastore tooltip read "5.466 GB von 38.929 GB belegt" and now reads "5,3 TiB von 38 TiB belegt"; throughput "43.6 MiB/s" becomes "44 MiB/s", and the running-backup banner shows "867 GiB von 1,6 TiB" instead of the raw log text. Durations from 10 hours on drop the minutes ("51 h" instead of "51 h 23 min"). Sizes used to be labelled "GB" although they were computed in GiB. Counts in English now use a comma as thousands separator. Exports, API fields and sort keys keep the raw values.
- **Live backup progress asks PVE much less.** Each refresh reads only the task-log lines written since the previous one (per running task, the running VM's section is kept in memory), instead of the whole log every 20 seconds. Nodes and tasks are asked in parallel over kept-alive connections, once each (no retries: the next poll asks again). One fetch per cluster now serves German and English viewers (labels are added per request), tabs that find the cache stale at the same moment wait for the same fetch, and the slow-backup check of each sync uses the same cache instead of its own fetch.

## [v2.40.7] — 2026-10-17

### Changed

- **The overview loads each thing once.** The per-VM query (five lookups per VM into the backup history) ran three times per page load — for the cluster summaries, the VM list and the restore-test coverage — and now runs once and is handed on. The cluster list was read twice, the sync state was read again per cluster, the node list once per cluster, and the encryption totals over all backups were counted a second time; all of this now comes from data already loaded. The weekly report shares its VM query the same way, and the cluster page reads its nodes once.
- **Indexes:** the restore tests per VM (looked up for every VM on each overview, and scanned on every VM delete) and the processed backup tasks per cluster are now indexed. Three indexes that duplicated a UNIQUE constraint's own index are dropped.

### Fixed

- **Running-backup labels in the wrong language.** The 2-second cache of the cluster page's status poll was shared by all viewers, so a German and an English tab could get each other's labels; it is now kept per language.

### Removed

- `scripts/monthly_report.py` and the code only it used (`build_monthly_report`, `write_monthly_report_pdf`, `list_vms`): nothing called the script any more, and its report no longer matched what the PDF writer expects. Monthly reports come from the reports page and the automatic run. Also removed: `get_setting` and a German-only notification label table, both only used by tests (now testing the live functions), and an unused CSS class; duplicate CSS rules merged (no visible change — computed styles of all ~2,800 elements on every page compared before and after).

## [v2.40.6] — 2026-10-17

### Removed

- **The Smile Sans / Instrument Sans web font.** The page now uses the device's system font (San Francisco on macOS/iOS, Segoe UI on Windows, Roboto on Android). The two font files under `static/fonts/` are gone; nothing is downloaded, so the page no longer flickers and reflows when the font arrives after a reload. The brand wordmark is unchanged.

## [v2.40.5] — 2026-10-17

### Fixed

- **Arrowheads cut off by the boxes after a reload.** Since v2.40.3 the diagram's boxes are as wide as their content and the spare room goes to the arrows. On a fresh load the lines were drawn before the web font had arrived; with the real font the boxes grew and moved, but the diagram kept its size, so nothing triggered a redraw and the boxes covered the tips. The diagram now also redraws when a column changes size (at most once per frame) and once the fonts have loaded.
- **No empty room on the right of a PBS box with several datastores.** The datastores wrapped below each other but the box kept the width they would need side by side; they are now always listed one under the other, so the box fits them.

## [v2.40.4] — 2026-10-17

### Changed

- **Tighter, plainer look.** Radii and spacing came down one step, still one measure for everything: corners 10 px for cards, 6 px for everything inside them, 4 px for small tags (were 14/10/6); 12 px between any two boxes and from the header to the first card, 12 px inside cards, 10 px inside boxes (were 16/16/12). Spacings that were still written out (health bar, card lists, report and encryption-alert bodies, code blocks, bootstrap panel, page top) now use the same values.

## [v2.40.3] — 2026-10-17

### Changed

- **The same spacing everywhere, as seen, not just as set.** v2.40.2 unified the values in the stylesheet, but on screen the room still differed: key-figure tiles 8 px apart, running tiles 10 px, cards 16 px; diagram boxes stretched to their column with their content centred (about 140 px of empty border in one box, 20 px in the next); running tiles centred like buttons. Now every gap between two boxes is 16 px (cards, tiles, diagram boxes, running tiles), every box has 12 px inside on all four sides, and cards 16 px. Diagram boxes are as wide as their content; the spare room goes evenly to the arrows, and content sits centred vertically so the room above equals the room below. Key-figure tiles are all as wide as the widest.
- The problem filter is now called **"Probleme (n)"** instead of "Nur Probleme (n)" ("Problems" in English).

### Fixed

- **Nothing runs over on phones any more** (checked at 320 and 375 px on every page): the reports page was 544 px wide — its tiles and status columns now wrap and the archive shows one card per report; in the settings, the cluster box and the PBS boxes grew past the card (a fieldset is as wide as its content by default, the namespace field was fixed at 14 rem); in the VM cards the 30-day bars ran past the card edge (the oldest days now drop off the left), the status icon was squeezed to nothing, the first line was indented differently and the zebra tint sat inside the card's padding; key-figure tiles switch to two per row when three labels do not fit; a running sync's ring moves below its text instead of covering it.
- On the reports page the "automatisch" pill no longer covers the PDF button in the archive.

## [v2.40.2] — 2026-10-17

### Changed

- **One measure for all boxes.** Corners and inner spacing were set box by box (nine radii from 1 to 24 px, paddings like 14/16, 8/10, 10/14, 8/12/10). They now follow three radii and one spacing grid, defined once in `:root`: 14 px for cards and dialogs, 10 px for everything inside a card (key-figure tiles, diagram boxes, fields, buttons, filters), 6 px for small tags and header buttons; status pills stay round. Cards have 16 px inside on all sides, boxes inside them 10/12 px, the cluster header 16 px between key figures and diagram. Borders are 1 px in the line colour everywhere; only a state (warning, unregistered PBS) colours or dashes them.

## [v2.40.1] — 2026-10-17

### Fixed

- **Animations no longer jump back every 30 s.** The live refresh rebuilt the diagram and the running tiles, and every animation in them started over from zero. All endless animations (light streak, moving blocks, rings, row sweep) now stand where the wall clock says — time modulo their period, like the brand glow already did — so a refresh, a new running row or a page reload continues them seamlessly.

## [v2.40.0] — 2026-10-17

### Added

- **Replications that start or end are noticed live.** Until now a running PBS sync job only showed after Sentinel's next own sync (hourly). The cluster page now asks the PBS behind its diagram every 30 s for their sync jobs (at most every 30 s per PBS, shared by all viewers); when one started or ended, the jobs are stored, the diagram is rebuilt and the page redraws. The overview triggers the same check in the background and shows it from the next load.

### Changed

- **No more listing through PVE of what is read straight from the PBS.** For a PBS storage whose PBS is registered for the cluster, sees the datastore and reads its namespace, PVE is no longer asked for the same snapshots — PVE passing that list on took up to 8 s, the direct read 0.3 s. If a PBS cannot be read in a sync, its snapshots are kept as they are instead of being marked removed.
- Live checks (running sync progress, sync jobs) no longer wait for retries: they are asked again 30 s later anyway.

## [v2.39.2] — 2026-10-17

### Changed

- **Connections to PVE and PBS are kept open (keep-alive):** each sync thread reuses one connection per server instead of opening a new one — with name lookup and TLS handshake — for every API call. The live logs showed single calls hanging 2–5 s at random while the same call otherwise takes 0.1 s, typical for connection setup; this now happens at most once per server and sync. A connection the server has closed in the meantime is reopened at once, without waiting.

## [v2.39.1] — 2026-10-17

### Fixed

- **The light streak now runs on into the arrowhead** instead of stopping where the pipe ends; it stays inside the shape of pipe and head.

## [v2.39.0] — 2026-10-17

### Changed

- **Running pipes in the diagram move like a running VM row:** instead of data packets (dots) a soft light streak runs through the pipe, brightest in the middle and fading to both sides, also round corners and along thin lanes. On a replication it lights only the blocks, which keep moving; the gaps stay dark. Off with reduced motion.

## [v2.38.2] — 2026-10-17

### Fixed

- **Monthly report could not be written** ("Permission denied" on `/reports`): the app runs as an unprivileged user in the container, but the mounted host directories belonged to root. `deploy.sh` now hands `data/` and `reports/` to that user on every deploy, with UID/GID taken from the freshly built image; without root rights it warns instead of failing the deploy.

## [v2.38.1] — 2026-10-17

### Fixed

- **Sync several seconds faster per cluster:** calls whose failure is expected no longer wait for retries. PVE 9 answers the old HA-groups call with HTTP 500 — it was retried twice (1.5 s + 3 s) on every cluster; now groups are only asked on PVE without HA rules, once. Reading a storage's backups starts on a node where the storage is active, and a node that cannot answer ("storage … is not available on node") is skipped at once for the next one instead of being retried. Measured on the live log: 4.5 s per cluster, 9 s on pve-fh-muc.

## [v2.38.0] — 2026-10-17

### Added

- **Warning for overdue restore tests** (Gotify), switchable in the notification settings and off by default. Each sync reports guests whose last restore test is older than their interval — once per guest and day, with the days overdue. Guests never tested ("offen") and guests gone from PVE raise none. The warning existed but was never sent.

## [v2.37.2] — 2026-10-17

### Added

- **Sync log shows where the time goes:** a "Zeiten:" line after the duration lists every step, slowest first — PVE inventory, PBS matching (DNS/TLS), per PBS certificates, backups, usage and sync jobs, task logs, running-backup check, database, diagram and checks.

## [v2.37.1] — 2026-10-17

### Changed

- **Saving backup events during a sync is about 11× faster** (12,000 events: 1.4 s → 0.13 s): one batch instead of a database round trip per snapshot, and removed backups are marked in one statement. Same merge rules as before.

### Removed

- Dead code: unused database helpers (`upsert_backup_events`, `list_all_available_backup_events`), old PVE date parsing, legacy `proxmox_get_*` aliases, unused models and types, the unused `DATA_DIR` setting, 16 unused translations per language and 17 unused CSS classes (about 340 lines).

## [v2.37.0] — 2026-10-17

### Changed

- **Running badges on the dashboard are shorter:** "4 Backups", "2 Replikationen" — the turning spinner already says they run (the full wording stays for screen readers).
- **A running backup moves in its VM row,** not only as a tinted bar: a ring turns around the guest icon and a light streak runs along the filled part of the bar (the whole row while the percentage is unknown). Off with reduced motion.

### Fixed

- **Cluster names on the dashboard no longer break over several lines** next to many badges; the badges move to the next line, and the stat tiles stay in one row.

## [v2.36.0] — 2026-10-17

### Changed

- **"Sync · n VMs" counts only current copies.** Before, every guest with any backup in the copy counted — also one whose last copy is months old because the sync no longer brings it (e.g. 103 on pbs-aboe-muc, last copied in January), so the diagram looked fine while the protection column said "PBS ×1". Current means as in the protection column (7 days, 9 for weekly sync jobs). Guests with only older copies follow in yellow as "n veraltet"; the tooltip lists each with the date of its latest copy.

## [v2.35.1] — 2026-10-17

### Fixed

- **Offsite copies in sync target namespaces were still not seen** (e.g. `mar-lej` on pbs-aboe-muc: only an old snapshot from the root namespace counted, so "PBS ×1"). The namespaces to read were taken from the stored diagram, which a version before v2.35.0 had saved without the target connection — so they were skipped. They are now worked out from the current sync jobs at every sync. This, not the copy age, was the cause of the v2.35.0 report.

## [v2.35.0] — 2026-10-17

### Fixed

- **Weekly offsite copies dropped out of the protection count** ("PBS ×1" although the guest has an offsite copy, while "Sync · n VMs" counted it). A copy counted only if its latest backup was at most 7 days old — a weekly sync job's copy is up to about 8 days old before the next run. The window now follows the sync jobs feeding a server: 9 days for weekly jobs, 7 otherwise.

### Added

- **Protection tooltip lists every server with its latest copy**, marking too old ones as "älter als n Tage, zählt nicht".
- **Target namespaces of sync jobs are read automatically.** When a sync job copies this cluster's backups into a namespace on one of its PBS (e.g. `mar-lej` on pbs-aboe-muc), the PBS read covers that namespace too — no namespace setting needed, and other clusters' namespaces stay out (taken from the diagram, i.e. only namespaces fed from this cluster's own datastores).

## [v2.34.0] — 2026-10-17

### Changed

- **Overview: sync status as a dot instead of a text line.** Before each cluster name a dot shows how current its data is — green: the last sync succeeded and is at most two sync intervals old (one missed run is fine); yellow: older, or the latest attempt failed; red: no successful sync for over 24 hours (as the overdue notice), or never. The last successful sync and "error since" are in the tooltip; the "Letzter erfolgreicher Sync" line is gone, so the cluster cards are lower.

## [v2.33.0] — 2026-10-17

### Added

- **"Sync · n VMs" next to "PVE · n VMs".** A datastore that sync jobs copy into shows how many guests of this cluster actually have backups lying there — taken from where the sync found each backup (PVE storage contents and direct PBS reads), not from what the sync jobs are configured to copy — minus the guests whose PVE backup jobs write there directly. Shown in the replication colour; the tooltip explains it.

## [v2.32.0] — 2026-10-17

### Added

- **Templates are marked** with a quiet, greyed-out "TPL" next to the name in the VM table (tooltip: template, does not run, used for cloning). Read from PVE's cluster resources on every sync (`vms.is_template`).

## [v2.31.2] — 2026-10-17

### Fixed

- **HA state in the language of the interface** ("Zustand: gestartet" instead of "started"). All states PVE HA reports are translated (started, starting, stopped, stopping, request_stop, disabled, ignored, migrate, relocate, recovery, fence, freeze, error); an unknown one is shown as PVE names it.

## [v2.31.1] — 2026-10-17

### Fixed

- **"PVE · n VMs" counted pulled copies as direct backups.** The count came from the backups found on each PVE storage — but PVE lists the whole datastore, including the snapshots a PBS sync job pulls in. Now it comes from PVE's backup jobs: each enabled job's target storage and its guest selection (VMIDs/ranges, all minus excludes, pool, limited to a node). Stored per guest (`vms.backup_storages`).
- A PBS storage configured in PVE but used by no backup job no longer gets a "PVE backs up here" line — it only holds copies. (Before the first sync with job data, every configured storage still counts.)
- The PVE line that lights up for a running backup is likewise the one its backup jobs write to, not every storage holding a copy.

## [v2.31.0] — 2026-10-17

### Added

- **HA guests are marked** with a small blue "HA" next to the name in the VM table. It is outlined when the guest may only run on certain nodes (or never on some); the tooltip shows the HA state and the nodes, e.g. "Darf nur laufen auf: pve1 (2), pve2" (number = priority), "Bevorzugt auf: …", "Darf nie laufen auf: …" or "Darf auf allen Nodes laufen".
- Read on every sync from `/cluster/ha/resources` plus the node-affinity rules (`/cluster/ha/rules`, PVE 9: nodes with priority, strict, positive/negative, disabled rules ignored) or, on older PVE, the guest's HA group (`/cluster/ha/groups`, "restricted" = only these nodes). Stored per guest (`vms.ha`); if the HA config cannot be read, the last known state is kept.

## [v2.30.1] — 2026-10-17

### Changed

- **QDevice as a small "+Q"** next to the node count, muted like the label, with state and QNetd host on hover. The label stays "Nodes", so a cluster's tiles keep the same width as everywhere else ("Nodes + QDevice" widened the tile and shifted the row).

## [v2.30.0] — 2026-10-17

### Added

- **Quorum device in the nodes tile.** On the overview and the cluster page, a cluster with a corosync quorum device shows "Nodes + QDevice"; the tooltip names its state and QNetd host, and the tile turns yellow when the QDevice is not connected. Read on every sync from PVE's `GET /cluster/config/qdevice` (`Sys.Audit`, part of the bootstrap's `PVEAuditor` role) and stored with the cluster (`clusters.qdevice`); if it cannot be read, the last known state is kept.

## [v2.29.2] — 2026-10-17

### Changed

- **Live sync progress reads only the end of the task log** (last 200 lines) instead of up to 100 000 lines on every 30-second poll; whether the task still runs comes from PBS's own test-status ("active"), with the "TASK OK/ERROR" end marker as fallback for older PBS. Behaviour checked against the PBS source (`read_task_log`).
- **Internal cleanup, no visible change:**
  - Live sync progress lives in its own service (`app/services/sync_progress.py`); the overview no longer imports from the cluster page's routes.
  - Sync progress is no longer fetched during the background sync or stored (column `pbs_sync_jobs.running_percent` dropped) — the pages only ever show live values.
  - One query for the guests' recent backup storages; the per-storage guest count is derived from it.
  - `running_backups_for_clusters` renamed `cached_running_backups` (it only reads the cache).
  - `static/js/topology.js` split into clear steps (fit layout, measure, bundle per box pair, choose lanes, route, render); no recursion on lane changes.
  - Dead CSS of the removed sync text rows and status dots deleted.

## [v2.29.1] — 2026-10-17

### Fixed

- **Seam between pipe and arrowhead.** Line and head were dimmed separately, so where they meet the transparency added up to a visible edge; dimming and colour now apply to each connection as a whole.
- **Over-long arrows.** Gaps stay compact (88–130 px) and spare room goes to the boxes again; with many columns the gaps narrow step by step until the diagram fits, instead of overflowing.
- **The line around the boxes overlapped the broad pipe.** It now leaves and enters well clear of the pipe and its arrowhead.

## [v2.29.0] — 2026-10-17

### Changed

- **Replication diagram as pipes.** Direct connections are three times as broad (24 px) with a matching filled arrowhead; replications are pipes of blocks; lines running the long way around stay thin (5 px). At rest pipes are dimmed and still.
- **New animation, only while data moves.** A running backup's or sync's pipe lights up, light data packets flow through it, a light streak sweeps along it every couple of seconds, and a replication's blocks move like a conveyor. Replaces the moving arrowheads.
- **Layout:** boxes keep their natural width and all spare room goes into the gaps (at least 88 px), so pipes are as long as the card allows; with many columns, name and sync rows wrap instead of the diagram overflowing.

## [v2.28.1] — 2026-10-17

### Added

- **"Replikation läuft" on the overview.** Next to "N Backups laufen", a cluster card shows when a PBS sync job is copying into the cluster's datastores right now ("2 Replikationen laufen" for several); the tooltip names source and target. Read from the precomputed diagram, so the overview stays fast; a sync the cluster page has already seen finish is left out.

## [v2.28.0] — 2026-10-17

### Changed

- **Bolder, calmer replication lines.** Lines are much thicker (8 px) and end in a filled arrowhead; lines running the long way around stay clearly thinner (5 px, 3 px for side targets of very few guests). Column gaps grew (56–96 px) so the lines have room.
- **Movement only while data moves.** Lines are still unless a backup or sync runs right now; then the line pulses blue, filled arrows run along it and dashed lines flow. The always-on chevrons are gone.
- On phones, boxes and datastore name rows may wrap so the diagram never gets wider than the screen.

## [v2.27.4] — 2026-10-17

### Fixed

- **A failed backup was still shown as running** ("Backup läuft" without percentage). While a backup runs, the sync stores an in-progress placeholder; a successful backup replaces it, but a failed one left it standing until a cleanup marked it failed after 6 hours.
  - Complete live data from PVE now decides what is running, on page load and in the 30-second refresh; placeholders only count when PVE could not be asked.
  - A failed run in the vzdump task log ("ERROR: Backup of VM 129 failed") marks its placeholder as failed right away, instead of being ignored.

## [v2.27.3] — 2026-10-16

### Changed

- **Datastores inside a box are sorted alphabetically** (naturally: volume1, volume2, volume10) instead of by guest count; the sync lines under a datastore likewise. The order of the boxes still follows the data flow so lines do not cross.

## [v2.27.2] — 2026-10-16

### Fixed

- **Sync progress jumped after loading** (e.g. 47 % → 59 %). The page showed the progress stored with the precomputed diagram at the last background sync — up to an hour old — until the live poll answered. Now the page shows only a live value (at most 2 minutes old from the cache); without one the ring turns without a number and the page asks for the live value right away.

## [v2.27.1] — 2026-10-16

### Fixed

- **A line skipping a column could disappear behind the middle box.** Whether it ran above or below the boxes depended on the middle of the whole diagram, which moves once lane space is reserved; on the second pass the line went "below" without room there. Now the choice depends only on where the target box sits in its column, and lanes are placed just outside all boxes. Such lines also leave and enter slightly off-centre, so they no longer merge with the straight line between neighbours.

## [v2.27.0] — 2026-10-16

### Changed

- **One straight line per pair of boxes.** However many datastores or jobs lie between two boxes, they are joined by exactly one horizontal connection through the height both boxes share; it carries the most telling state of its jobs (running › failed › warning › OK). Lines skipping a column run above the boxes, or below them when the target sits in the lower half, so they cross no other line.
- **Dashed lines move with their arrows** — the dashes travel along the line at the arrows' speed instead of arrows sliding over a still line.
- **Running sync progress as a ring.** A sync writing into a datastore right now shows the same turning blue ring as a running backup, with its percentage (live every 30 s); the line next to it just says "läuft seit 22:15".
- Columns never get narrower than names that must not wrap; a long sync source wraps after "server/" instead of being cut off.

## [v2.26.1] — 2026-10-16

### Changed

- **Running-backup rings turn.** The whole ring now rotates slowly (one turn in 8 s) and pulses more clearly; the gentle fade alone was hardly visible on the dark ring, and not at all at a few percent. Now even a 4 % arc visibly travels around. The percentage stays upright.

## [v2.26.0] — 2026-10-16

### Added

- **Running syncs are recognised** — while a sync job runs, PBS reports only its task UPID, so the diagram showed "nie" and a still line. Now it shows "⟳ 45 % · läuft seit 22:47": the start comes from the UPID, the progress from the task log ("percentage done"), refreshed live every 30 s on the cluster page; when the task ends, the line calms down ("gerade fertig").
- **Data moving right now stands out**: a running sync's line and the PVE line a running backup writes to (the guest's recent backup storage) turn thick, blue and pulsing, with more and faster arrows. Updated live with the backup progress.

### Fixed

- **Running-backup rings did not visibly pulse** and were green instead of blue: the generic ring rule overrode them and drew a static ring under the pulsing one.
- Ring in the diagram could be squeezed into an oval; server and cluster names could wrap at a hyphen. The PVE tag sits in the datastore's name row and "voll ca." is left out when the warning triangle already shows it, so boxes stay low.

## [v2.25.3] — 2026-10-16

### Changed

- **Running-backup rings pulse gently** — the whole ring, done and open part alike, fades in and out every few seconds, so a backup at 3 % shows movement as clearly as one at 90 %. Replaces the stripes on the filled arc, which were invisible at low percentages. The percentage stays steady; a ring without percentage also turns slowly.

## [v2.25.2] — 2026-10-16

### Added

- **Warning triangle for datastores filling up.** In the replication diagram, a datastore the PBS expects to be full within 30 days gets a warning triangle next to its name — yellow, red from 14 days on — with days and date in the tooltip. The days are computed against the current date on every page view, not when the diagram was built.

## [v2.25.1] — 2026-10-16

### Changed

- **VMID in the running-backup tiles** is smaller, regular weight and set apart from the name, so it does not read as part of it.

## [v2.25.0] — 2026-10-16

### Changed

- **Moving arrows in the replication diagram.** Chevrons travel along every line in the direction of the data — fast on the lines from PVE, clearly slower on the dashed replication lines; failed or never-run syncs stand still. Lines are thicker and the gaps between columns wider (adapting to the window width) so the movement reads. Respects "reduce motion".
- **Running backups without a frame or heading.** The tiles stand on their own; the "N Backups laufen" heading is gone (kept for screen readers). Stripes travel calmly along the filled arc of each progress ring so a running backup is visibly moving; a ring without percentage turns slowly (6 s per turn instead of 1.2 s).
- **"langsam, sonst 40 min"** instead of "langsamer als üblich (sonst ~40 min)", on one line.
- Datastore names and the PVE tag in the diagram are never shortened any more; only sync lines may wrap.

## [v2.24.2] — 2026-10-16

### Changed

- **Replication diagram without a box around it.** The frame and background around the diagram are gone; its boxes look like the key-figure tiles (Nodes, VMs, …) and sit directly on the cluster card, in light and dark mode.

## [v2.24.1] — 2026-10-16

### Changed

- **Compact replication diagram.** Datastores are laid out side by side — ring left, name, PVE guests and sync lines right — instead of text stacked under the ring. All boxes share one height (the tallest box), columns are centred vertically and boxes fill their column's width, so the diagram has no empty half and no stretched boxes. A source on the same PBS is named by its datastore only ("← nas-b1"); long names are shortened only when space runs out. Space for lines above or below the boxes is reserved only when such lines exist.

## [v2.24.0] — 2026-10-16

### Changed

- **Cluster page loads fast.** It no longer waits for PVE to list running backups (task lists and logs of every node, often seconds): it shows what is cached and the page fetches live progress right after loading. The overview no longer waits up to 6 s either. The replication diagram is precomputed at the end of every sync (and after PBS settings change) and stored in `cluster_topology`; the page reads one row.
- **Cluster header without the sync text lines.** The sync status is a dot in the diagram — in the cluster box for PVE and in each PBS box's header — with the time in the tooltip and the sync log on click. Name and key-figure tiles share one row; PBS without a box in the diagram keep a dot under the name.
- **Diagram uses the full width.** Columns share the card width, lines skipping or going back run through the middle of the gaps between columns.
- **Running backups as tiles** with a progress ring (spinning without a percentage), data written, expected finish and throughput, instead of a list with bars.
- **Throughput without decimals** ("188 MiB/s"; one decimal only below 10) and with arrows instead of words: ↓ read, ↑ write, ⇅ when both are equal.

## [v2.23.0] — 2026-10-16

### Added

- **Aliases per PBS connection** (Settings → Cluster, next to "Namespace"): other host names or IPs under which PVE or a sync remote knows this PBS, separated by commas. Needed when Backup Sentinel cannot recognise the PBS by itself — e.g. PVE uses `pbs-mar-lej` in a backup network Backup Sentinel cannot reach, while the PBS is registered as `mgm-pbs-mar-lej`. Aliases count for the replication diagram (one box instead of two) and for counting backup copies, right away and without a sync. The namespace form route is now `/clusters/{slug}/pbs/{id}/settings` and saves both fields.

## [v2.22.3] — 2026-10-16

### Fixed

- **A PBS known under two names appeared twice** (e.g. registered as `mgm-pbs-mar-lej`, configured in PVE as `pbs-mar-lej`). Backup Sentinel now recognises a PBS by its certificate:
  - On every sync it asks each registered PBS for the fingerprints of the certificates it serves (`/nodes/localhost/certificates/info`), so a PBS registered without fingerprint is still known by it.
  - A PVE storage or sync remote that names no fingerprint (e.g. ACME certificate) is asked for its certificate over TLS (port 8007, 3 s timeout, once per host and sync).
  - The registered PBS behind each PVE storage and sync remote is resolved at sync time (fingerprint, name, DNS, certificate) and stored, instead of being guessed without DNS on every page view.
  - This also merges such aliases when counting backup copies (×2).

## [v2.22.2] — 2026-10-16

### Changed

- **Clearer replication diagram.**
  - Datastores are placed by the **longest** path from the cluster: a store that is both a (side) backup target and a replica now stands behind its source, so the main flow reads left to right (e.g. PVE → pbs-fh-muc → pull copy on pbs-aboe-muc).
  - Direct PVE targets show how many guests backed up there in the last 7 days ("PVE · 1 VM"); a target used by at most a quarter of the main target's guests (such as the PBS VM backing up to another PBS) is drawn thin.
  - Lines only run between columns: neighbours are joined directly, lines skipping a column use a lane above the boxes, lines back to an earlier column (mutual backup) a lane below; on phones a lane at the side. No line runs behind a box any more.
  - Within a column, datastores follow the order of their sources to avoid crossings.
  - The diagram spans the full width of the cluster card; labels under the rings are one line each (last run, source) and shortened if needed.
  - Datastores of a PBS that takes part in the flow but are not involved themselves are no longer shown.

## [v2.22.1] — 2026-10-16

### Fixed

- **PBS shown twice in the replication diagram.** A PBS registered for several clusters — e.g. once with and once without certificate fingerprint — counted as two servers, so its datastores and replicas appeared twice. Connections sharing a fingerprint, name or API host are now one server, and a sync job read through several connections is one edge.

## [v2.22.0] — 2026-10-16

### Added

- **Backup targets and replication diagram** on the cluster page, right below the key-figure tiles. It shows where the backups go: from the cluster (PVE) to the datastores PVE backs up to directly, and on through the PBS **sync jobs** (pull, push and local) to their copies. Every datastore is a ring with its usage and, when filling up, "voll ca. MM/JJ". Replication lines are dashed and coloured by the last run (OK, warnings, failed, never run); each copy names its source and last run, the tooltip adds schedule, namespaces and next run. The role belongs to the edge, not the server: one PBS can be primary target for one datastore and hold a copy in another, and two PBS backing up each other get arrows both ways. Copies are followed per namespace, so a datastore holding several clusters' copies only shows this cluster's. Unregistered PBS (remote config only) are drawn dashed without usage. Replaces the datastore chips under the PBS sync lines.
- New sync data: the PBS storages of the PVE config (`pve_pbs_targets`) and the sync jobs and remotes of every PBS (`pbs_sync_jobs`, from `/admin/sync` and `/config/remote`). Missing rights for these only add a line to the PBS sync log; the sync stays green.
- **Guest type icons** in the VM table: a monitor for VMs and a box for containers replace the status dot, in the status colour (new column `vms.guest_type`).

## [v2.21.1] — 2026-10-13

### Fixed

- **"Full on" dates in the past.** PBS extrapolates the last month's datastore usage linearly; when usage shrinks (prune/GC), the 100 % crossing lies in the past and PBS still reports it. Backup Sentinel showed it ("voll ca. 16.04.26") and treated it as "full within 14 days": the datastore turned critical and could trigger a "datastore full" notification at e.g. 75 %. A full date that is not in the future now means "not filling up" — no date shown, status from usage alone. Already stored values are hidden right away.

## [v2.21.0] — 2026-10-13

### Added

- **Namespace per PBS connection.** A PBS connection of a cluster can be limited to one namespace (Settings → Cluster, field "Namespace"; or `--namespace NS` in the PBS bootstrap). The direct PBS sync then reads that namespace and its child namespaces instead of only the root. This makes copies from a **pull sync** into a namespace on another PBS count as an additional copy (×2), without registering that PBS as a storage in PVE. Empty keeps the old behaviour (root only), so other clusters' guests with the same VMIDs in other namespaces are never mixed in. A bootstrap re-run without `--namespace` keeps the configured namespace.

## [v2.20.1] — 2026-10-12

### Changed

- **Compact weekly report header.** One row instead of three: brand logo and wordmark on the left, "Wochenbericht" with the week and period below it on the right (both lines left-aligned to each other). On phones the title moves below the logos.

## [v2.20.0] — 2026-10-12

### Added

- **Per-cluster breakdown in the weekly report**: one line per cluster with VMs, nodes and encryption, and chips for what needs attention — critical, warnings, failed backups of the week, due restore tests, a failing sync — or "ok". The figures match the dashboard's cluster cards.
- **Brand logo and wordmark in the weekly report header**, as in the web header. The logo (`BSENTINEL_BRAND_LOGO_LIGHT_URL`) is converted from SVG to PNG and, like the wordmark, embedded in the email (cid:), because many mail clients block remote images and don't render SVG. New dependency: `resvg-py` (prebuilt wheels, no system libraries). `scripts/render_email_wordmark.py` regenerates the wordmark image.

### Changed

- The weekly report's key-figure tiles have equal heights when a label wraps.
- **Weekly report preview** is a button next to "Test-E-Mail senden" instead of a small link.

## [v2.19.0] — 2026-10-12

### Changed

- **Settings in tabs.** The settings page was one long column. It now has four tabs: *Cluster* (cluster overview with PBS connections first, then new cluster, governance and the sync log), *Benachrichtigungen*, *Erscheinungsbild* and *Änderungsprotokoll*. The active tab is the URL hash (`#cluster`, `#benachrichtigungen`, `#appearance`, `#audit`), so the redirects after saving land on the right tab and links can point to one; arrow keys switch tabs. Without JavaScript all sections stay visible as before.

## [v2.18.0] — 2026-10-12

### Changed

- **The weekly report is a designed HTML email.** Instead of plain text it now shows the week's verdict ("Alles im grünen Bereich" / "N Punkte brauchen Aufmerksamkeit"), key figures as tiles (VMs, critical, warnings, failed backups), the problems as sections with coloured status chips (backups, verifies, nearly full datastores with a fill bar and the estimated full date), then as information the week's full runs and VMs with a single backup location, the restore-test status and a button to open Backup Sentinel. VM names link to their cluster when `APP_URL` is set. Header colours follow the installation's theme. The layout uses tables and inline styles so Outlook, Gmail and Apple Mail render it alike, and stacks on phones. A plain-text part stays in the same email for clients without HTML.
- **Full runs no longer count as problems** in the weekly report: they are listed as information (slower, not an error), so "all good" can still appear.

### Added

- **Preview** next to the weekly report switch (Settings → Notifications) shows the report as it would be sent now.

## [v2.17.0] — 2026-10-12

### Changed

- **Single backup location is shown as information.** Since v2.16.0 a single location was no longer a warning, but the status dot was green like a VM with several locations. There is now an *info* level: everything fine but only one backup location gives a blue status dot (tooltip "ok, nur ein Backup-Standort"); green means fine with at least two locations. Warnings and critical states still take precedence. Such VMs stay out of "Only problems".
- **Weekly report** lists VMs with a single backup location again (dropped by mistake in v2.16.0), now as information after the problem sections: they do not count as problems, so "all good" can still appear.

## [v2.16.2] — 2026-10-12

### Fixed

- **Almost every backup of a VM with a TPM showed as a full run** (orange bar). PVE attaches the TPM state as an extra drive for the backup, so its dirty bitmap is "created new" on every run, and that one line marked the whole backup as full although the data disks ran incrementally. Bitmap lines of `tpmstate*` and `efidisk*` are now ignored; a backup is a full run only if a data disk's bitmap was invalid or new. A migration clears the stored flags and the next sync reads the vzdump logs of the last 31 days again (as far as PVE still keeps them).
- Markers of processed vzdump tasks were dropped after 14 days although tasks are read for 31 days, so older logs would have been fetched again. They are now kept for 35 days.

### Added

- **Legend under the VM history charts**, for the bar kinds that occur: backup, dirty bitmap not usable (whole disk read), deleted, failed.

## [v2.16.1] — 2026-10-12

### Changed

- **Dark mode between navy and grey.** Since v2.15.1 the dark surfaces were near-black greys (saturation capped at 14 %); before that, a saturated navy. They now sit in between: the primary's hue with up to 32 % saturation and slightly lighter surfaces, for smile eyes Dark Blue page `#131325` and panel `#1b1b34`.
- **No neon yellow in dark mode.** Active filters, the sync button and primary buttons used the accent `#ffff00` unchanged on dark surfaces. In dark mode the accent is now lighter and less saturated (`#e8e87d` for smile eyes Neon Yellow). The warning amber is calmer as well (`#dfb97f`).

## [v2.16.0] — 2026-10-12

### Changed

- **A single backup target is no longer a warning.** VMs whose backups sit on exactly one PBS showed a yellow status dot and a yellow "PBS ×1" pill and counted as a problem, although many setups have one PBS by design. One verified target now keeps the status dot green; the pill is blue ("fine, just no second copy") and the VM no longer appears under "Only problems" or in the weekly report. Two or more targets stay green.
- **Expanding a VM row is visible.** A ▶ before each VM name shows the row can be opened and turns when it is. The opened details have a **Collapse** button, and a click on free space in the details closes them as well (clicks on chart bars still only show their tooltip).

### Fixed

- **Duration, throughput and reuse only for the last few days.** These values come from the vzdump task logs, which were read for tasks of the last 4 days only, so older bars of the 30-day charts stayed empty. Task logs of the last 31 days are now read; already processed tasks are skipped, so the first sync after the update fetches the older logs once (as far as PVE still keeps them).

## [v2.15.3] — 2026-10-12

### Fixed

- **VM detail charts overflowed** for VMs backed up several times a day (e.g. hourly): every single backup got its own bar, so 30 days became hundreds of bars that ran far past the four chart columns. The charts now show one bar per backup day, like the 30-day sparkline in the table: the day's latest successful backup, coloured ok / deleted / failed. The tooltip names how many backups ran that day.

## [v2.15.2] — 2026-10-11

### Changed

- **Copies tooltip:** one server per line, with a correct singular or plural ("auf 1 Server" / "auf 2 Servern") instead of "Server(n)". Servers are shown by their short host name (`pbs-aboe-muc` instead of `pbs-aboe-muc.int.smileeyes.de`). The stray empty line that appeared when a VM had no copy information is gone.
- "VM(s) mit unverschlüsselten Backups" now uses a correct singular or plural as well.

### Fixed

- **Tooltips were clipped** inside the VM table (e.g. on the first row in windows under 1200 px). Tooltips are now one floating element on the page. It sits above the element, flips below it when there is no room, and stays inside the window horizontally.

## [v2.15.1] — 2026-10-11

### Changed

- **Calmer dark mode.** Dark surfaces (page, panels, header, chips, lines, buttons) were mixes of the primary colour with black, so smile eyes Dark Blue produced a saturated navy page. They are now near-neutral greys that only carry the primary's hue: fixed lightness, saturation capped at 14 %, for example page `#101016` and panel `#191921`. Text greys are desaturated as well. This applies to every preset and custom primary. Status colours, accent and the running-backup blue are unchanged.

## [v2.15.0] — 2026-10-11

### Changed

- **Redesigned VM table on the cluster page.** Striped rows with a hover highlight, and one status dot per VM (green / yellow / red = worst of backup age, second copy and verify; hollow = deleted VM) with the reasons in its tooltip. The VM name is bold and node and VMID are muted. Column order: status, VM, node, VMID, last backup, 30 days, protection, restore test.
- **"Protection" column** replaces the backup cell's four badges and dropdown with one pill: source, server count and verify state (e.g. `PBS ×2 ✓`). It is only outlined when all is well and coloured on warning or error.
- **Restore test column states one fact:** "fällig in N T", "offen", "N T überfällig" or "–" (no test required). Sortable by urgency.
- **Settings moved to the row details.** Backup frequency and restore interval are now set in the details that open when you click a row (reopened automatically after saving). A non-daily frequency still shows as a small `w` / `–` next to the VM name.
- **30-day bars keep their width** (4 px + 2 px gap). When the table gets too wide, the chart shows fewer days instead of shrinking the bars into a barcode, and the header states how many ("22 Tage").

### Fixed

- **The same PBS was counted twice as a second copy** when PVE knew it under its own storage name (`pbs-aboe-muc.nas-b1`) without a matching fingerprint (e.g. an ACME certificate) or by IP. PVE storages are now matched to the registered PBS connection by fingerprint (any notation), host name, short name or resolved IP. Unregistered PBS are grouped by fingerprint and named by host. The copies tooltip lists server names instead of storage names. Takes effect with the next sync.

## [v2.14.0] — 2026-10-11

### Added

- **Weekly report by email**, every Monday from 07:00 local time (caught up later in the week if the server was down): VMs with backup problems, failed or stale verifies, VMs with only one backup location, full runs of the week, nearly full datastores and the restore-test status (expired / open / tested this week) — or a short "nothing to report". Language follows `BSENTINEL_REPORT_LANGUAGE`. New switch in the email settings (on by default; requires email to be enabled). Sent at most once per ISO week.

## [v2.13.0] — 2026-10-11

### Added

- **VM history on click.** Clicking a VM row (anywhere except its controls) opens four 30-day charts below it: size, duration, average throughput and share reused from the previous backup — full runs in orange, failed in red, deleted in sage, each bar with a tooltip. Data comes from the new endpoint `GET /api/clusters/{slug}/vms/{id}/history` (404 for VMs of other clusters). Open details follow their row through filters and close when the table is sorted.

## [v2.12.0] — 2026-10-11

### Added

- **"Nur Probleme" filter** at the end of the node filter bar, with a count. It shows VMs whose backup is critical or a warning, whose restore test has expired, whose verify failed or is stale, that have only one backup location, or whose running backup is slower than usual (live). A never-tested restore ("offen") alone is not counted. Combines with the node filter and the stat tiles.

## [v2.11.2] — 2026-10-11

### Fixed

- **Fresh installations failed to start** since v2.10.0: the schema created `pbs_datastore_usage` before the `pbs_connections` table it references. Existing databases were not affected. The table is now created after `pbs_connections`.

## [v2.11.1] — 2026-10-11

### Fixed

- **Release metadata for v2.10.0 and v2.11.0.** Both tags were pushed without bumping `APP_VERSION` (the app kept reporting v2.9.2) and without changelog entries; v2.10.0 also failed CI on a formatting check. This release carries the correct version and the missing notes below. No code changes beyond that.

## [v2.11.0] — 2026-10-11

### Added

- **Second-copy check (3-2-1).** Every backup records its physical location (`backup_events.locations`): a PBS is identified by its certificate fingerprint (else host) plus datastore — from the PVE storage config and the direct PBS connections — so the same PBS configured by IP and by name counts once. A pill in the backup column shows on how many servers the backups of the last 7 days are stored (**×2**), yellow when it's only one.
- **PBS verify status** in the same pill: **✓** verified, **?** no successful verify for 30 days, **✗** verify failed within 7 days. A failed verify triggers the new **"Verify fehlgeschlagen"** notification (once per snapshot).

## [v2.10.0] — 2026-10-11

### Added

- **PBS datastore capacity with forecast.** Each sync reads `status/datastore-usage` (used/total and the PBS's own estimated full date) into `pbs_datastore_usage`. The cluster page shows every datastore under its PBS (e.g. "nas-b1 · 62 %"), yellow at ≥ 80 % or full within 60 days, red at ≥ 90 % or within 14 days. Red datastores trigger the new **"Datastore fast voll"** notification (once per day and store).

## [v2.9.2] — 2026-10-11

### Security

- **CSRF protection.** State-changing requests (POST/PUT/PATCH/DELETE) whose `Origin` or `Referer` points to another site are rejected with 403 (OWASP origin verification; trusted hosts: `Host`, `X-Forwarded-Host` and `BSENTINEL_APP_BASE_URL`). Previously a crafted page could, for example, delete a cluster in a logged-in admin's browser. Requests without either header — the PVE/PBS bootstrap scripts (curl) — keep working. No form or JS changes needed.

## [v2.9.1] — 2026-10-11

### Fixed

- **Dark mode stays readable with any primary colour.** Dark surfaces are darkened until light text reaches 7:1, and button/topbar text is chosen by contrast — a mid-luminance custom primary (e.g. `#4A7EB5`) no longer yields ~3:1 button text. The default smile eyes theme is unchanged.
- Contrast warnings on the settings page accept only numeric values from the URL; a crafted link can no longer show arbitrary text.
- The hex colour fields no longer truncate pasted values with surrounding spaces.
- `POST /settings/appearance` is a regular (threadpool) route again, so its database calls don't block the event loop.

### Changed

- Theme resolution moved to `app/services/theme_store.py`; the monthly report job no longer imports from the web layer.
- Tests: HTTP round trip of the appearance settings (errors, warnings, forged warnings), the theme-rendering fallback, and dark-mode contrast for several primaries. CI installs `httpx` for FastAPI's test client.

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
