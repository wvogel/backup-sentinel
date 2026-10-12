"""Weekly summary email: what needs attention, in one message instead of many alerts."""

from __future__ import annotations

from datetime import date, datetime, timedelta
from typing import Any

from app.i18n import ts

SEND_HOUR = 7  # Monday, local time
MAX_LISTED = 15


def due_week(now_local: datetime, last_sent: str) -> str | None:
    """ISO week key ("2026-W42") if this week's report is due, else None.

    Due from Monday 07:00 local time; a week missed while the server was down
    is still sent later in that week.
    """
    year, week, weekday = now_local.isocalendar()
    if weekday == 1 and now_local.hour < SEND_HOUR:
        return None
    key = f"{year}-W{week:02d}"
    return None if key == last_sent else key


def _listed(lines: list[str]) -> list[str]:
    if len(lines) <= MAX_LISTED:
        return lines
    return [*lines[:MAX_LISTED], ts("weekly.more", count=len(lines) - MAX_LISTED)]


def build(
    rows: list[dict[str, Any]],
    datastores: list[dict[str, Any]],
    stats: dict[str, Any],
    now: datetime,
) -> tuple[str, str]:
    """(subject, plain-text body) from governance rows, datastore usage and 7-day backup stats."""
    vms = [r for r in rows if not r.get("vm_gone")]
    name = lambda r: f"{r['cluster_name']} / {r['vm_name']}"  # noqa: E731
    critical = [r for r in vms if r["backup_severity"] == "critical"]
    warning = [r for r in vms if r["backup_severity"] == "warning"]
    week_ago = (now - timedelta(days=7)).date()

    sections: list[tuple[str, list[str]]] = [
        (
            "weekly.section_backup",
            [
                f"- {name(r)}: {ts('weekly.severity_' + r['backup_severity'], days=r.get('backup_age_days') or 0)}"
                for r in critical + warning
            ],
        ),
        (
            "weekly.section_verify",
            [
                f"- {name(r)}: {ts('weekly.verify_' + r['verify_status'])}"
                for r in vms
                if r.get("verify_status") in ("critical", "warning")
            ],
        ),
        ("weekly.section_full_runs", [f"- {vm}" for vm in stats.get("full_runs", [])]),
        (
            "weekly.section_datastores",
            [
                f"- {d['pbs_name']} / {d['store']}: {d['pct']} %"
                + (
                    f" ({ts('weekly.full_at', date=d['estimated_full_at'].strftime('%d.%m.%Y'))})"
                    if d.get("estimated_full_at")
                    else ""
                )
                for d in datastores
                if d["status"] in ("critical", "warning")
            ],
        ),
    ]

    lines = [
        ts("weekly.summary", vms=len(vms), critical=len(critical), warning=len(warning), failed=stats.get("failed", 0)),
        "",
    ]
    problems = False
    for key, items in sections:
        if items:
            problems = True
            lines += [ts(key, count=len(items)), *_listed(items), ""]
    if not problems:
        lines += [ts("weekly.all_good"), ""]
    # Information, not a problem: VMs whose backups sit on a single server.
    single = [f"- {name(r)}" for r in vms if r.get("copy_status") == "single"]
    if single:
        lines += [ts("weekly.section_copies", count=len(single)), *_listed(single), ""]

    tested = sum(
        1 for r in vms if isinstance(r.get("last_restore_tested_at"), date) and r["last_restore_tested_at"] >= week_ago
    )
    lines.append(
        ts(
            "weekly.restore",
            expired=sum(1 for r in vms if r["restore_due_status"] == "critical"),
            open=sum(1 for r in vms if r["restore_due_status"] == "open"),
            tested=tested,
        )
    )
    year, week, _ = now.isocalendar()
    return ts("weekly.subject", week=week, year=year, critical=len(critical)), "\n".join(lines)
