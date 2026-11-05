from __future__ import annotations

import asyncio
import logging
import threading
import time
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from app import db
from app.config import (
    APP_BASE_URL,
    APP_VERSION,
    DEFAULT_TIMEZONE,
    SYNC_INTERVAL_MINUTES,
)
from app.i18n import ts
from app.services import email_assets, topology_store, weekly_report
from app.services.backup_location import CachedLookups, assign_pbs_servers, match_pbs_connection
from app.services.backup_status import assess_slow_backup
from app.services.notifications import (
    channel_enabled,
    notify_backup_critical,
    notify_backup_slow,
    notify_datastore_full,
    notify_restore_overdue,
    notify_size_anomaly,
    notify_sync_failed,
    notify_sync_overdue,
    notify_unencrypted_backups,
    notify_verify_failed,
    send_email,
    send_monthly_report_email,
)
from app.services.pbs import (
    PBSSyncError,
    assess_datastore_usage,
    fetch_all_pbs_backups,
    fetch_datastore_usage,
    fetch_pbs_sync_jobs,
)
from app.services.proxmox import fetch_cluster_inventory, fetch_finished_backup_tasks
from app.services.proxmox_progress import running_backup_progress_cached
from app.services.report_archive import generate_archived_report
from app.services.theme_store import active_theme

logger = logging.getLogger(__name__)

syncing_clusters: set[int] = set()
sync_logs: dict[int, list[str]] = {}
sync_executor = ThreadPoolExecutor(max_workers=4, thread_name_prefix="sync")
# Restore runs can take hours; a dedicated single-slot executor keeps them off
# the sync workers and enforces "one active restore at a time" (spec §15).
restore_executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="restore")
_restore_future: Any = None
RESTORE_QUEUE_TICK_SECONDS = 45  # short-cadence queue self-heal (auto-sync is hourly)


class SyncLogHandler(logging.Handler):
    """Captures log lines into the per-cluster live buffer."""

    def __init__(self, cluster_id: int):
        super().__init__(level=logging.DEBUG)
        self._cluster_id = cluster_id

    def emit(self, record: logging.LogRecord):
        buf = sync_logs.get(self._cluster_id)
        if buf is not None:
            line = self.format(record)
            buf.append(line)


def read_directly(conns: list[dict], lookups: CachedLookups):
    """Whether a PVE storage's backups come straight from its PBS in this
    sync: a registered PBS of the cluster that sees the datastore (as of its
    last usage read) and whose read covers the storage's namespace."""
    seen = {(row["pbs_id"], row["store"]) for row in db.list_datastore_usage()}

    def covered(cfg: dict) -> bool:
        conn = match_pbs_connection(str(cfg.get("server") or ""), conns, lookups.resolve)
        if not conn or (conn["id"], str(cfg.get("datastore") or "")) not in seen:
            return False
        scope, ns = str(conn.get("namespace") or ""), str(cfg.get("namespace") or "")
        return ns == scope or (bool(scope) and ns.startswith(f"{scope}/"))

    return covered


class _StepTimes:
    """Seconds per sync step, for one "Zeiten:" line in the sync log."""

    def __init__(self) -> None:
        self.secs: dict[str, float] = {}

    @contextmanager
    def __call__(self, label: str) -> Iterator[None]:
        t = time.monotonic()
        try:
            yield
        finally:
            self.secs[label] = self.secs.get(label, 0.0) + time.monotonic() - t

    def line(self) -> str:
        steps = sorted(self.secs.items(), key=lambda item: -item[1])
        return ts("synclog.timings", steps=" · ".join(f"{label} {secs:.1f}s" for label, secs in steps))


def _dedup_pbs_events(events: list[dict]) -> list[dict]:
    """Remove PVE-via-PBS duplicates from a combined list of PBS backup events.

    PVE's storage content API and the direct PBS query both return snapshots
    for PBS-type storages.  PVE uses ``ctime`` (job creation time), PBS uses
    ``backup-time`` (actual snapshot start) — these can differ by 30+ minutes
    for VMs that wait in a sequential backup job.  PVE also reports an inflated
    chunk-reference size instead of the real snapshot size.

    PVE-via-PBS events are tagged ``_from_pve=True`` in ``sync_cluster_backups``
    before being added to the combined list.  When a direct-PBS event exists
    within ±1 hour bucket of a PVE-via-PBS event for the same VMID, the
    PVE-via-PBS event is dropped.
    """
    direct: dict[tuple[int, int], dict] = {}
    for event in events:
        if not event.get("_from_pve"):
            hour = int(event["started_at"].timestamp()) // 3600
            direct[(event["vmid"], hour)] = event

    deduped: list[dict] = []
    for event in events:
        if not event.get("_from_pve"):
            deduped.append(event)
            continue
        hour = int(event["started_at"].timestamp()) // 3600
        vmid = event["vmid"]
        if (vmid, hour) in direct or (vmid, hour - 1) in direct or (vmid, hour + 1) in direct:
            continue
        deduped.append(event)
    return deduped


