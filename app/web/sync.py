from __future__ import annotations

import asyncio
import json
import logging
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime

from app import db
from app.config import REPORT_DIR, SYNC_INTERVAL_MINUTES
from app.services.notifications import (
    notify_backup_critical,
    notify_size_anomaly,
    notify_sync_failed,
    notify_sync_overdue,
    notify_unencrypted_backups,
    send_monthly_report_email,
)
from app.services.pbs import PBSSyncError, fetch_all_pbs_backups
from app.services.pdf_reports import write_backup_period_pdf
from app.services.proxmox import fetch_cluster_inventory
from app.services.reporting import build_backup_period_report, serialize_report_payload

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
        inventory = fetch_cluster_inventory(cluster)
        pve_events: list[dict] = []
        all_pbs_events: list[dict] = []
        logger.info("  PVE-Inventory: %d Nodes, %d VMs (%.1fs)",
                    len(inventory.nodes), len(inventory.vms), time.monotonic() - t0)
        pve_log.append(f"Nodes: {len(inventory.nodes)}, VMs: {len(inventory.vms)}")
        db.sync_cluster_inventory(cluster["id"], inventory.nodes, inventory.vms)

        if inventory.backup_events:
            for event in inventory.backup_events:
                event["source"] = "pbs" if event.get("storage_type") == "pbs" else "pve"
            pve_events = [event for event in inventory.backup_events if event.get("source") == "pve"]
            all_pbs_events.extend(event for event in inventory.backup_events if event.get("source") == "pbs")
            pve_log.append(f"PVE-direkte Events: {len(inventory.backup_events)}")
        else:
            pve_log.append("PVE-direkte Events: 0")

        db.sync_backup_events_for_source(cluster["id"], "pve", pve_events)

        pbs_conns = db.list_pbs_connections(cluster["id"])
        if not pbs_conns:
            pve_log.append("Kein PBS konfiguriert.")
        else:
            vmids = {vm["vmid"] for vm in inventory.vms}
            for conn in pbs_conns:
                pbs_log: list[str] = []
                pbs_ok = True
                t_pbs = time.monotonic()
                logger.info("  PBS-Sync: %s …", conn.get("name"))
                try:
                    events, ds_logs = fetch_all_pbs_backups(conn, vmids, cutoff)
                    logger.info("  PBS-Sync: %s → %d Events (%.1fs)",
                                conn.get("name"), len(events), time.monotonic() - t_pbs)
                    pbs_log.extend(ds_logs)
                    for event in events:
                        event["source"] = "pbs"
                    all_pbs_events.extend(events)
                except PBSSyncError as exc:
                    pbs_ok = False
                    pbs_log.append(f"Fehler: {exc}")
                    logger.error("PBS sync failed for cluster %s / PBS %s: %s",
                                 cluster["name"], conn.get("name"), exc)
                except Exception as exc:
                    pbs_ok = False
                    pbs_log.append(f"Unerwarteter Fehler: {exc}")
                    logger.error("PBS unexpected error for cluster %s / PBS %s: %s",
                                 cluster["name"], conn.get("name"), exc)
                db.update_pbs_sync_status(conn["id"], pbs_ok, "\n".join(pbs_log))

        db.sync_backup_events_for_source(cluster["id"], "pbs", all_pbs_events)

        db.update_vm_last_backup_from_events(cluster["id"])
    except Exception as exc:
        pve_log.append(f"Fehler: {exc}")
        logger.error("Sync failed for cluster %s: %s", cluster["name"], exc)
        db.update_cluster_sync_status(cluster["id"], False, "\n".join(pve_log))
        notify_sync_failed(cluster["name"], str(exc))
        raise

    try:
        anomalies = db.detect_backup_size_anomalies(cluster["id"])
        if anomalies:
            logger.info("  Größen-Anomalien erkannt: %d VMs", len(anomalies))
            pve_log.append(f"Größen-Anomalien: {len(anomalies)} VMs")
            for anomaly in anomalies:
                notify_size_anomaly(
                    cluster["name"], anomaly["vm_name"], anomaly["vmid"],
                    anomaly["latest_size"], anomaly["avg_size"], anomaly["deviation"],
                )
    except Exception as exc:
        logger.warning("Anomaly detection failed for %s: %s", cluster["name"], exc)

    try:
        governance = db.vm_governance_rows(cluster["id"])
        for row in governance:
            if row["backup_severity"] == "critical" and row["status_backup_kind"] != "none":
                notify_backup_critical(
                    cluster["name"], row["vm_name"], row["vmid"], row["backup_age_days"],
                )
    except Exception as exc:
        logger.warning("Critical notification check failed: %s", exc)

    try:
        unenc_vms = db.unencrypted_vms(cluster["id"])
        if unenc_vms:
            logger.info("  Unverschlüsselte Backups: %d VMs", len(unenc_vms))
            pve_log.append(f"Unverschlüsselte Backups: {len(unenc_vms)} VMs")
            notify_unencrypted_backups(cluster["name"], len(unenc_vms), [v["vm_name"] for v in unenc_vms])
    except Exception as exc:
        logger.warning("Encryption audit notification failed: %s", exc)

    elapsed_total = time.monotonic() - t_start
    logger.info("══ Sync DONE: %s (%.1fs gesamt) ══", cluster["name"], elapsed_total)
    pve_log.append(f"Dauer: {elapsed_total:.1f}s")
    db.update_cluster_sync_status(cluster["id"], True, "\n".join(pve_log))


def sync_all_clusters() -> None:
    for cluster in db.list_clusters():
        if not cluster["api_user"] or not cluster["token_id"] or not cluster["token_secret"]:
            continue
        if cluster["id"] in syncing_clusters:
            logger.info("Auto-sync: %s wird bereits synchronisiert, überspringe", cluster["name"])
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

    now = datetime.now(UTC)
    if now.day != 1:
        return
    last_month = (now.replace(day=1) - timedelta(days=1)).replace(day=1).date()
    existing = db.get_monthly_report_for_month(last_month)
    if existing:
        logger.info("Auto-Report: Report für %s existiert bereits, überspringe", last_month)
        return
    logger.info("Auto-Report: Generiere Report für %s", last_month)
    try:
        events = db.list_all_report_backup_events()
        report = build_backup_period_report(datetime.min.replace(tzinfo=UTC), now, events)
        report["period_label"] = f"Monatsreport {last_month.strftime('%m/%Y')}"
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
        write_backup_period_pdf(report, pdf_path)
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
        logger.warning("24h-Sync-Ausfall gemeldet: %s (seit %s)",
                       cluster["name"], cluster["last_pve_sync_failure_started_at"])


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
