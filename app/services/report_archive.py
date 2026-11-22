"""Generate the NIS2 report (overall + one PDF per cluster + JSON) and archive
it — shared by the button on /reports (interim) and the monthly run."""

from __future__ import annotations

import json
import logging
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from app import db
from app.config import DEFAULT_TIMEZONE, REPORT_DIR, REPORT_LANGUAGE
from app.services.nis2_render import render_html, write_pdf
from app.services.nis2_report import build_nis2_report, cluster_view
from app.services.reporting import serialize_report_payload
from app.services.theme_store import active_theme

logger = logging.getLogger(__name__)

# Layout version of the archived PDFs. Raise it when a change must reach the
# archive too: rebuild_report_archive() then renders every month anew, once.
REPORT_FORMAT = 2
_FORMAT_SETTING = "report_archive_format"


def generate_archived_report(month: date, trigger_mode: str, now: datetime | None = None) -> Path:
    """Report for `month` (interim while it runs); returns the overall PDF's path."""
    now = now or datetime.now(UTC)
    report = build_nis2_report(month, trigger=trigger_mode, lang=REPORT_LANGUAGE, now=now)
    try:
        theme_colors = active_theme()[1]
    except Exception as exc:  # no settings table yet, DB hiccup: default colours
        logger.warning("Report theme unavailable: %s", exc)
        theme_colors = None

    stem = f"nis2-report-{month:%Y-%m}-{now:%Y%m%d-%H%M%S}"
    pdf_path = REPORT_DIR / f"{stem}.pdf"
    write_pdf(render_html(report, theme_colors=theme_colors), pdf_path)
    files = {"all": str(pdf_path)}
    for cluster in report["clusters"]:
        view = cluster_view(report, cluster["slug"])
        target = REPORT_DIR / f"{stem}-{cluster['slug']}.pdf"
        write_pdf(render_html(view, theme_colors=theme_colors), target)
        files[cluster["slug"]] = str(target)

    json_path = REPORT_DIR / f"{stem}.json"
    payload = serialize_report_payload(report)
    json_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    db.archive_monthly_report(month, trigger_mode, pdf_path, json_path, payload, files, REPORT_FORMAT)
    return pdf_path


def remove_archived_files(row: dict[str, Any]) -> None:
    """Delete the files of an archived report (the DB row is deleted separately)."""
    for path in {row["pdf_path"], row["json_path"], *(row.get("files") or {}).values()}:
        Path(path).unlink(missing_ok=True)


def request_archive_rebuild() -> None:
    """Rebuild the archive with the next sync — the evidence start of a cluster
    changed, so every monthly report must be measured anew."""
    db.save_settings({_FORMAT_SETTING: "0"})


def rebuild_report_archive(now: datetime | None = None) -> int:
    """Once per REPORT_FORMAT (or after request_archive_rebuild): replace every
    monthly report and every report of an older layout, reconstructing the
    monthly report of each finished month since recording began. Interim
    reports of an older layout are dropped — the month's report replaces them.
    Returns how many reports were written."""
    settings = db.get_settings_dict(_FORMAT_SETTING)
    if int(settings.get(_FORMAT_SETTING) or 0) >= REPORT_FORMAT:
        return 0
    now = now or datetime.now(UTC)
    for row in db.list_monthly_reports():
        if row["trigger_mode"] == "auto" or (row.get("format") or 1) < REPORT_FORMAT:
            db.delete_monthly_report(row["id"])
            remove_archived_files(row)
    written = 0
    starts = db.nis2_recording_start_by_cluster().values()
    if starts:
        tz = ZoneInfo(DEFAULT_TIMEZONE)
        month = min(starts).astimezone(tz).date().replace(day=1)
        current = now.astimezone(tz).date().replace(day=1)
        while month < current:
            if not db.get_monthly_report_for_month(month):
                generate_archived_report(month, "auto", now)
                written += 1
            month = date(month.year + (month.month == 12), month.month % 12 + 1, 1)
    db.save_settings({_FORMAT_SETTING: str(REPORT_FORMAT)})
    logger.info("Report archive rebuilt for layout %d: %d monthly reports", REPORT_FORMAT, written)
    return written