def sync_cluster_backups(cluster: dict) -> None:
    """Fetch backup events from PBS direct connections (preferred) and PVE proxy."""
    cutoff = None
    pve_log: list[str] = []
    step = _StepTimes()
    t_start = time.monotonic()
    logger.info("══ Sync START: %s ══", cluster["name"])

    try:
        t0 = time.monotonic()
        # Reasons not to delete gone VMs this run — any unreachable source
        # could make existing backups look missing.
        prune_blockers: list[str] = []
        known_vmids = db.list_known_vmids(cluster["id"])
        lookups = CachedLookups()
        with step(ts("synclog.step_inventory")):
            inventory = fetch_cluster_inventory(
                cluster,
                known_vmids=known_vmids,
                read_directly=read_directly(db.list_pbs_connections(cluster["id"]), lookups),
            )
        pve_events: list[dict] = []
        all_pbs_events: list[dict] = []
        pbs_all_ok = True
        logger.info(
            "  %s",
            ts(
                "synclog.pve_inventory",
                nodes=len(inventory.nodes),
                vms=len(inventory.vms),
                secs=f"{time.monotonic() - t0:.1f}",
            ),
        )
        pve_log.append(f"Nodes: {len(inventory.nodes)}, VMs: {len(inventory.vms)}")
        with step(ts("synclog.step_database")):
            db.sync_cluster_inventory(cluster["id"], inventory.nodes, inventory.vms)
            if inventory.qdevice is not None:  # unknown: keep the last known state
                db.update_cluster_qdevice(cluster["id"], inventory.qdevice)
            if inventory.ha is not None:
                db.update_vm_ha(cluster["id"], inventory.ha)
        # Best-effort guest IP collection for auto-restore-enabled VMs (spec §9).
        from app.services.restore_orchestrator import collect_cluster_ips

        collect_cluster_ips(cluster, inventory.vms)
        if not inventory.vms:
            prune_blockers.append(ts("synclog.pve_zero_vms"))
        prune_blockers.extend(f"PVE {err}" for err in inventory.fetch_errors)

        if inventory.backup_events:
            for event in inventory.backup_events:
                event["source"] = "pbs" if event.get("storage_type") == "pbs" else "pve"
            pve_events = [event for event in inventory.backup_events if event.get("source") == "pve"]
            for event in inventory.backup_events:
                if event.get("source") == "pbs":
                    event["_from_pve"] = True
                    event["fetch_origin"] = "pve"
                    all_pbs_events.append(event)
            pve_log.append(ts("synclog.pve_events", count=len(inventory.backup_events)))
        else:
            pve_log.append(ts("synclog.pve_events", count=0))

        with step(ts("synclog.step_database")):
            db.sync_backup_events_for_source(cluster["id"], "pve", pve_events)

        pbs_conns = db.list_pbs_connections(cluster["id"])
        # Name PBS storages seen through PVE after the registered PBS behind them,
        # so one server is one copy however it was reached (matched by host name).
        with step(ts("synclog.step_matching")):
            assign_pbs_servers(inventory.backup_events or [], pbs_conns, lookups.resolve)
            if inventory.pbs_targets is not None:
                # Any cluster's registration counts: the PBS may be registered elsewhere.
                all_conns = db.list_pbs_connections()
                for target in inventory.pbs_targets:
                    match = lookups.connection(target["server"], all_conns)
                    target["pbs_id"] = match["id"] if match else None
                db.replace_pve_pbs_targets(cluster["id"], inventory.pbs_targets)
        if not pbs_conns:
            pve_log.append(ts("synclog.no_pbs"))
        else:
            # Include VMs deleted from PVE so their remaining PBS snapshots
            # are still tracked instead of being flagged as removed.
            vmids = {vm["vmid"] for vm in inventory.vms} | known_vmids
            # Namespaces sync jobs copy this cluster's backups into: read them
            # too, so the copies count.
            with step(ts("synclog.step_topology")):
                sync_target_namespaces = topology_store.sync_target_namespaces(cluster["id"])
            for conn in pbs_conns:
                pbs_log: list[str] = []
                pbs_ok = True
                t_pbs = time.monotonic()
                logger.info("  %s", ts("synclog.pbs_start", name=conn.get("name")))
                try:
                    with step(ts("synclog.step_pbs_backups", name=conn.get("name"))):
                        events, ds_logs, pbs_complete = fetch_all_pbs_backups(
                            conn, vmids, cutoff, extra_namespaces=sync_target_namespaces.get(conn["id"])
                        )
                    if not pbs_complete:
                        pbs_ok = False
                    logger.info(
                        "  %s",
                        ts(
                            "synclog.pbs_done",
                            name=conn.get("name"),
                            count=len(events),
                            secs=f"{time.monotonic() - t_pbs:.1f}",
                        ),
                    )
                    pbs_log.extend(ds_logs)
                    for event in events:
                        event["source"] = "pbs"
                        event["fetch_origin"] = "pbs"
                    all_pbs_events.extend(events)
                    with step(ts("synclog.step_pbs_usage", name=conn.get("name"))):
                        record_datastore_usage(cluster, conn, pbs_log)
                    with step(ts("synclog.step_pbs_jobs", name=conn.get("name"))):
                        record_sync_jobs(conn, pbs_log, lookups)
                except PBSSyncError as exc:
                    pbs_ok = False
                    pbs_log.append(ts("synclog.error", error=exc))
                    logger.error("PBS sync failed for cluster %s / PBS %s: %s", cluster["name"], conn.get("name"), exc)
                except Exception as exc:
                    pbs_ok = False
                    pbs_log.append(ts("synclog.unexpected_error", error=exc))
                    logger.error(
                        "PBS unexpected error for cluster %s / PBS %s: %s", cluster["name"], conn.get("name"), exc
                    )
                db.update_pbs_sync_status(conn["id"], pbs_ok, "\n".join(pbs_log))
                if not pbs_ok:
                    pbs_all_ok = False
                    prune_blockers.append(ts("synclog.pbs_failed", name=conn.get("name")))

        with step(ts("synclog.step_database")):
            # PVE's storage content API and the direct PBS fetch both return the
            # same snapshots; timestamps may differ by up to a minute (PVE uses
            # ctime, PBS backup-time) and sizes can differ (PVE sometimes reports
            # the total referenced chunk size instead of the snapshot size).
            # Deduplicate: direct-PBS events (have a "location") win.
            all_pbs_events = _dedup_pbs_events(all_pbs_events)
            db.sync_backup_events_for_source(cluster["id"], "pbs", all_pbs_events, mark_removed=pbs_all_ok)
            db.update_backup_event_storages(cluster["id"], pve_events + all_pbs_events)

        # Mark stub in-progress events older than N hours as aborted so the
        # UI stops showing a "Backup running" badge for crashed/cancelled jobs.
        stale = db.cleanup_stale_inprogress_backups(cluster["id"], max_age_hours=6)
        if stale:
            pve_log.append(ts("synclog.stale_cleaned", count=stale))

        db.update_vm_last_backup_from_events(cluster["id"])

        try:
            with step(ts("synclog.step_task_logs")):
                enrich_backup_events_from_task_logs(cluster, [node["name"] for node in inventory.nodes])
        except Exception as exc:  # best effort, never fails the sync
            logger.warning("Task-log enrichment failed for %s: %s", cluster["name"], exc)
        try:
            with step(ts("synclog.step_slow_check")):
                notify_slow_running_backups(cluster, [node["name"] for node in inventory.nodes], inventory.vms)
        except Exception as exc:
            logger.warning("Slow-backup check failed for %s: %s", cluster["name"], exc)

        if prune_blockers:
            skipped = ts("synclog.prune_skipped", reasons="; ".join(prune_blockers))
            logger.warning("  %s", skipped)
            pve_log.append(skipped)
        else:
            pruned = db.prune_gone_vms_without_backups(cluster["id"])
            if pruned:
                logger.info("  %s", ts("synclog.pruned", count=pruned))
            pve_log.append(ts("synclog.pruned", count=pruned))
        # Shared PBS and sync jobs can change other clusters' diagrams too.
        with step(ts("synclog.step_topology")):
            topology_store.refresh_all()
    except Exception as exc:
        pve_log.append(ts("synclog.error", error=exc))
        logger.error("Sync failed for cluster %s: %s", cluster["name"], exc)
        db.update_cluster_sync_status(cluster["id"], False, "\n".join(pve_log))
        notify_sync_failed(cluster["name"], str(exc))
        raise

    t_checks = time.monotonic()
    try:
        anomalies = db.detect_backup_size_anomalies(cluster["id"])
        if anomalies:
            logger.info("  %s", ts("synclog.size_anomalies", count=len(anomalies)))
            pve_log.append(ts("synclog.size_anomalies", count=len(anomalies)))
            for anomaly in anomalies:
                notify_size_anomaly(
                    cluster["name"],
                    anomaly["vm_name"],
                    anomaly["vmid"],
                    anomaly["latest_size"],
                    anomaly["avg_size"],
                    anomaly["deviation"],
                )
    except Exception as exc:
        logger.warning("Anomaly detection failed for %s: %s", cluster["name"], exc)

    try:
        notify_guest_alerts(cluster, db.vm_governance_rows(cluster["id"]))
    except Exception as exc:
        logger.warning("Critical notification check failed: %s", exc)

    try:
        notify_failed_verifies(cluster)
    except Exception as exc:
        logger.warning("Verify notification check failed: %s", exc)

    try:
        unenc_vms = db.unencrypted_vms(cluster["id"])
        if unenc_vms:
            logger.info("  %s", ts("synclog.unencrypted", count=len(unenc_vms)))
            pve_log.append(ts("synclog.unencrypted", count=len(unenc_vms)))
            notify_unencrypted_backups(cluster["name"], len(unenc_vms), [v["vm_name"] for v in unenc_vms])
    except Exception as exc:
        logger.warning("Encryption audit notification failed: %s", exc)

    step.secs[ts("synclog.step_checks")] = time.monotonic() - t_checks
    elapsed_total = time.monotonic() - t_start
    logger.info("══ Sync DONE: %s (%.1fs gesamt) ══", cluster["name"], elapsed_total)
    pve_log.append(ts("synclog.duration", secs=f"{elapsed_total:.1f}"))
    pve_log.append(step.line())
    logger.info("  %s", step.line())
    db.update_cluster_sync_status(cluster["id"], True, "\n".join(pve_log))


