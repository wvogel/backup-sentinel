"""Assemble the NIS2 evidence report: metrics per cluster and overall, gaps,
restore-test protocol, calendars and the 12-month trend (spec 2026-11-22).

The result is a plain dict (JSON-serialisable via serialize_report_payload);
rendering lives in nis2_render.py.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from app import db
from app.config import DEFAULT_TIMEZONE
from app.i18n import tl
from app.services import nis2_metrics as m
from app.services.backup_status import format_duration
from app.services.human_format import format_count
from app.services.restore_probe import check_chips

TREND_MONTHS = 12
DUE_LIST_LIMIT = 3
_PASSED = ("passed", "limited")


@dataclass(frozen=True)
class Period:
    start: datetime  # UTC
    end: datetime  # UTC, exclusive
    month: date  # first of the month the period belongs to
    interim: bool  # current month up to now
    tz: str

    @property
    def local_days(self) -> list[date]:
        tz = ZoneInfo(self.tz)
        first = self.start.astimezone(tz).date()
        last = (self.end - timedelta(microseconds=1)).astimezone(tz).date()
        return [first + timedelta(days=i) for i in range((last - first).days + 1)]

    @property
    def reference_date(self) -> date:
        """The local date the restore evidence is judged at (last day of the period)."""
        return (self.end - timedelta(microseconds=1)).astimezone(ZoneInfo(self.tz)).date()


def period_for(month: date, *, now: datetime | None = None, tz: str = DEFAULT_TIMEZONE) -> Period:
    """The calendar month; cut at `now` while it is still running (interim report)."""
    start, end = m.month_bounds(month, tz)
    now = now or datetime.now(UTC)
    if now < end:
        return Period(start, max(now, start), month, True, tz)
    return Period(start, end, month, False, tz)


def _day_bounds(day: date, tz: str) -> tuple[datetime, datetime]:
    z = ZoneInfo(tz)
    a = datetime(day.year, day.month, day.day, tzinfo=z)
    b = a + timedelta(days=1)
    b = datetime(b.year, b.month, b.day, tzinfo=z)
    return a.astimezone(UTC), b.astimezone(UTC)


def _guest_window(g: dict, start: datetime, end: datetime, data_start: datetime | None) -> tuple[datetime, datetime]:
    ws = max(start, g["first_seen_at"], data_start or end)
    we = min(end, g["missing_since"]) if g["missing_since"] else end
    return ws, we


def _in_restore_scope(g: dict, end: datetime) -> bool:
    return bool(g["restore_required"]) and (g["missing_since"] is None or g["missing_since"] >= end)


def _scope_kpis(
    guests: list[dict],
    backups: dict[int, list[datetime]],
    tests: dict[int, list[date]],
    start: datetime,
    end: datetime,
    ref_date: date,
    data_start: dict[int, datetime],
    interim: bool = False,
) -> dict[str, Any]:
    """The two KPIs (and their raw parts) for guests in [start, end)."""
    window = uncovered = 0
    covs: list[tuple[dict, m.Coverage, datetime, datetime]] = []
    restore = {"valid": 0, "overdue": 0, "never": 0}
    states: dict[int, str] = {}
    for g in guests:
        if g["backup_kind"] == "none":
            continue
        ws, we = _guest_window(g, start, end, data_start.get(g["cluster_id"]))
        if we <= ws:
            continue
        if g["backup_kind"] == "weekly":
            cov = m.monthly_coverage(backups.get(g["id"], []), ws, we, interim=interim)
        else:
            cov = m.guest_coverage(backups.get(g["id"], []), m.effective_rpo_hours(g["rpo_hours"], "daily"), ws, we)
        window += cov.window_seconds
        uncovered += cov.uncovered_seconds
        covs.append((g, cov, ws, we))
        if _in_restore_scope(g, end):
            interval = m.restore_interval_days(g["restore_cadence"], g["backup_kind"])
            state = m.restore_state(tests.get(g["id"], []), interval, ref_date)
            restore[state] += 1
            states[g["id"]] = state
    restore_total = sum(restore.values())
    return {
        "window_seconds": window,
        "uncovered_seconds": uncovered,
        "rpo_pct": m.whole_pct(window - uncovered, window),
        "restore_pct": m.whole_pct(restore["valid"], restore_total),
        "rpo_exact": m.exact_pct(window - uncovered, window),
        "restore_exact": m.exact_pct(restore["valid"], restore_total),
        "restore": restore,
        "restore_total": restore_total,
        "coverages": covs,
        "restore_states": states,
    }


def _fmt_dt(value: datetime | None, tz: str, lang: str, *, with_year: bool = False) -> str:
    if value is None:
        return ""
    local = value.astimezone(ZoneInfo(tz))
    if lang == "en":
        return local.strftime("%Y-%m-%d %H:%M" if with_year else "%m-%d %H:%M")
    return local.strftime("%d.%m.%Y %H:%M" if with_year else "%d.%m. %H:%M")


def _hours(seconds: int) -> int:
    """Whole hours, truncated like the percentages."""
    return seconds // 3600


_SNAP_TIME = re.compile(r"(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2})Z?")


def _snapshot_label(row: dict, tz: str, lang: str) -> str:
    """When the restored backup was taken — what matters, not where it lies."""
    found = _SNAP_TIME.search(row.get("snapshot_id") or "")
    if not found:
        return ""
    return _fmt_dt(datetime.fromisoformat(found.group(1)).replace(tzinfo=UTC), tz, lang)


_RECOVERY_TYPES = ("full", "partial", "file")


def _recovery_label(value: str | None, lang: str) -> str:
    """A documented test's recovery type in words ("full" → "vollständiger Restore")."""
    if value in _RECOVERY_TYPES:
        return tl(lang, f"nis2.recovery_{value}")
    return value or tl(lang, "nis2.trigger_documented")


