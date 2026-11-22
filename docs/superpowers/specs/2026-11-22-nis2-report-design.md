# Design: NIS2 evidence report (replaces the backup report)

**Date:** 2026-11-22
**Status:** Direction approved (mockup "passt"); pending document sign-off
**Author:** brainstormed with Wolf Vogel
**Mockup:** two A4 pages (overall + one cluster), approved; source kept by the
author, not in the repo.

## Intent and why

The current PDF report (`app/services/pdf_reports.py`) lists every backup event
per VM. It runs to dozens of pages, answers nobody's question and, in the
operator's words, "ist für die Füße". What matters under NIS2 (Art. 21 (2) c —
backup management and disaster recovery; in Germany BSIG § 30 (1) Nr. 3) is
evidence of two things:

1. **Gap-free backups.** Every guest was continuously protected within its RPO.
2. **Logged restore tests.** Backups are proven restorable, and every test,
   including failed ones, is on record.

**Readers:** the operator (IT lead) and an auditor. There is no auditor yet. The
operator wants to show this exact report to the managing director (GF). It must
look like a professional tool, not a home-grown one: an executive summary first,
with evidence behind it.

## Goals

- A new report: **overall** plus **one section per cluster**, in executive form,
  in the approved visual language (A4, navy band, two ring KPIs, status verdict
  in plain words, trend, cluster table, sign-off).
- Two headline KPIs, defined precisely (see *Metrics*) and identical everywhere
  they appear: **Backup-Lückenlosigkeit** and **Restore-Nachweis**.
- An auditor-grade appendix: the full restore-test protocol for the period, the
  per-VM backup calendar, and a page defining the method.
- Same lifecycle as today: generated **automatically each month** for the
  previous month (the existing scheduler and e-mail), and **manually at any time**
  as an interim report (from the 1st of the current month until now).
- Downloads: the overall PDF and **a PDF per cluster**, plus the JSON data.

## Non-goals

- A list of every single backup. It is gone, for good.
- Changing how restore tests run, how RPO/cadence are configured, or the
  dashboard and cluster pages.
- Retroactively regenerating old archived reports. Their files stay
  downloadable as they are.

## Anti-goals (failure even if it "works")

- **Rounding up.** Percentages are whole numbers, **truncated**: 99.97 % prints
  "99 %". "100 %" only when nothing was missed. Colours are judged on the printed
  number, so a "98" is never green against a 99 target.
- Claims the data does not back. For example, "all gaps closed" only if every gap
  actually has a following backup.
- A report that grows with the number of backups. Its length depends on clusters,
  VMs and tests, never on backup count.
- Looking like an export: no default tables with grey borders, no wrapped
  numbers, no content running into the footer.

## Approaches considered

| | Approach | Trade-off |
|---|---|---|
| **A (chosen)** | HTML/CSS template rendered to PDF with **WeasyPrint** | Same Jinja/CSS stack as the UI; real typography, SVG charts. The same template doubles as a browser preview. Cost: system libs (Pango/HarfBuzz) in the image, about 30–40 MB. |
| B | Redesign in ReportLab (status quo library) | No new deps, but high-end layout and typography are laborious and the result tends to look home-grown. |
| C | Headless Chromium | Same quality as A, but +~300 MB image for one PDF a month. |

The operator chose **A**.

## Metrics (the contract)

All metrics are computed for a **period** `[S, E)` (calendar month, or the 1st of
the month until now for an interim report) and scoped to a **cluster** or to
**all clusters**.

### In-scope guests