def notify_guest_alerts(cluster: dict, rows: list[dict]) -> None:
    """Critical backup age and expired restore tests, per guest."""
    for row in rows:
        if row["backup_severity"] == "critical" and row["status_backup_kind"] != "none":
            notify_backup_critical(cluster["name"], row["vm_name"], row["vmid"], row["backup_age_days"])
        # Expired restore test (not: never tested, see restore_due_status).
        if row["restore_due_status"] == "critical" and not row["vm_gone"]:
            notify_restore_overdue(
                cluster["name"], row["vm_name"], row["vmid"], row["restore_age_days"] - row["restore_interval_days"]
            )


def notify_failed_verifies(cluster: dict) -> None:
    for row in db.vm_governance_rows(cluster["id"]):
        if row["verify_status"] == "critical" and row["verify_failed_at"]:
            notify_verify_failed(cluster["name"], row["vm_name"], row["vmid"], row["verify_failed_at"])


def record_datastore_usage(cluster: dict, conn: dict, pbs_log: list[str]) -> None:
    """Store datastore capacity and alert on nearly full stores (best effort)."""
    try:
        usage = fetch_datastore_usage(conn)
    except Exception as exc:
        pbs_log.append(ts("synclog.datastore_usage_failed", error=exc))
        logger.warning("Datastore usage failed for PBS %s: %s", conn.get("name"), exc)
        return
    db.replace_datastore_usage(conn["id"], usage)
    for row in usage:
        status = assess_datastore_usage(row["used_bytes"], row["total_bytes"], row["estimated_full_at"])
        pct = round(row["used_bytes"] / row["total_bytes"] * 100) if row["total_bytes"] else 0
        pbs_log.append(ts("synclog.datastore_usage", store=row["store"], pct=pct))
        if status == "critical":
            notify_datastore_full(cluster["name"], conn.get("name", ""), row["store"], pct, row["estimated_full_at"])


