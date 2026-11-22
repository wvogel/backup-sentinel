"""Generate the NIS2 report (overall + one PDF per cluster + JSON) and archive
it — shared by the button on /reports (interim) and the monthly run."""

from __future__ import annotations

import json
import logging
from datetime import UTC, date, datetime
from pathlib import Path

from app import db
from app.config import REPORT_DIR, REPORT_LANGUAGE
from app.services.nis2_render import render_html, write_pdf
from app.services.nis2_report import build_nis2_report, cluster_view
from app.services.reporting import serialize_report_payload
from app.services.theme_store import active_theme

logger = logging.getLogger(__name__)


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
    db.archive_monthly_report(month, trigger_mode, pdf_path, json_path, payload, files)
    return pdf_path