def _check_order(chip: dict) -> tuple:
    """ICMP, ARP, then TCP ports in numeric order."""
    label = chip["label"]
    if not label.startswith("TCP "):
        return (0, label != "ICMP", 0)
    port = label[4:]
    return (1, 0, int(port) if port.isdigit() else 1 << 20)


def _protocol_rows(raw: list[dict], guests_by_id: dict[int, dict], tz: str, lang: str) -> list[dict]:
    rows = []
    for r in raw:
        g = guests_by_id.get(r["vm_id"])
        if g is None:
            continue
        passed = r["result"] in _PASSED
        # Every check the run made — ICMP, ARP, each TCP port — passed or not.
        if r["kind"] == "run":
            checks = sorted(check_chips(r.get("checks")), key=_check_order)
        else:
            checks = [{"label": _recovery_label(r.get("recovery_type"), lang), "ok": None}]
        rows.append(
            {
                "at": r["at"],
                "at_label": _fmt_dt(r["at"], tz, lang),
                "vm_id": r["vm_id"],
                "vm_name": g["vm_name"],
                "vmid": g["vmid"],
                "cluster_id": g["cluster_id"],
                "cluster_name": g["cluster_name"],
                "kind": r["kind"],
                "snapshot": _snapshot_label(r, tz, lang) if r["kind"] == "run" else tl(lang, "nis2.trigger_documented"),
                "result": r["result"],
                "result_tone": "ok" if r["result"] == "passed" else ("warn" if passed else "critical"),
                "result_label": tl(lang, f"nis2.result_{r['result']}"),
                "duration_seconds": r.get("duration_seconds"),
                "duration_label": format_duration(r["duration_seconds"]) if r.get("duration_seconds") else "–",
                "checks": checks,
            }
        )
    _mark_superseded(rows)
    return rows