def record_sync_jobs(
    conn: dict, pbs_log: list[str], lookups: CachedLookups | None = None, jobs: list[dict] | None = None
) -> None:
    """Store this PBS's sync jobs for the replication view (best effort:
    missing rights must not fail the PBS sync; old rows stay). `jobs`, when
    already fetched, are stored as they are."""
    try:
        jobs = jobs if jobs is not None else fetch_pbs_sync_jobs(conn)
    except Exception as exc:
        pbs_log.append(ts("synclog.sync_jobs_failed", error=exc))
        logger.warning("Sync jobs failed for PBS %s: %s", conn.get("name"), exc)
        return
    all_conns = db.list_pbs_connections()
    lookups = lookups or CachedLookups()
    for job in jobs:
        if job["remote_name"]:
            match = lookups.connection(job["remote_host"] or job["remote_name"], all_conns)
            job["remote_pbs_id"] = match["id"] if match else None
    db.replace_pbs_sync_jobs(conn["id"], jobs)
    pbs_log.append(ts("synclog.sync_jobs", count=len(jobs)))


# A replication starting or ending between two syncs: the PBS feeding a
# cluster's diagram are asked about their sync jobs at most this often.
JOB_CHECK_SECONDS = 30.0
_jobs_checked: dict[int, float] = {}
_jobs_lock = threading.Lock()


