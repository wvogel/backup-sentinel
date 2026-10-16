# Backup Sentinel -- User Documentation

![Screenshot](screenshot-overview.png)

## Table of Contents

1. [Dashboard](#dashboard)
2. [Cluster Detail Page](#cluster-detail-page)
3. [Reports](#reports)
4. [Settings](#settings)
5. [Theme & Language](#theme--language)

---

## Dashboard

The dashboard (`/`) provides a high-level overview of all monitored Proxmox clusters.

### Health Bar

At the top of the page, a colored health bar shows every cluster as a segment:

| Color  | Meaning                                      |
|--------|----------------------------------------------|
| Green  | All backups OK, no warnings                  |
| Yellow | At least one warning (e.g. stale sync)       |
| Red    | Critical issues (failed backups, sync errors) |
| Grey   | Never synced / unknown state                 |

A **stale** badge appears when syncs have been failing for an extended period. Click any segment to jump to the cluster detail page.

### KPI Cards

Below the health bar, four summary cards are displayed:

- **Restore Coverage** -- A ring chart showing the percentage of VMs with a documented restore test (e.g. 85 %). The color changes from green (>= 80 %) to yellow (>= 50 %) to red (< 50 %).
- **Restore Overdue** -- Number of VMs whose restore test interval has expired.
- **Restore Critical** -- Number of VMs in critical restore-test state.
- **Tested This Month** -- Number of restore tests recorded in the current calendar month.

### Cluster Cards

Each cluster is displayed as a card showing:

- Cluster name with an overall status badge (OK / Warning / Critical).
- Last successful PVE sync timestamp.
- Error-since timestamp if the most recent sync failed.
- Node count, VM count, and a breakdown of backup statuses.
- PBS connection status (if configured).
- Recent restore test results as compact icons.

Click a cluster card to navigate to the cluster detail page.

---

## Cluster Detail Page

The cluster detail page (`/clusters/<slug>`) provides a deep dive into a single Proxmox cluster.

### Sync Status

At the top, the hero section shows:

- **Sync button** -- Trigger an immediate re-sync of the cluster.
- **PVE sync status** -- A colored dot (green = success, red = failure, grey = never synced) with the last sync timestamp. Click the dot to view the full sync log.
- **PBS sync status** -- One row per connected PBS, showing the same dot + timestamp pattern.

### Stat Tiles

Interactive stat tiles show node count, VM count, critical/warning counts, encryption percentage, and restore-due count. Click a tile to filter the VM table below to matching rows only.

### Unencrypted Backups Alert

If any VMs have unencrypted backups, a collapsible warning section lists the affected VMs with their last unencrypted backup date.

### VM Table

The main table lists every VM in the cluster with sortable columns:

| Column         | Description                                                |
|----------------|------------------------------------------------------------|
| 🖥 / 📦         | Guest icon: monitor = VM, box = container. The colour is the status: green = all fine and at least 2 backup locations, blue = fine but a single backup location (information, not a problem), yellow = warning, red = critical (worst of backup age and verify); greyed out = VM deleted. The tooltip lists the reasons |
| VM             | Guest name; a small **w** or **–** marks weekly backups or "no backup expected" |
| Node           | The Proxmox node the VM runs on                           |
| VMID           | Proxmox VM ID                                             |
| Last Backup    | Timestamp of the most recent backup; running backups show their progress in the row |
| 30 Days        | One bar per day (green = OK, orange = since deleted, red = failed, grey = no backup). In a narrow window the chart shows fewer days instead of thinner bars; the header then states how many |
| Protection     | Source (PBS/local), number of servers holding backups of the last 7 days (**×2**) and verify state (**✓**/**?**/**✗**). Outlined only when all is well; coloured on warning/error |
| Restore Test   | "due in N d", **open** (never tested), "N d overdue" or "–" (no test required). The tooltip shows the last test |

Rows are striped. **Clicking a row** (marked by the ▶ before the VM name) opens its details below: the VM's settings and the history of the last 30 days with one bar per day (size, duration, throughput, reused data). Close them with **Collapse**, a click on free space in the details, or another click on the row.

**Node filter bar** -- Buttons above the table let you filter VMs by node.

**Sorting** -- Click any column header to sort ascending/descending.

**Backup frequency** -- In a VM's details (click the row) you can override the auto-detected frequency (daily / weekly / none / auto).

**Restore-test due dates** -- The Restore Test column warns ahead of the test interval expiring. The lead time scales with the interval: **yearly** intervals warn **1 month** early, **quarterly** (or shorter) ones **2 weeks** early. Once the interval has elapsed the entry turns red ("N d overdue"). The tooltip shows the last test and the interval.

**Restore interval** -- In a VM's details a picker sets how often a VM must be tested: **90 d**, **180 d** (default), **365 d** or **none**. Weekly backups use at least 365 days. A VM never tested so far is **open**: it counts as due, but not as critically overdue. The *Document restore test* form accepts several VMs at once (Ctrl/⌘), e.g. for a full restore drill.

**Second copy and verify** -- The pill in the Protection column shows on how many servers the backups of the last 7 days are stored (e.g. **×2**) and the PBS verify state: **✓** verified, **?** not verified for over 30 days, **✗** verify failed. Green = at least 2 servers and verified; blue = a single server and verified (fine, just no second copy); yellow = not verified; red = verify failed (also sent as a notification). A PBS counts as one server even if PVE knows it under another storage name, by IP or without a fingerprint (e.g. with an ACME certificate): Backup Sentinel matches the PVE storage to the registered PBS connection (fingerprint, host name, short name or resolved IP) and lists server names in the tooltip.

**Backup targets and replication** -- On the right of the cluster header a diagram shows where the backups go: from the cluster (PVE) to the datastores PVE backs up to directly (per the PBS storages in the PVE config), and from there through the PBS sync jobs on to their copies (pull, push and local syncs; dashed line, blue = last run OK, yellow = with warnings, red = failed, grey = never run). Arrows travel along the lines in the direction of the data: fast on the lines from PVE, slower on replication lines; failed or never-run syncs do not move. A PBS can be the primary target for one datastore and hold a copy in another; two PBS backing up each other get arrows both ways. Copies are followed per namespace, so a datastore holding several clusters' copies shows only this cluster's. The sync status is a dot in the cluster box (PVE) and in the header of each PBS box (green = OK, red = failed, grey = never); the tooltip shows the time, a click opens the sync log. The diagram is precomputed on every sync so the page loads fast. Each copy shows its source and last run, the tooltip adds schedule, namespaces and next run. PBS not registered in Backup Sentinel are drawn dashed with their name from the remote config and without usage.

**PBS storage** -- Every datastore in the diagram is a ring with its usage and, if it is filling up, the PBS's estimate ("full ~04/27"). Yellow from 80 % or full within 60 days, red from 90 % or full within 14 days (also sent as a notification). A datastore expected to be full within 30 days gets a warning triangle next to its name (yellow, red from ≤ 14 days; tooltip: days and date). An estimate in the past (shrinking usage) is ignored.

**Running backups** -- Above the table each running backup gets a tile with a progress ring, data written, expected finish and throughput (↓ read, ↑ write, ⇅ when both are equal); stripes travel slowly along the filled arc; without a percentage the ring turns slowly. A backup taking clearly longer than usual shows "slow, usually 40 min". Clicking a tile jumps to the VM. In the table the row is filled up to the current percentage and marked blue on the left; its tooltip shows start, ETA and data volume. The display refreshes every 30 seconds without reloading the page; the page itself never waits for PVE but fetches the progress right after loading. On the overview, each cluster card shows how many backups are running (as of the last sync).

**Deleted VMs** -- VMs that no longer exist in the Proxmox cluster are shown greyed out with a **deleted** badge (tooltip: since when) as long as backups of them remain. They raise no backup or restore warnings. Once no backups are left, the VM is removed from the database on the next sync, including its restore-test history -- but only if every PVE node, storage and PBS datastore was reachable during that sync.

**Mobile view** -- On narrow screens (phones) the table is automatically turned into cards: one card per VM with labelled fields, so nothing is hidden behind horizontal scrolling.

### Restore Test Form

Below the VM table, a form allows documenting a new restore test:

1. Select a VM from the dropdown.
2. Choose the test date.
3. Select the recovery type: **Full** (complete VM restore), **Partial** (partial data), or **File** (individual file recovery).
4. Select the result: **Passed**, **Limited** (partially successful), or **Failed**.
5. Enter the duration in minutes.
6. Add optional notes.
7. Submit.

The restore test is recorded in the database and immediately reflected in the VM table, KPI cards, and monthly reports.

### Restore Test History

A table below the form shows the most recent restore tests for this cluster, including the VM name, date, type, result, duration, and who performed the test.

---

## Reports

The reports page (`/reports`) provides monthly backup compliance reports.

### Current Report Summary

A hero card at the top shows aggregated stats for the current reporting period:

- **Total backups** -- Number of backup events.
- **OK** -- Successful backups.
- **Errors** -- Failed backups.
- **Deleted** -- Backups that were deleted from storage.
- **PDF Snapshot** button -- Generate a PDF report for the current month on demand.

### Cluster Breakdown

Below the summary, each cluster is shown as a collapsible card. Expand a cluster to see per-VM backup details:

- Each VM is a nested collapsible section.
- VMs with failures or deletions are expanded by default.
- The inner table shows individual backup events with date, size, encryption status, verification status, and result.

### Report Archive

At the bottom, a list of previously generated reports (PDF and JSON) is available for download. Reports are stored in the `/reports` volume and persist across container restarts.

Monthly reports are generated automatically at the end of each month. You can also trigger a snapshot at any time using the PDF Snapshot button.

---

## Settings

The settings page (`/settings`) is the central place for managing clusters, PBS connections, and notifications. It has four tabs: **Clusters** (clusters, PBS connections, new cluster, governance), **Notifications**, **Appearance** and **Change log**. The active tab is part of the address (e.g. `/settings#benachrichtigungen`), so links open that tab directly; the arrow keys switch between tabs.

### Add a New Cluster

1. Enter a **Cluster name** (e.g. `pve-prod`).
2. Enter the **PVE API URL** (e.g. `https://pve.example.internal:8006`).
3. Click **Prepare Cluster**.
4. A bootstrap script is displayed. Run this script on any node of the Proxmox cluster to create an API token and register the cluster automatically.
5. The page polls for completion -- once the script finishes, the cluster appears in the list.

### Manage Clusters

Each registered cluster is shown as a card with:

- **Sync button** -- Trigger an immediate sync.
- **Cluster name link** -- Navigate to the cluster detail page.
- **Rename button** -- Change the display name.
- **Delete button** -- Remove the cluster and all associated data.
- **PVE URL** -- The configured API endpoint.
- **Last sync** -- Timestamp of the last successful sync.

### PBS Connections

Each cluster card has an "Add PBS" section to connect a Proxmox Backup Server:

1. Enter the **PBS name** (display label).
2. Enter the **PBS API URL**.
3. A bootstrap script is shown to run on the PBS host.
4. Once connected, the PBS appears under the cluster with its own sync status.

PBS connections can be removed individually.

### Notification Settings

A dedicated panel configures notification channels:

**Gotify (push notifications):**
- Gotify server URL.
- API token (stored encrypted).
- Enable/disable toggle.
- Test button to verify connectivity.

**Email (SMTP):**
- SMTP host, port, TLS mode (SSL/STARTTLS/none).
- Username and password (stored encrypted).
- Sender address and recipient list.
- Enable/disable toggle.
- Test button to send a test email.

**Quiet Hours:**
- Enable/disable quiet hours.
- Start and end time (e.g. 22:00 -- 07:00).
- During quiet hours, notifications are suppressed.

### Governance Panel

A sidebar panel summarizes the NIST CSF 2.0 alignment:

- **Identify** -- Asset inventory via PVE/PBS sync.
- **Protect** -- Encryption monitoring, backup policy enforcement.
- **Detect** -- Automated alerting on failures and anomalies.
- **Recover** -- Documented restore tests with compliance tracking.

**Weekly report** -- With email enabled and *weekly report* switched on (default), a designed HTML email (with a text version for clients without HTML) arrives every Monday from 7:00: logo and wordmark as in the web interface, the week's verdict ("All good" or "N items need attention"), key figures and one line per cluster with VMs, nodes, encryption and what needs attention (critical, warnings, failed backups, due restore tests, sync failures), then the problems -- backups, verifies, nearly full datastores (with fill level) -- and, as information, the week's full runs and VMs with a single backup location, finally the restore-test status. VM names link to their cluster (requires `APP_URL`). The **Weekly report preview** button shows the report as it would be sent now. If the server was down on Monday, the report is sent later that week.

### Settings Audit Log

Changes to settings are logged with a timestamp and the user who made the change (derived from the OAuth2 proxy headers).

---

## Theme & Language

### Theme Switching

Backup Sentinel supports three theme modes, accessible from the header:

- **Light** -- Light background with dark text.
- **Dark** -- Dark background with light text.
- **Auto** -- Follows the operating system preference (`prefers-color-scheme`).

The selection is stored in a browser cookie and persists across sessions.

### Language Switching

The application is fully bilingual (English and German). Switch the language from the header menu. The selection is stored in the `bsentinel-lang` cookie.

All UI labels, notifications, and generated reports respect the selected language.
