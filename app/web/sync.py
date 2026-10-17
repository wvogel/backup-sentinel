from __future__ import annotations

import asyncio
import json
import logging
import threading
import time
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

from app import db
from app.config import (
    APP_BASE_URL,
    APP_VERSION,
    DEFAULT_TIMEZONE,
    REPORT_DIR,
    REPORT_LANGUAGE,
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
    fetch_pbs_cert_fingerprints,
    fetch_pbs_sync_jobs,
)
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


def read_directly(conns: list[dict], lookups: CachedLookups):
    """Whether a PVE storage's backups come straight from its PBS in this
    sync: a registered PBS of the cluster that sees the datastore (as of its
    last usage read) and whose read covers the storage's namespace."""
    seen = {(row["pbs_id"], row["store"]) for row in db.list_datastore_usage()}

    def covered(cfg: dict) -> bool:
        conn = match_pbs_connection(
            str(cfg.get("fingerprint") or ""), str(cfg.get("server") or ""), conns, lookups.resolve
        )
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

        with step(ts("synclog.step_database")):
            db.sync_backup_events_for_source(cluster["id"], "pve", pve_events)

        for conn in db.list_pbs_connections(cluster["id"]):
            with step(ts("synclog.step_pbs_certs", name=conn.get("name"))):
                record_cert_fingerprints(conn)
        pbs_conns = db.list_pbs_connections(cluster["id"])
        # Name PBS storages seen through PVE after the registered PBS behind them,
        # so one server is one copy however it was reached.
        with step(ts("synclog.step_matching")):
            assign_pbs_servers(inventory.backup_events or [], pbs_conns, lookups.resolve, lookups.probe)
            if inventory.pbs_targets is not None:
                # Any cluster's registration counts: the PBS may be registered elsewhere.
                all_conns = db.list_pbs_connections()
                for target in inventory.pbs_targets:
                    match = lookups.connection(target["fingerprint"], target["server"], all_conns)
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
            # A PBS that could not be read keeps its snapshots: PVE no longer
            # lists them as a fallback (read_directly).
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


def record_cert_fingerprints(conn: dict) -> None:
    """Remember the certificates this PBS serves (best effort)."""
    try:
        fingerprints = fetch_pbs_cert_fingerprints(conn)
    except Exception as exc:
        logger.warning("Certificate info failed for PBS %s: %s", conn.get("name"), exc)
        return
    if fingerprints != list(conn.get("cert_fingerprints") or []):
        db.update_pbs_cert_fingerprints(conn["id"], fingerprints)


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
            match = lookups.connection(job["remote_fingerprint"], job["remote_host"], all_conns)
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
    report = weekly_report.collect(db.vm_governance_rows(), datastores, stats, now, db.cluster_summaries())
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