def refresh_sync_job_states(cluster_id: int) -> bool:
    """Notice sync jobs that started or ended since the last sync, for the
    cluster's diagram: when one did, store the jobs and rebuild the diagrams.
    True when something changed (best effort: an unreachable PBS keeps what
    is shown)."""
    topology = topology_store.load(cluster_id)
    pbs_ids = {e["pbs_id"] for e in topology["edges"] if e.get("kind") == "sync" and e.get("pbs_id")}
    if not pbs_ids:
        return False
    now = time.monotonic()
    with _jobs_lock:
        due = {p for p in pbs_ids if now - _jobs_checked.get(p, -JOB_CHECK_SECONDS) >= JOB_CHECK_SECONDS}
        _jobs_checked.update(dict.fromkeys(due, now))
    if not due:
        return False
    conns = {c["id"]: c for c in db.list_pbs_connections()}
    running: dict[int, dict[str, str | None]] = {}
    for row in db.list_pbs_sync_jobs():
        running.setdefault(row["pbs_id"], {})[row["job_id"]] = row.get("running_upid")
    changed = False
    for pbs_id in sorted(due):
        conn = conns.get(pbs_id)
        if not conn:
            continue
        try:
            jobs = fetch_pbs_sync_jobs(conn, retries=0)
        except Exception as exc:
            logger.warning("Sync job check failed for PBS %s: %s", conn.get("name"), exc)
            continue
        if {j["job_id"]: j["running_upid"] for j in jobs} != running.get(pbs_id, {}):
            record_sync_jobs(conn, [], jobs=jobs)
            changed = True
    if changed:
        topology_store.refresh_all()
    return changed


def refresh_sync_job_states_soon(cluster_ids: list[int]) -> None:
    """The same check in the background, so a page never waits for a PBS."""

    def run() -> None:
        for cluster_id in cluster_ids:
            try:
                refresh_sync_job_states(cluster_id)
            except Exception:
                logger.exception("Sync job check failed for cluster %s", cluster_id)

    sync_executor.submit(run)


def enrich_backup_events_from_task_logs(cluster: dict, nodes: list[str]) -> None:
    """Add duration, throughput, dedup ratio and full/incremental from vzdump logs."""
    tasks = fetch_finished_backup_tasks(
        cluster, nodes, db.processed_backup_task_upids(cluster["id"]), ZoneInfo(DEFAULT_TIMEZONE)
    )
    done: list[str] = []
    now = datetime.now(UTC)
    for task in tasks:
        unmatched = db.update_backup_event_details(cluster["id"], task.runs)
        # Retry next sync if a backup isn't listed yet — but not forever.
        if unmatched == 0 or (now - task.ended_at).total_seconds() > 86400:
            done.append(task.upid)
    db.mark_backup_tasks_processed(cluster["id"], done)
    if tasks:
        logger.info("  %s", ts("synclog.task_logs", count=len(tasks), done=len(done)))


