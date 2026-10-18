"""Weekly summary email: what needs attention, in one message instead of many alerts.

`collect()` gathers the week once; `subject()` and `text()` turn it into the subject
and plain-text part, `render_html()` into the HTML part of the same email.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Any
from urllib.parse import quote

from jinja2 import Environment, FileSystemLoader, select_autoescape

from app.i18n import ts
from app.services.theme import best_ink, contrast_ratio

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


@dataclass
class Item:
    """One line of a section: a VM (cluster + name) or a datastore, with an optional detail."""

    name: str
    cluster: str = ""
    detail: str = ""
    tone: str = "warning"  # critical | warning | info
    cluster_slug: str = ""
    pct: int | None = None  # datastores: usage bar

    @property
    def label(self) -> str:
        return f"{self.cluster} / {self.name}" if self.cluster else self.name


@dataclass
class Section:
    key: str  # i18n key of the heading, takes {count}
    tone: str  # critical | warning | info
    items: list[Item] = field(default_factory=list)


@dataclass
class ClusterLine:
    """One cluster's row: the figures of its dashboard card plus the week's failed backups."""

    name: str
    slug: str
    nodes: int
    vms: int
    critical: int
    warning: int
    restore_due: int
    encrypted_pct: int
    failed: int
    sync_ok: bool

    @property
    def tone(self) -> str:
        if self.critical or not self.sync_ok:
            return "critical"
        return "warning" if self.warning or self.failed else "ok"


@dataclass
class WeeklyReport:
    week: int
    year: int
    period_start: date
    period_end: date
    vms: int
    critical: int
    warning: int
    failed: int
    restore_expired: int
    restore_open: int
    restore_tested: int
    problems: list[Section]
    infos: list[Section]
    clusters: list[ClusterLine] = field(default_factory=list)

    @property
    def problem_count(self) -> int:
        return sum(len(s.items) for s in self.problems)


def collect(
    rows: list[dict[str, Any]],
    datastores: list[dict[str, Any]],
    stats: dict[str, Any],
    now: datetime,
    clusters: list[dict[str, Any]] = (),
) -> WeeklyReport:
    """Everything the report shows, from governance rows, datastore usage, 7-day backup
    stats and the dashboard's cluster summaries (`db.cluster_summaries()`)."""
    vms = [r for r in rows if not r.get("vm_gone")]
    critical = [r for r in vms if r["backup_severity"] == "critical"]
    warning = [r for r in vms if r["backup_severity"] == "warning"]
    week_ago = (now - timedelta(days=7)).date()

    def vm(r: dict[str, Any], detail: str = "", tone: str = "warning") -> Item:
        return Item(r["vm_name"], r["cluster_name"], detail, tone, r.get("cluster_slug", ""))

    problems = [
        Section(
            "weekly.section_backup",
            "warning",
            [
                vm(
                    r,
                    ts("weekly.severity_" + r["backup_severity"], days=r.get("backup_age_days") or 0),
                    r["backup_severity"],
                )
                for r in critical + warning
            ],
        ),
        Section(
            "weekly.section_verify",
            "warning",
            [
                vm(r, ts("weekly.verify_" + r["verify_status"]), r["verify_status"])
                for r in vms
                if r.get("verify_status") in ("critical", "warning")
            ],
        ),
        Section(
            "weekly.section_datastores",
            "warning",
            [
                Item(
                    d["store"],
                    d["pbs_name"],
                    ts("weekly.full_at", date=d["estimated_full_at"].strftime("%d.%m.%Y"))
                    if d.get("estimated_full_at")
                    else "",
                    d["status"],
                    pct=d["pct"],
                )
                for d in datastores
                if d["status"] in ("critical", "warning")
            ],
        ),
    ]
    for section in problems:  # a section is as severe as its worst item
        if any(item.tone == "critical" for item in section.items):
            section.tone = "critical"
    # Information, not problems: full runs (slower, not an error) and VMs whose
    # backups sit on a single server.
    infos = [
        Section(
            "weekly.section_full_runs",
            "warning",
            [
                Item(*reversed(full.split(" / ", 1))) if " / " in full else Item(full)
                for full in stats.get("full_runs", [])
            ],
        ),
        Section("weekly.section_copies", "info", [vm(r, tone="info") for r in vms if r.get("copy_status") == "single"]),
    ]

    year, week, _ = now.isocalendar()
    return WeeklyReport(
        week=week,
        year=year,
        period_start=week_ago,
        period_end=(now - timedelta(days=1)).date(),
        vms=len(vms),
        critical=len(critical),
        warning=len(warning),
        failed=stats.get("failed", 0),
        restore_expired=sum(1 for r in vms if r["restore_due_status"] == "critical"),
        restore_open=sum(1 for r in vms if r["restore_due_status"] == "open"),
        restore_tested=sum(
            1
            for r in vms
            if isinstance(r.get("last_restore_tested_at"), date) and r["last_restore_tested_at"] >= week_ago
        ),
        problems=[s for s in problems if s.items],
        infos=[s for s in infos if s.items],
        clusters=[
            ClusterLine(
                name=c["cluster_name"],
                slug=c["cluster_slug"],
                nodes=c["node_count"],
                vms=c["vm_count"],
                critical=c["critical_count"],
                warning=c["warning_count"],
                restore_due=c["overdue_restore_count"],
                encrypted_pct=c["encryption"]["pct_encrypted"],
                failed=stats.get("failed_by_cluster", {}).get(c["cluster_slug"], 0),
                # Failing since the last success (cleared on the next successful sync).
                sync_ok=not c.get("last_pve_sync_failure_started_at"),
            )
            for c in clusters
        ],
    )


