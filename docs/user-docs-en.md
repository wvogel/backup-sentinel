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
| Node           | The Proxmox node the VM runs on                           |
| VM             | Guest name                                                 |
| VMID           | Proxmox VM ID                                             |
| Last Backup    | Timestamp of the most recent backup; shows a progress indicator if a backup is currently running |
| 30 Days        | Sparkline showing daily backup status over the last 30 days (green = OK, red = failed, grey = no backup) |
| Backup         | Backup kind (snapshot/stop/suspend), severity badge, source (Local/PBS), and policy override dropdown |
| Restore Test   | Last restore test result (passed/limited/failed), recovery type icon, date, and duration |

**Node filter bar** -- Buttons above the table let you filter VMs by node.

**Sorting** -- Click any column header to sort ascending/descending.

**Backup policy override** -- Use the dropdown in the Backup column to override the auto-detected policy for a VM (daily / weekly / none / auto).

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

The settings page (`/settings`) is the central place for managing clusters, PBS connections, and notifications.

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