def notify_slow_running_backups(cluster: dict, nodes: list[str], vms: list[dict]) -> None:
    # Shared with the cluster pages' polls: no extra PVE fetch while they run.
    progress, _complete = running_backup_progress_cached(cluster, nodes)
    if not progress:
        return
    typical = db.typical_backup_durations(cluster["id"])
    names = {vm["vmid"]: vm["name"] for vm in vms}
    for vmid, item in progress.items():
        slow = assess_slow_backup(item, typical.get(vmid))
        if slow:
            notify_backup_slow(cluster["name"], names.get(vmid, str(vmid)), vmid, slow[0], slow[1], item["started_at"])


def sync_all_clusters() -> None:
    for cluster in db.list_clusters():
        if not cluster["api_user"] or not cluster["token_id"] or not cluster["token_secret"]:
            continue
        if cluster["id"] in syncing_clusters:
            logger.info("%s", ts("synclog.already_running", cluster=cluster["name"]))
            continue
        syncing_clusters.add(cluster["id"])
        sync_logs[cluster["id"]] = []
        handler = SyncLogHandler(cluster["id"])
        handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
        root = logging.getLogger()
        root.addHandler(handler)
        try:
            sync_cluster_backups(cluster)
        except Exception as exc:
            logger.error("Auto-sync failed for cluster %s: %s", cluster["name"], exc)
        finally:
            root.removeHandler(handler)
            syncing_clusters.discard(cluster["id"])


def auto_generate_monthly_report() -> None:
    now = datetime.now(tz=ZoneInfo(DEFAULT_TIMEZONE))
    first_of_current = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    last_month = (first_of_current - timedelta(days=1)).replace(day=1).date()
    existing = db.get_monthly_report_for_month(last_month)
    if existing:
        return
    logger.info("Auto report: generating the report for %s", last_month)
    try:
        pdf_path = generate_archived_report(
            last_month,
            ts("report.monthly_period", month=last_month.strftime("%m/%Y")),
            first_of_current.astimezone(UTC),
            "auto",
            now,
        )
        logger.info("Auto report: PDF written: %s", pdf_path)
        send_monthly_report_email(pdf_path, last_month.strftime("%m/%Y"))
    except Exception as exc:
        logger.error("Auto report failed: %s", exc)


def notify_overdue_cluster_syncs() -> None:
    overdue_clusters = db.list_clusters_with_sync_overdue(24)
    for cluster in overdue_clusters:
        notify_sync_overdue(cluster["name"], cluster["last_pve_sync_failure_started_at"])
        db.mark_cluster_sync_overdue_notified(cluster["id"])
        logger.warning(
            "24h-Sync-Ausfall gemeldet: %s (seit %s)", cluster["name"], cluster["last_pve_sync_failure_started_at"]
        )


def weekly_report_email(*, inline_images: bool = True) -> tuple[str, str, str, dict[str, bytes]]:
    """(subject, text, HTML, inline PNGs by content ID) of the weekly report as of now.

    The preview passes inline_images=False: the HTML then carries the images as data: URIs.
    """
    now = datetime.now(UTC)
    datastores = []
    for row in db.list_datastore_usage():
        total = row["total_bytes"]
        row["pct"] = round(row["used_bytes"] / total * 100) if total else 0
        row["status"] = assess_datastore_usage(row["used_bytes"], total, row["estimated_full_at"])
        datastores.append(row)
    stats = db.weekly_backup_stats(now - timedelta(days=7))
    vm_rows = db.vm_governance_rows()
    report = weekly_report.collect(vm_rows, datastores, stats, now, db.cluster_summaries(vm_rows=vm_rows))
    pngs = {"wordmark": email_assets.wordmark_png()}
    if logo := email_assets.brand_logo_png():
        pngs["logo"] = logo
    images = {cid: f"cid:{cid}" if inline_images else email_assets.data_uri(png) for cid, png in pngs.items()}
    _preset, colors = active_theme()
    html = weekly_report.render_html(
        report, primary=colors.primary, accent=colors.accent, app_url=APP_BASE_URL, version=APP_VERSION, images=images
    )
    return weekly_report.subject(report), weekly_report.text(report), html, pngs