def subject(report: WeeklyReport) -> str:
    return ts("weekly.subject", week=report.week, year=report.year, critical=report.critical)


def text(report: WeeklyReport) -> str:
    """Plain-text part (for clients without HTML)."""
    lines = [
        ts("weekly.summary", vms=report.vms, critical=report.critical, warning=report.warning, failed=report.failed),
        "",
    ]
    if report.clusters:
        lines.append(ts("weekly.title_clusters") + ":")
        lines += [
            "- "
            + ts(
                "weekly.cluster_line",
                name=c.name,
                vms=c.vms,
                critical=c.critical,
                warning=c.warning,
                failed=c.failed,
                restore=c.restore_due,
            )
            + ("" if c.sync_ok else " · " + ts("weekly.cluster_sync_failing"))
            for c in report.clusters
        ]
        lines.append("")

    def line(item: Item) -> str:
        if item.pct is not None:
            return f"- {item.label}: {item.pct} %" + (f" ({item.detail})" if item.detail else "")
        return f"- {item.label}" + (f": {item.detail}" if item.detail else "")

    def section_lines(section: Section) -> list[str]:
        items = [line(item) for item in section.items]
        return [ts(section.key, count=len(items)), *_listed(items), ""]

    for section in report.problems:
        lines += section_lines(section)
    if not report.problems:
        lines += [ts("weekly.all_good"), ""]
    for section in report.infos:
        lines += section_lines(section)
    lines.append(
        ts("weekly.restore", expired=report.restore_expired, open=report.restore_open, tested=report.restore_tested)
    )
    return "\n".join(lines)


def build(
    rows: list[dict[str, Any]],
    datastores: list[dict[str, Any]],
    stats: dict[str, Any],
    now: datetime,
) -> tuple[str, str]:
    """(subject, plain-text body) — see `collect()`."""
    report = collect(rows, datastores, stats, now)
    return subject(report), text(report)


_env = Environment(loader=FileSystemLoader("templates"), autoescape=select_autoescape(["html"]))


def render_html(
    report: WeeklyReport,
    *,
    primary: str,
    accent: str,
    app_url: str,
    version: str,
    images: dict[str, str] | None = None,
) -> str:
    """HTML part: table layout with inline styles, so mail clients (Outlook included) render it as designed.

    `images` maps "wordmark" / "logo" to an image source (cid: in the email,
    data: in the preview); missing ones fall back to text.
    """
    base = app_url.rstrip("/")

    def link(item: Item) -> str:
        return f"{base}/clusters/{quote(item.cluster_slug)}" if base and item.cluster_slug else ""

    header_ink = best_ink(primary, "#ffffff")
    return _env.get_template("email/weekly_report.html").render(
        r=report,
        t=ts,
        link=link,
        max_listed=MAX_LISTED,
        primary=primary,
        header_ink=header_ink,
        # The accent marks the brand line in the header — only if it stands out there.
        mark=accent if contrast_ratio(accent, primary) >= 3 else header_ink,
        app_url=base,
        version=version,
        images=images or {},
    )