def _mark_superseded(rows: list[dict]) -> None:
    """A not fully passed attempt (failed, error, cancelled, limited) is superseded
    when the same guest passed a test later in the period; the report leaves it
    out. `rows` are in time order."""
    for i, row in enumerate(rows):
        row["superseded_by"] = None
        if row["result"] == "passed":
            continue
        later = next((x for x in rows[i + 1 :] if x["vm_id"] == row["vm_id"] and x["result"] == "passed"), None)
        if later:
            row["superseded_by"] = later["at_label"]


def _summary_sentences(s: dict, lang: str) -> list[str]:
    """Plain-language verdict text — only computed facts."""
    out = []
    if s["rpo_pct"] is not None:
        out.append(tl(lang, "nis2.sum_rpo", vms=s["vms"], pct=s["rpo_pct"]))
    if s["gap_count"]:
        tail = tl(lang, "nis2.sum_gaps_open", n=s["open_gaps"]) if s["open_gaps"] else tl(lang, "nis2.sum_gaps_closed")
        out.append(tl(lang, "nis2.sum_gaps", n=s["gap_count"], hours=s["gap_hours"], tail=tail))
    elif s["rpo_pct"] is not None:
        out.append(tl(lang, "nis2.sum_no_gaps"))
    if s["tests_run"]:
        out.append(tl(lang, "nis2.sum_tests", passed=s["tests_passed"], run=s["tests_run"]))
    else:
        out.append(tl(lang, "nis2.sum_no_tests"))
    missing = s["restore"]["overdue"] + s["restore"]["never"]
    if s["restore_total"]:
        out.append(tl(lang, "nis2.sum_untested", n=missing) if missing else tl(lang, "nis2.sum_all_tested"))
    return out