A guest counts for **backup metrics** in the period when its status backup kind
(as computed in `db_governance`) is not `none`. Guests with **no backup job at
all** (`unconfigured`) **are in scope** and measured as daily. They are exactly
the gap an auditor looks for. *(Builder's call.)*

Time window per guest:
- **Start:** `max(S, first_seen_at)`. A new column `vms.first_seen_at` is set
  when the sync first inserts a guest. Existing rows are backfilled with their
  earliest backup event, or else with the migration time. So a guest that was
  never backed up, or whose first backup came late, counts as **uncovered** from
  when it was first seen. It does not get a free pass. *(Builder's call.)*
- **End:** `missing_since` if it is set and before `E`. A guest whose
  `missing_since` is before `S` is out of scope for that period. The current
  `vm_gone` flag is not used for past periods. *(Builder's call.)*

A guest counts for **restore metrics** when it is in scope for backups and
`restore_required` is true (same rule as the governance view:
`restore_required and backup kind != none`).

### Successful backup

A `backup_events` row with `status = 'ok'` and `size_bytes > 1`. Pruned backups
(`removed_at` set) **still count**: the backup existed at that time. The backup
point is `started_at`.

### Backup-Lückenlosigkeit (gap-free share)

- **Effective RPO** of a guest: `rpo_hours` (default 24). For guests whose
  backup kind is `weekly`, `max(rpo_hours, 180)` (7.5 days), consistent with the
  existing weekly thresholds.
- **Tolerance (operator's decision): 12 hours.** The schedule drift of a daily
  job, where one day starts at 21:00 and the next at 21:04, is not a gap. A
  **gap** exists only when the time between two successful backups exceeds
  **RPO + 12 h**. When it does, the whole overrun counts as uncovered, measured
  from the RPO, not from RPO + 12 h. "Hours over RPO" therefore stays honest.
  Example: 24 h RPO, backups 46 h apart → a gap of 22 h. With 30 h apart, there
  is no gap.
- A guest is **covered** at time *t* unless *t* lies in such a gap. That is,
  unless its latest successful backup is older than the RPO *and* the stretch
  without a backup it belongs to exceeds RPO + 12 h. A stretch still open at `E`
  is judged by its age at `E`.
- The tolerance is a constant next to the thresholds. The method page states it.
- **Lückenlosigkeit** = covered guest-hours ÷ in-scope guest-hours over the
  period, as a whole percent, truncated.
- A **gap** is a maximal interval in which a guest is not covered. It is
  reported with:
  - the last backup before it,
  - the next backup after it, or "offen" if there is none before `E`,
  - the time without a backup,
  - the RPO,
  - the hours over RPO.

  The first backup *before* `S` counts, so a gap that started in the previous
  month is reported from `S`.
- **Per day** (cluster calendar strip): that day's covered guest-hours share,
  truncated. Green = 100, amber = 97–99, red = below 97.
- **Per guest per day** (appendix calendar): green if covered all day, red if any
  gap touched that day, blank before the guest existed.

### Restore-Nachweis (restore evidence)

- **Valid test** at time *t*: the latest `restore_tests` row (result `passed` or
  `limited`, manual or automated) with `tested_at` within the guest's interval
  (`cadence_days(restore_cadence)`, weekly-kind guests at least 365) before *t*.
  This is the same rule as the governance due-status.
- **Restore-Nachweis** = guests with a valid test at `E` ÷ restore-scope guests,
  as a whole percent, truncated.
- **Overdue** = tested before, but no longer valid. **Never tested** = no
  `restore_tests` row.

### Test protocol

Every restore attempt *finished* in the period. That means all `restore_runs`
with `finished_at` in `[S, E)` and any result (passed, limited, failed, error,
cancelled), plus manual `restore_tests` rows that have no run. Per entry:

- date and time, guest and cluster, trigger,
- source snapshot (`source_pbs`/`source_datastore`/`snapshot_id`),
- result, total duration,
- checks passed (the `green_check_labels` set, e.g. "ICMP · TCP 443"),
- for failed or error runs, the reason (`checks["error"]` or the last log lines).

Headline numbers: tests passed / tests run, and average duration of passed runs.

### Targets and verdict

| KPI | Green (Erfüllt) | Amber (Hinweise) | Red (Handlungsbedarf) |
|---|---|---|---|
| Lückenlosigkeit | ≥ 99 % | 98 % | < 98 % |
| Restore-Nachweis | ≥ 90 % | 80–89 % | < 80 % |

The verdict of a scope is the worse of the two KPIs. An open gap at `E` (a guest
currently outside its RPO) forces at least amber. Thresholds are constants in
one place. Making them settings is left for later (see *Left to the builder*).

### 12-month trend

The trend shows both KPIs for each of the last 12 calendar months, computed with
the same functions from the stored history. Restore-Nachweis history uses
today's restore policies. The method page says so.

A month **before Backup Sentinel has data**, meaning the month ends before the
earliest `backup_events.started_at` of the scope, is drawn as "keine Daten" (no
point, a gap in the line). It is never drawn as 0 %. The first month with
partial data is computed from the first event onwards. *(Builder's call.)*

### Evidence that outlives deleted guests

`prune_gone_vms_without_backups` deletes guest rows, and with them, through
cascades, their tests, runs and events. The **archived report is the frozen
evidence**: every generated PDF and JSON stays unchanged, even when the live data
behind it is gone later. No soft delete in this change. The method page states
that live recomputation of past months only covers guests that still exist.
*(Builder's call; a later change could keep restore history of deleted guests.)*

## Report structure

1. **Overall page.**
   - Band with brand logo, wordmark, "NIS2 · Datensicherung", period.
   - Eyebrow "Gesamtübersicht · n Cluster · n VMs", title, one-line subtitle.
   - Verdict box: colour dot, verdict, two generated sentences of plain-text
     summary.
   - Two ring KPIs, each with target, name and a one-sentence definition.
   - Four small figures:
     - successful/failed backups,
     - RPO gaps (total hours, longest),
     - tests passed/run with average duration,
     - guests without a valid test (overdue · never).
   - Two 12-month trend charts with a dashed target line.
   - Cluster table: VMs, Lückenlosigkeit with bar, gaps, Restore-Nachweis with
     bar, open tests, status pill.
   - Sign-off lines for the IT lead and the managing director.
2. **One page per cluster.**
   - Same band, eyebrow (VMs · nodes · PBS targets), cluster name, verdict, the
     two rings with sub-facts.
   - Daily strip for the period.
   - Gap table (only gaps).
   - Test-status donut (valid / overdue / never) next to the next due tests.
   - The period's restore tests, up to what fits. The rest is referenced as
     "+ n weitere, siehe Anhang".
3. **Appendix A: restore-test protocol.** All attempts, grouped by cluster,
   flowing over as many pages as needed with a repeated table header.
4. **Appendix B: backup calendar.** One row per in-scope guest, grouped by
   cluster, one cell per day.
5. **Appendix C: method.** Definitions above in plain language, thresholds, data
   sources, the limitations (history uses current policies; pruned backups count),
   generation time and app version.

Footer on every page: app version · generated at · auto/manual, legal basis,
"Seite x von y".

The **per-cluster PDF** has the same structure, limited to that cluster:
- its cluster page as the first page, with the sign-off block added,
- appendices A and B filtered to that cluster,
- appendix C.

## Visual language (from the approved mockup)

- A4 portrait. Top band navy (theme primary) with a 2.2 mm accent rule (theme
  running colour). Brand logo (`BRAND_LOGO_LIGHT_URL`) plus the chrome-gradient
  wordmark as SVG text (WeasyPrint cannot do `background-clip: text`).
- Colours come from the active UI theme (`active_theme()`, as `palette_for` does
  today). Surfaces: Sand / Paper / Sage. Status colours: green, amber, red.
- Type: the brand font when configured (`BSENTINEL_BRAND_FONT_URL`, a URL pattern
  with `{weight}` → Regular/Medium/SemiBold/Bold, for example
  `https://static.example.com/BrandSans-{weight}.woff2`). Otherwise the system
  sans-serif. The production `.env` gets the Brand Sans pattern.
- Charts are inline SVG generated server-side: rings, area/line trends with a
  dashed target, donut, day strip, heat grid. No JS, no external chart library.
- Numbers use tabular figures. Nothing wraps inside a table cell. Nothing touches
  the footer. Per-guest tables break across pages with repeated headers.
- Language: `REPORT_LANGUAGE` (de/en), labels through the i18n files.

## Lifecycle, storage and UI

- **Monthly:** `auto_generate_monthly_report` (unchanged trigger) builds the
  previous month and e-mails the **overall** PDF as today.
- **Manual:** the existing button on `/reports` builds an interim report for the
  current month to date (`trigger_mode = 'manual'`).
- **Archive:** one `monthly_reports` row per generation. A new `files` JSONB maps
  `{"all": pdf, "<cluster-slug>": pdf, "json": path}`. Old rows keep working
  through `pdf_path`/`json_path`. The archive list on `/reports` shows "Gesamt"
  plus one link per cluster. The JSON holds the computed metrics, gaps and
  protocol, not raw events.
- **`/reports` page:** the hero shows the two KPIs for the current month to date
  instead of backup counts. The cluster rows show the same two KPIs and the
  verdict. There is a "Vorschau" link to `/reports/preview` (the report HTML in
  the browser, current month to date, optional `?cluster=`). The old per-event
  drill-down on the page goes.
- **Removed:** `pdf_reports.py`'s event tables and the backup-count comparison.
  ReportLab stays only if something else still uses it; otherwise it is dropped
  from `requirements.txt`.
- **Docker:** install WeasyPrint's system libs (Pango, HarfBuzz, fonts-dejavu
  as fallback) in the runtime image.

## Constraints

- Generation runs in the background executor as today. It must finish within
  seconds to a few minutes for about 120 guests and 12 months of history. Compute
  per month in SQL plus Python, not per guest-hour.
- No network calls during generation except the brand logo and font, both
  optional with a fallback.
- Docs (README, user/admin docs DE+EN, GitLab/GitHub descriptions) are updated
  with the change, as is the changelog.

## Left to the builder

- Module layout. Suggested:
  - `app/services/nis2_metrics.py`: pure functions (coverage, gaps, restore
    validity, verdict) with thorough unit tests,
  - `app/services/nis2_report.py`: data assembly and rendering,
  - `templates/report_pdf/*.html` and the matching CSS.
- The exact verdict sentences, as long as they state only computed facts.
- How many protocol rows fit on a cluster page, and the overflow behaviour.
- Log-excerpt length for failed runs. Builder's default: the reason line plus at
  most 8 log lines.
- Whether thresholds become settings. Not now: keep them as constants.
- The migration of the archive table (additive `files` column).
