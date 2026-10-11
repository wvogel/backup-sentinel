from __future__ import annotations

import asyncio
import json
import logging
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from zoneinfo import ZoneInfo

from app import db
from app.config import DEFAULT_TIMEZONE, REPORT_DIR, REPORT_LANGUAGE, SYNC_INTERVAL_MINUTES
from app.i18n import ts
from app.services.backup_status import assess_slow_backup
from app.services.notifications import (
    notify_backup_critical,
    notify_backup_slow,
    notify_size_anomaly,
    notify_sync_failed,
    notify_sync_overdue,
    notify_unencrypted_backups,
    send_monthly_report_email,
)
from app.services.pbs import PBSSyncError, fetch_all_pbs_backups
from app.services.pdf_reports import write_backup_period_pdf
from app.services.proxmox import fetch_cluster_inventory, fetch_finished_backup_tasks
from app.services.proxmox_progress import fetch_running_backup_progress
from app.services.reporting import build_backup_period_report, serialize_report_payload
from app.services.theme_store import active_theme

logger = logging.getLogger(__name__)

syncing_clusters: set[int] = set()
sync_logs: dict[int, list[str]] = {}
sync_executor = ThreadPoolExecutor(max_workers=4, thread_name_prefix="sync")


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


def sync_cluster_backups(cluster: dict) -> None:
    """Fetch backup events from PBS direct connections (preferred) and PVE proxy."""
    cutoff = None
    pve_log: list[str] = []
    t_start = time.monotonic()
    logger.info("══ Sync START: %s ══", cluster["name"])

    try:
        t0 = time.monotonic()
        # Reasons not to delete gone VMs this run — any unreachable source
        # could make existing backups look missing.
        prune_blockers: list[str] = []
        known_vmids = db.list_known_vmids(cluster["id"])
        inventory = fetch_cluster_inventory(cluster, known_vmids=known_vmids)
        pve_events: list[dict] = []
        all_pbs_events: list[dict] = []
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
        db.sync_cluster_inventory(cluster["id"], inventory.nodes, inventory.vms)
        if not inventory.vms:
            prune_blockers.append(ts("synclog.pve_zero_vms"))
        prune_blockers.extend(f"PVE {err}" for err in inventory.fetch_errors)

        if inventory.backup_events:
            for event in inventory.backup_events:
                event["source"] = "pbs" if event.get("storage_type") == "pbs" else "pve"
            pve_events = [event for event in inventory.backup_events if event.get("source") == "pve"]
            all_pbs_events.extend(event for event in inventory.backup_events if event.get("source") == "pbs")
            pve_log.append(ts("synclog.pve_events", count=len(inventory.backup_events)))
        else:
            pve_log.append(ts("synclog.pve_events", count=0))

        db.sync_backup_events_for_source(cluster["id"], "pve", pve_events)

        pbs_conns = db.list_pbs_connections(cluster["id"])
        if not pbs_conns:
            pve_log.append(ts("synclog.no_pbs"))
        else:
            # Include VMs deleted from PVE so their remaining PBS snapshots
            # are still tracked instead of being flagged as removed.
            vmids = {vm["vmid"] for vm in inventory.vms} | known_vmids
            for conn in pbs_conns:
                pbs_log: list[str] = []
                pbs_ok = True
                t_pbs = time.monotonic()
                logger.info("  %s", ts("synclog.pbs_start", name=conn.get("name")))
                try:
                    events, ds_logs, pbs_complete = fetch_all_pbs_backups(conn, vmids, cutoff)
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
                    all_pbs_events.extend(events)
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
                    prune_blockers.append(ts("synclog.pbs_failed", name=conn.get("name")))

        db.sync_backup_events_for_source(cluster["id"], "pbs", all_pbs_events)
        db.update_backup_event_storages(cluster["id"], pve_events + all_pbs_events)

        # Mark stub in-progress events older than N hours as aborted so the
        # UI stops showing a "Backup running" badge for crashed/cancelled jobs.
        stale = db.cleanup_stale_inprogress_backups(cluster["id"], max_age_hours=6)
        if stale:
            pve_log.append(ts("synclog.stale_cleaned", count=stale))

        db.update_vm_last_backup_from_events(cluster["id"])

        try:
            enrich_backup_events_from_task_logs(cluster, [node["name"] for node in inventory.nodes])
        except Exception as exc:  # best effort, never fails the sync
            logger.warning("Task-log enrichment failed for %s: %s", cluster["name"], exc)
        try:
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
    except Exception as exc:
        pve_log.append(ts("synclog.error", error=exc))
        logger.error("Sync failed for cluster %s: %s", cluster["name"], exc)
        db.update_cluster_sync_status(cluster["id"], False, "\n".join(pve_log))
        notify_sync_failed(cluster["name"], str(exc))
        raise

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
        governance = db.vm_governance_rows(cluster["id"])
        for row in governance:
            if row["backup_severity"] == "critical" and row["status_backup_kind"] != "none":
                notify_backup_critical(
                    cluster["name"],
                    row["vm_name"],
                    row["vmid"],
                    row["backup_age_days"],
                )
    except Exception as exc:
        logger.warning("Critical notification check failed: %s", exc)

    try:
        unenc_vms = db.unencrypted_vms(cluster["id"])
        if unenc_vms:
            logger.info("  %s", ts("synclog.unencrypted", count=len(unenc_vms)))
            pve_log.append(ts("synclog.unencrypted", count=len(unenc_vms)))
            notify_unencrypted_backups(cluster["name"], len(unenc_vms), [v["vm_name"] for v in unenc_vms])
    except Exception as exc:
        logger.warning("Encryption audit notification failed: %s", exc)

    elapsed_total = time.monotonic() - t_start
    logger.info("══ Sync DONE: %s (%.1fs gesamt) ══", cluster["name"], elapsed_total)
    pve_log.append(ts("synclog.duration", secs=f"{elapsed_total:.1f}"))
    db.update_cluster_sync_status(cluster["id"], True, "\n".join(pve_log))


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
    progress = fetch_running_backup_progress(cluster, nodes)
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
    from datetime import timedelta

    from app.services.reporting import build_month_comparison

    now = datetime.now(tz=ZoneInfo(DEFAULT_TIMEZONE))
    first_of_current = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    last_month = (first_of_current - timedelta(days=1)).replace(day=1).date()
    existing = db.get_monthly_report_for_month(last_month)
    if existing:
        return
    logger.info("Auto-Report: Generiere Report für %s", last_month)
    try:
        events = db.list_all_report_backup_events()
        report = build_backup_period_report(datetime.min.replace(tzinfo=UTC), now, events)
        report["period_label"] = ts("report.monthly_period", month=last_month.strftime("%m/%Y"))
        try:
            cur_month_events = db.backup_events_for_month(last_month.year, last_month.month)
            prev_month = (last_month.replace(day=1) - timedelta(days=1)).replace(day=1)
            prev_month_events = db.backup_events_for_month(prev_month.year, prev_month.month)
            report["comparison"] = build_month_comparison(cur_month_events, prev_month_events, now)
        except Exception as exc:
            logger.warning("Auto-Report: Monatsvergleich fehlgeschlagen: %s", exc)
        snapshot = serialize_report_payload(report)
        month_slug = last_month.strftime("%Y-%m")
        timestamp_slug = report["generated_at"].strftime("%Y%m%d-%H%M%S")
        pdf_path = REPORT_DIR / f"backup-report-{month_slug}-{timestamp_slug}.pdf"
        json_path = REPORT_DIR / f"backup-report-{month_slug}-{timestamp_slug}.json"
        try:
            from app.db_notifications import list_notification_logs_for_period

            period_start = datetime(last_month.year, last_month.month, 1, tzinfo=UTC)
            period_end = first_of_current.astimezone(UTC)
            notifs = list_notification_logs_for_period(period_start, period_end)
        except Exception:
            notifs = []
        write_backup_period_pdf(
            report, pdf_path, notifications=notifs, lang=REPORT_LANGUAGE, theme_colors=active_theme()[1]
        )
        json_path.write_text(json.dumps(snapshot, ensure_ascii=False, indent=2), encoding="utf-8")
        db.archive_monthly_report(last_month, "auto", pdf_path, json_path, report)
        logger.info("Auto-Report: PDF erstellt: %s", pdf_path)
        send_monthly_report_email(pdf_path, last_month.strftime("%m/%Y"))
    except Exception as exc:
        logger.error("Auto-Report fehlgeschlagen: %s", exc)


def notify_overdue_cluster_syncs() -> None:
    overdue_clusters = db.list_clusters_with_sync_overdue(24)
    for cluster in overdue_clusters:
        notify_sync_overdue(cluster["name"], cluster["last_pve_sync_failure_started_at"])
        db.mark_cluster_sync_overdue_notified(cluster["id"])
        logger.warning(
            "24h-Sync-Ausfall gemeldet: %s (seit %s)", cluster["name"], cluster["last_pve_sync_failure_started_at"]
        )


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
        await asyncio.sleep(interval)


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