def send_weekly_report_if_due() -> None:
    """Monday 07:00 summary email (see app.services.weekly_report)."""
    settings = db.get_settings_dict()
    if settings.get("notify_weekly_report", "true") != "true":
        return
    tz = ZoneInfo(DEFAULT_TIMEZONE)
    week = weekly_report.due_week(datetime.now(tz), settings.get("weekly_report_last_week", ""))
    if not week:
        return
    if not channel_enabled("email"):
        # Mark the week handled — no point rebuilding it every sync until Sunday.
        db.save_settings({"weekly_report_last_week": week})
        return
    subject, body, html, pngs = weekly_report_email()
    if send_email(subject, body, html=html, inline_images=pngs):  # failure → retried on the next loop
        db.save_settings({"weekly_report_last_week": week})
        logger.info("Weekly report sent for %s", week)


async def auto_sync_loop() -> None:
    interval = SYNC_INTERVAL_MINUTES * 60
    logger.info("Auto-sync started, interval: %d minutes", SYNC_INTERVAL_MINUTES)
    await asyncio.sleep(5)
    while True:
        logger.info("Auto-sync: syncing all clusters")
        await asyncio.get_event_loop().run_in_executor(sync_executor, sync_all_clusters)
        try:
            await asyncio.get_event_loop().run_in_executor(sync_executor, notify_overdue_cluster_syncs)
        except Exception as exc:
            logger.error("Sync-overdue check failed: %s", exc)
        try:
            await asyncio.get_event_loop().run_in_executor(sync_executor, auto_generate_monthly_report)
        except Exception as exc:
            logger.error("Auto-Report check failed: %s", exc)
        try:
            await asyncio.get_event_loop().run_in_executor(sync_executor, send_weekly_report_if_due)
        except Exception as exc:
            logger.error("Weekly report failed: %s", exc)
        try:
            await asyncio.get_event_loop().run_in_executor(sync_executor, _recover_stuck_restores)
        except Exception as exc:
            logger.error("Restore self-heal failed: %s", exc)
        _kick_restore_queue()
        await asyncio.sleep(interval)


def _recover_stuck_restores() -> None:
    """Self-heal a wedged restore queue — crucially, OFF the single-slot restore
    executor. ``recover_stuck_runs`` also runs inside ``run_restore_queue``, but
    that is submitted to the one restore slot, so when the slot is occupied by a
    stuck or long resume/worker (e.g. a post-deploy resume probing an environment
    that is already gone) the one thing that can unblock the queue is itself stuck
    behind the block. Running it here, on a sync worker each tick, finalizes the
    phantom 'running' row and cancels the doomed thread so the slot frees; then a
    kick lets the freed queue drain."""
    from app import db_restore
    from app.services import restore_config, restore_orchestrator

    # Cheap DB gate first: no 'running' row → nothing to heal, don't touch the host.
    if not db_restore.list_runs(limit=1, status="running"):
        return
    config = restore_config.load()
    if config is None:
        return
    config = restore_orchestrator.ensure_host_key_pinned(config)
    server = restore_orchestrator.build_server(config)
    recovered = restore_orchestrator.recover_stuck_runs(server, config)
    if recovered:
        logger.warning("Restore self-heal: recovered %d wedged run(s) off the restore slot", recovered)
        _kick_restore_queue()


def _reap_orphans() -> None:
    """Destroy guests on the restore host that no live run owns — on a short
    cadence, INDEPENDENT of a new run starting.

    The in-run reaper (``execute`` → ``server.reap``) only fires when the next run
    begins, so a helper/VM left behind by a crashed or wedged run lingers until
    then — and if the queue is idle or stalled, that never comes. A lingering
    helper is not just untidy: it reuses a management IP+MAC and can make the whole
    management subnet flaky. Run only while NOTHING is executing (an active run owns
    its environment and reaps itself), so this never races a live restore. Host
    identity is verified first — never reap a host that resolves to production."""
    from app import db_restore
    from app.services import restore_config, restore_orchestrator
    from app.services.restore_worker import owned_vmids

    if db_restore.has_active_run():
        return  # a restore is executing; its own execute() handles reaping
    config = restore_config.load()
    if config is None:
        return
    try:
        config = restore_orchestrator.ensure_host_key_pinned(config)
        server = restore_orchestrator.build_server(config)
        server.verify_host_identity(restore_orchestrator.production_endpoints())
        destroyed = server.reap(owned_vmids())
    except Exception as exc:  # host unreachable / identity clash / API hiccup — try again next tick
        logger.debug("orphan reap skipped: %s", exc)
        return
    if destroyed:
        logger.warning("Restore orphan sweep: destroyed unowned guest(s) %s", destroyed)