def _scope_detail(
    name: str,
    slug: str | None,
    guests: list[dict],
    kpis: dict,
    period: Period,
    counts: dict[int, tuple[int, int]],
    protocol: list[dict],
    tests: dict[int, list[date]],
    lang: str,
) -> dict[str, Any]:
    tz = period.tz
    gaps = []
    for g, cov, _ws, _we in kpis["coverages"]:
        for gap in cov.gaps:
            gaps.append(
                {
                    "vm_name": g["vm_name"],
                    "vmid": g["vmid"],
                    "cluster_name": g["cluster_name"],
                    "prev_backup": gap.prev_backup,
                    "next_backup": gap.next_backup,
                    "prev_label": _fmt_dt(gap.prev_backup, tz, lang) or tl(lang, "nis2.never"),
                    "next_label": _fmt_dt(gap.next_backup, tz, lang) or tl(lang, "nis2.gap_open"),
                    "without_hours": _hours(
                        int(((gap.next_backup or period.end) - gap.prev_backup).total_seconds())
                        if gap.monthly and gap.prev_backup
                        else gap.over_seconds + gap.rpo_hours * 3600
                    ),
                    "rpo_hours": gap.rpo_hours,
                    "over_hours": _hours(gap.over_seconds),
                    "monthly": gap.monthly,
                    "open": gap.open,
                    "in_period_seconds": int((gap.end - gap.start).total_seconds()),
                }
            )
    gaps.sort(key=lambda x: (-x["over_hours"], x["vm_name"]))

    # Daily strip and per-guest calendar.
    days = period.local_days
    day_pcts: list[int | None] = []
    calendar: dict[int, list[str | None]] = {}
    for g, _cov, _ws, _we in kpis["coverages"]:
        calendar[g["id"]] = []
    for day in days:
        d0, d1 = _day_bounds(day, tz)
        d0, d1 = max(d0, period.start), min(d1, period.end)
        total = unc = 0
        for g, cov, ws, we in kpis["coverages"]:
            span = m.overlap_seconds(ws, we, d0, d1)
            hole = sum(m.overlap_seconds(x.start, x.end, d0, d1) for x in cov.gaps)
            total += span
            unc += hole
            calendar[g["id"]].append(None if span == 0 else ("critical" if hole else "ok"))
        day_pcts.append(m.whole_pct(total - unc, total))
    cal_rows = [{"vm_name": g["vm_name"], "vmid": g["vmid"], "cells": calendar[g["id"]]} for g, *_ in kpis["coverages"]]

    # Restore due list: never tested first, then most overdue.
    due = []
    for g, *_ in kpis["coverages"]:
        state = kpis["restore_states"].get(g["id"])
        if state not in ("overdue", "never"):
            continue
        interval = m.restore_interval_days(g["restore_cadence"], g["backup_kind"])
        past = [d for d in tests.get(g["id"], []) if d <= period.reference_date]
        overdue_days = (period.reference_date - max(past)).days - interval if past else None
        due.append({"vm_name": g["vm_name"], "state": state, "overdue_days": overdue_days})
    due.sort(key=lambda x: (x["state"] != "never", -(x["overdue_days"] or 0), x["vm_name"]))

    vm_ids = {g["id"] for g, *_ in kpis["coverages"]}
    ok = sum(counts.get(i, (0, 0))[0] for i in vm_ids)
    failed = sum(counts.get(i, (0, 0))[1] for i in vm_ids)
    # A superseded attempt is left out entirely (figures, cluster page, appendix).
    counted = [p for p in protocol if not p["superseded_by"]]
    passed_runs = [p for p in counted if p["result"] in _PASSED]
    durations = [p["duration_seconds"] for p in passed_runs if p["duration_seconds"]]
    nodes = {g["node_name"] for g, *_ in kpis["coverages"] if g["missing_since"] is None}

    s: dict[str, Any] = {
        "name": name,
        "slug": slug,
        "vms": len(kpis["coverages"]),
        "nodes": len(nodes),
        "rpo_pct": kpis["rpo_pct"],
        "rpo_tone": m.rpo_tone(kpis["rpo_pct"]),
        "restore_pct": kpis["restore_pct"],
        "restore_tone": m.restore_tone(kpis["restore_pct"]),
        "rpo_exact": kpis["rpo_exact"],
        "restore_exact": kpis["restore_exact"],
        "restore": kpis["restore"],
        "restore_total": kpis["restore_total"],
        "gaps": gaps,
        "gap_count": len(gaps),
        "gap_hours": _hours(sum(x["in_period_seconds"] for x in gaps)),
        "longest_gap_hours": max((x["over_hours"] for x in gaps), default=0),
        "open_gaps": sum(1 for x in gaps if x["open"]),
        "backups_ok": ok,
        "backups_ok_label": format_count(ok, lang),
        "backups_failed": failed,
        "tests_run": len(counted),
        "tests_passed": len(passed_runs),
        "avg_test_label": format_duration(sum(durations) // len(durations)) if durations else "–",
        "due": due,
        "due_shown": due[:DUE_LIST_LIMIT],
        "day_pcts": day_pcts,
        "day_tones": [m.day_tone(p) for p in day_pcts],
        "calendar": cal_rows,
        "protocol": counted,
    }
    s["verdict"] = m.verdict(s["rpo_pct"], s["restore_pct"], open_gap=s["open_gaps"] > 0)
    s["verdict_label"] = tl(lang, f"nis2.verdict_{s['verdict']}")
    # Name the KPI that sets the verdict ("Handlungsbedarf: Restore-Nachweis 78 %").
    rank = {"ok": 0, "warn": 1, "critical": 2, None: -1}
    worst = max(
        (("restore", s["restore_tone"], s["restore_pct"]), ("rpo", s["rpo_tone"], s["rpo_pct"])),
        key=lambda k: rank[k[1]],
    )
    s["verdict_title"] = (
        f"{s['verdict_label']}: {tl(lang, 'nis2.kpi_' + worst[0])} {worst[2]} %"
        if s["verdict"] != "ok" and rank[worst[1]] > 0
        else s["verdict_label"]
    )
    s["summary"] = _summary_sentences(s, lang)
    return s


def build_nis2_report(
    month: date,
    *,
    trigger: str,
    lang: str,
    now: datetime | None = None,
    tz: str = DEFAULT_TIMEZONE,
    with_trend: bool = True,
) -> dict[str, Any]:
    """The whole report for `month` (interim when the month is still running).
    `with_trend=False` skips the 12-month history (the /reports page)."""
    now = now or datetime.now(UTC)
    period = period_for(month, now=now, tz=tz)
    months = m.last_months(month, TREND_MONTHS if with_trend else 1)
    trend_start = m.month_bounds(months[0], tz)[0]

    guests = db.nis2_guest_rows()
    guests_by_id = {g["id"]: g for g in guests}
    backups = db.nis2_backup_times(trend_start)
    tests = db.nis2_restore_test_dates()
    counts = db.nis2_backup_counts(period.start, period.end)
    data_start = db.nis2_earliest_event_by_cluster()
    tz_obj = ZoneInfo(tz)
    end_date = (period.end - timedelta(microseconds=1)).astimezone(tz_obj).date() + timedelta(days=1)
    protocol = _protocol_rows(
        db.nis2_restore_protocol(period.start, period.end, period.start.astimezone(tz_obj).date(), end_date),
        guests_by_id,
        tz,
        lang,
    )

    clusters: dict[int, tuple[str, str]] = {}
    for g in guests:
        clusters.setdefault(g["cluster_id"], (g["cluster_name"], g["cluster_slug"]))

    def kpis_for(scope: list[dict], start: datetime, end: datetime, ref: date, interim: bool = False) -> dict:
        return _scope_kpis(scope, backups, tests, start, end, ref, data_start, interim)

    scopes: list[tuple[str, str | None, list[dict], list[int] | None]] = [("", None, guests, None)]
    for cid, (cname, slug) in clusters.items():
        scopes.append((cname, slug, [g for g in guests if g["cluster_id"] == cid], [cid]))

    results = []
    for name, slug, scope_guests, cids in scopes:
        kpis = kpis_for(scope_guests, period.start, period.end, period.reference_date, period.interim)
        scope_protocol = [p for p in protocol if cids is None or p["cluster_id"] in cids]
        detail = _scope_detail(name, slug, scope_guests, kpis, period, counts, scope_protocol, tests, lang)
        trend = {"rpo": [], "restore": []}
        relevant = [data_start[c] for c in (cids or data_start) if c in data_start]
        first_data = min(relevant) if relevant else None
        for mo in months:
            # The line is drawn with exact values (truncated to 0.01, never rounded up).
            if mo == month:
                trend["rpo"].append(detail["rpo_exact"])
                trend["restore"].append(detail["restore_exact"])
                continue
            ms, me = m.month_bounds(mo, tz)
            if first_data is None or first_data >= me:
                trend["rpo"].append(None)  # no data yet: a hole, never 0 %
                trend["restore"].append(None)
                continue
            ref = (me - timedelta(microseconds=1)).astimezone(tz_obj).date()
            k = kpis_for(scope_guests, ms, me, ref)
            trend["rpo"].append(k["rpo_exact"])
            trend["restore"].append(k["restore_exact"])
        detail["trend"] = trend
        results.append(detail)

    overall, cluster_details = results[0], [c for c in results[1:] if c["vms"]]
    overall["clusters"] = len(cluster_details)
    return {
        "lang": lang,
        "tz": tz,
        "generated_at": now,
        "trigger": trigger,
        "period": {
            "start": period.start,
            "end": period.end,
            "month": period.month,
            "interim": period.interim,
            "days": period.local_days,
            "reference_date": period.reference_date,
        },
        "trend_months": months,
        "overall": overall,
        "clusters": cluster_details,
    }


def cluster_view(report: dict[str, Any], slug: str) -> dict[str, Any] | None:
    """The report narrowed to one cluster (its page first, its appendices)."""
    for c in report["clusters"]:
        if c["slug"] == slug:
            return report | {"overall": None, "clusters": [c], "single": True}
    return None
