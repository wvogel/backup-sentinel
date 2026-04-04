from __future__ import annotations

from collections import Counter
from datetime import UTC, date, datetime, timedelta
from typing import Any

from app.services.backup_status import assess_backup_status

REPORT_REMOVED_RETENTION_DAYS = 31


def build_monthly_report(
    cluster_rows: list[dict],
    vm_rows: list[dict],
    now: datetime | None = None,
    report_month: date | None = None,
) -> dict:
    reference = now or datetime.now(UTC)
    month = report_month or reference.date().replace(day=1)
    severity_counter: Counter[str] = Counter()
    backup_kind_counter: Counter[str] = Counter()
    cluster_map = {row["id"]: row["name"] for row in cluster_rows}
    cluster_counter: Counter[str] = Counter()

    detailed_rows: list[dict] = []
    for vm in vm_rows:
        backup_kind = vm.get("status_backup_kind") or vm.get("effective_backup_kind", vm["backup_kind"])
        severity, age_days = assess_backup_status(backup_kind, vm["last_backup_at"], reference)
        severity_counter[severity] += 1
        backup_kind_counter[backup_kind] += 1
        cluster_counter[cluster_map.get(vm["cluster_id"], "unknown")] += 1
        detailed_rows.append(
            {
                "cluster_name": cluster_map.get(vm["cluster_id"], "unknown"),
                "vm_name": vm["name"],
                "node_name": vm["node_name"],
                "backup_kind": backup_kind,
                "last_backup_at": vm["last_backup_at"],
                "severity": severity,
                "age_days": age_days,
            }
        )

    return {
        "generated_at": reference,
        "report_month": month,
        "report_month_label": month.strftime("%m/%Y"),
        "totals": {
            "clusters": len(cluster_rows),
            "vms": len(vm_rows),
            "critical": severity_counter["critical"],
            "warning": severity_counter["warning"],
            "ok": severity_counter["ok"],
        },
        "by_kind": dict(backup_kind_counter),
        "by_cluster": dict(cluster_counter),
        "rows": detailed_rows,
    }


def serialize_report_payload(value):
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, dict):
        return {key: serialize_report_payload(item) for key, item in value.items()}
    if isinstance(value, list):
        return [serialize_report_payload(item) for item in value]
    return value


def build_backup_period_report(
    period_start: datetime,
    period_end: datetime,
    events: list[dict],
) -> dict:
    """Build a report for a time period based on backup events."""
    report_events = filter_reportable_backup_events(events, period_end)
    total_ok = sum(1 for e in report_events if e["status_for_report"] == "ok")
    total_failed = sum(1 for e in report_events if e["status_for_report"] == "failed")
    total_deleted = sum(1 for e in report_events if e["status_for_report"] == "deleted")

    # Group by cluster + VM
    vm_groups: dict[tuple, list] = {}
    for event in report_events:
        key = (event["cluster_name"], event["vm_name"], event["node_name"])
        vm_groups.setdefault(key, []).append(event)

    rows = []
    for (cluster_name, vm_name, node_name), evts in sorted(vm_groups.items()):
        rows.append({
            "cluster_name": cluster_name,
            "vm_name": vm_name,
            "node_name": node_name,
            "event_count": len(evts),
            "ok_count": sum(1 for e in evts if e["status_for_report"] == "ok"),
            "failed_count": sum(1 for e in evts if e["status_for_report"] == "failed"),
            "deleted_count": sum(1 for e in evts if e["status_for_report"] == "deleted"),
            "events": sorted(evts, key=lambda e: e["started_at"], reverse=True),
        })

    return {
        "period_start": period_start,
        "period_end": period_end,
        "period_label": f"{period_start.strftime('%d.%m.%Y')} – {period_end.strftime('%d.%m.%Y')}",
        "generated_at": datetime.now(UTC),
        "totals": {
            "events": len(report_events),
            "ok": total_ok,
            "failed": total_failed,
            "deleted": total_deleted,
            "vms": len(vm_groups),
        },
        "rows": rows,
        "raw_events": report_events,
    }


def filter_reportable_backup_events(
    events: list[dict],
    reference_end: datetime,
    retention_days: int = REPORT_REMOVED_RETENTION_DAYS,
) -> list[dict]:
    cutoff = reference_end - timedelta(days=retention_days)
    reportable: list[dict] = []
    for event in events:
        removed_at = event.get("removed_at")
        started_at = event.get("started_at")
        if removed_at is not None and started_at is not None and started_at < cutoff:
            continue

        status_for_report = "deleted" if removed_at is not None else ("ok" if event.get("status") == "ok" else "failed")
        enriched = dict(event)
        enriched["status_for_report"] = status_for_report
        reportable.append(enriched)
    return reportable


def build_month_comparison(
    current_events: list[dict],
    previous_events: list[dict],
    reference_end: datetime | None = None,
) -> dict:
    """Build a month-over-month comparison summary."""
    reference = reference_end or datetime.now(UTC)
    current_reportable = filter_reportable_backup_events(current_events, reference)
    previous_reportable = filter_reportable_backup_events(previous_events, reference)

    cur_ok = sum(1 for e in current_reportable if e.get("status_for_report") == "ok")
    cur_fail = sum(1 for e in current_reportable if e.get("status_for_report") == "failed")
    cur_total = len(current_reportable)
    cur_vms = len({(e.get("cluster_name", ""), e.get("vm_name", "")) for e in current_reportable})

    prev_ok = sum(1 for e in previous_reportable if e.get("status_for_report") == "ok")
    prev_fail = sum(1 for e in previous_reportable if e.get("status_for_report") == "failed")
    prev_total = len(previous_reportable)
    prev_vms = len({(e.get("cluster_name", ""), e.get("vm_name", "")) for e in previous_reportable})

    def delta_pct(cur: int, prev: int) -> float | None:
        if prev == 0:
            return None
        return round((cur - prev) / prev * 100, 1)

    return {
        "current": {"total": cur_total, "ok": cur_ok, "failed": cur_fail, "vms": cur_vms},
        "previous": {"total": prev_total, "ok": prev_ok, "failed": prev_fail, "vms": prev_vms},
        "delta_total_pct": delta_pct(cur_total, prev_total),
        "delta_ok_pct": delta_pct(cur_ok, prev_ok),
        "delta_failed_pct": delta_pct(cur_fail, prev_fail),
        "delta_vms": cur_vms - prev_vms,
        "has_previous": prev_total > 0,
    }


def build_compliance_summary(governance_rows: list[dict], restore_tests: list[dict]) -> dict:
    return {
        "restore_required": sum(1 for row in governance_rows if row["restore_required"]),
        "restore_overdue": sum(1 for row in governance_rows if row["restore_due_status"] in {"warning", "critical"}),
        "restore_critical": sum(1 for row in governance_rows if row["restore_due_status"] == "critical"),
        "tested_this_month": sum(1 for row in restore_tests if row["tested_at"].month == datetime.now(UTC).month),
    }