def _kick_restore_queue() -> None:
    """Fire the restore queue on its dedicated single-slot executor without
    awaiting it (a restore may run for hours). Skip ONLY while a restore is
    genuinely executing — a run in status 'running' (``execute`` sets it before any
    long work and holds it until finalize). A future that is merely "not done" with
    nothing 'running' is a wedged/finished-but-idle slot and must NOT block the
    queue, or queued runs sit idle until the next hourly sync. With max_workers=1 a
    submit behind a truly-stuck thread just waits harmlessly."""
    global _restore_future
    from app import db_restore

    if _restore_future is not None and not _restore_future.done() and db_restore.has_active_run():
        return
    from app.services.restore_orchestrator import run_restore_queue

    _restore_future = restore_executor.submit(run_restore_queue)


def _should_drain_now(has_active: bool, paused: bool, has_queued: bool) -> bool:
    """Self-heal decision: nudge the queue when work waits, nothing is executing,
    and the operator hasn't paused it. (Pause is honoured; a running restore is left
    alone.)"""
    return has_queued and not has_active and not paused


def _restore_queue_watchdog() -> None:
    """Keep the single-slot queue moving on a short cadence — the hourly auto-sync
    is too coarse. Finalize wedged 'running' phantoms, then, the real fix: kick a
    drain whenever runs are queued but nothing is running (and not paused), so the
    queue can't stall with work waiting. Self-healing, no button."""
    from app import db_restore
    from app.services import restore_config

    _recover_stuck_restores()  # phantom 'running' with nothing on the host
    _reap_orphans()  # sweep helpers/VMs no live run owns, even with an idle queue
    try:
        if _should_drain_now(db_restore.has_active_run(), restore_config.queue_paused(), db_restore.has_queued_runs()):
            logger.info("restore queue self-heal: runs queued but nothing running — kicking drain")
            _kick_restore_queue()
    except Exception as exc:
        logger.debug("restore queue watchdog check failed: %s", exc)


async def restore_queue_loop() -> None:
    """Short-cadence companion to auto_sync_loop, just for the restore queue."""
    await asyncio.sleep(10)
    while True:
        try:
            await asyncio.get_event_loop().run_in_executor(sync_executor, _restore_queue_watchdog)
        except Exception as exc:
            logger.error("restore queue watchdog failed: %s", exc)
        await asyncio.sleep(RESTORE_QUEUE_TICK_SECONDS)


def reconcile_and_resume_restores() -> None:
    """On startup, re-attach to any restore that was running when the app
    stopped (a deploy must not interrupt it). Runs that cannot be resumed
    (no restore config) are marked error by reconcile_on_startup; the rest are
    resumed on the single-slot restore executor so the queue stays serialized."""
    global _restore_future
    from app import db_restore
    from app.services import restore_config, restore_orchestrator, restore_worker

    try:
        restore_worker.reconcile_on_startup()
        config = restore_config.load()
        if config is None:
            return
        running = db_restore.list_runs(limit=50, status="running")
        if not running:
            return
        config = restore_orchestrator.ensure_host_key_pinned(config)
        server = restore_orchestrator.build_server(config)

        def _resume_all() -> None:
            for run in running:
                try:
                    plan = restore_orchestrator.plan_from_run(run, config)
                    restore_worker.resume_run(run["id"], plan, server, run.get("task_upid"))
                except Exception as exc:
                    logger.error("resume of restore run %s failed: %s", run["id"], exc)

        _restore_future = restore_executor.submit(_resume_all)
    except Exception as exc:
        logger.error("restore reconcile/resume on startup failed: %s", exc)


def background_sync(cluster: dict) -> None:
    cid = cluster["id"]
    sync_logs[cid] = []
    handler = SyncLogHandler(cid)
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    root = logging.getLogger()
    root.addHandler(handler)
    try:
        sync_cluster_backups(cluster)
    except Exception as exc:
        logger.error("Background sync failed for %s: %s", cluster["name"], exc)
    finally:
        root.removeHandler(handler)
        syncing_clusters.discard(cluster["id"])
