from __future__ import annotations

import asyncio
import logging
from datetime import UTC, date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse
from starlette import status

from app import db
from app.config import DEFAULT_TIMEZONE, REPORT_LANGUAGE
from app.services.nis2_render import render_html
from app.services.nis2_report import build_nis2_report, cluster_view
from app.services.report_archive import generate_archived_report, remove_archived_files
from app.services.theme_store import active_theme
from app.web.common import common_context, templates

logger = logging.getLogger(__name__)
router = APIRouter()


def _current_month(now: datetime | None = None) -> date:
    local = (now or datetime.now(UTC)).astimezone(ZoneInfo(DEFAULT_TIMEZONE))
    return local.date().replace(day=1)


def _parse_month(value: str | None) -> date:
    if not value:
        return _current_month()
    try:
        return datetime.strptime(value, "%Y-%m").date()
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="month must be YYYY-MM") from exc


@router.get("/reports", response_class=HTMLResponse)
def reports_page(request: Request) -> HTMLResponse:
    # Month to date, without the 12-month history: the same numbers the next report shows.
    report = build_nis2_report(_current_month(), trigger="manual", lang=REPORT_LANGUAGE, with_trend=False)
    archived = db.list_monthly_reports()
    slug_names = {c["slug"]: c["name"] for c in report["clusters"]}
    for row in archived:
        files = row.get("files") or {}
        row["cluster_files"] = sorted(
            ((slug, slug_names.get(slug, slug)) for slug in files if slug != "all"), key=lambda f: f[1].casefold()
        )
    return templates.TemplateResponse(
        request,
        "report.html",
        common_context(request) | {"nis2": report, "archived_reports": archived},
    )


@router.get("/reports/preview", response_class=HTMLResponse)
def report_preview(cluster: str | None = None, month: str | None = None) -> HTMLResponse:
    """The report as HTML in the browser — overall, or one cluster (`?cluster=slug`)."""
    report = build_nis2_report(_parse_month(month), trigger="manual", lang=REPORT_LANGUAGE)
    if cluster:
        report = cluster_view(report, cluster)
        if report is None:
            raise HTTPException(status_code=404, detail="Cluster not found")
    return HTMLResponse(render_html(report, theme_colors=active_theme()[1], preview=True))


@router.post("/reports/generate")
async def generate_report() -> RedirectResponse:
    """Interim report: the current month up to now."""
    now = datetime.now(UTC)
    await asyncio.get_running_loop().run_in_executor(None, generate_archived_report, _current_month(now), "manual", now)
    return RedirectResponse(url="/reports", status_code=status.HTTP_303_SEE_OTHER)


@router.post("/reports/archive/{report_id}/delete")
def delete_report(report_id: int) -> RedirectResponse:
    deleted = db.delete_monthly_report(report_id)
    if deleted:
        remove_archived_files(deleted)
    return RedirectResponse(url="/reports", status_code=status.HTTP_303_SEE_OTHER)


def _download_name(archived: dict, suffix: str, ext: str) -> str:
    stem = "nis2-report" if archived.get("files") else "backup-report"
    try:
        month = archived["report_month"].strftime("%Y-%m")
    except Exception:
        month = str(archived["id"])
    return f"{stem}-{month}{'-' + suffix if suffix else ''}.{ext}"


@router.get("/reports/archive/{report_id}/pdf")
def download_report_pdf(report_id: int, scope: str = "all") -> FileResponse:
    archived = db.get_monthly_report(report_id)
    if not archived:
        raise HTTPException(status_code=404, detail="Report not found")
    files = archived.get("files") or {}
    path = files.get(scope) or (archived["pdf_path"] if scope == "all" else None)
    if not path or not Path(path).exists():
        raise HTTPException(status_code=404, detail="Report file not found")
    return FileResponse(
        path=path,
        media_type="application/pdf",
        filename=_download_name(archived, "" if scope == "all" else scope, "pdf"),
        content_disposition_type="attachment",
    )


@router.get("/reports/archive/{report_id}/json")
def download_report_json(report_id: int) -> FileResponse:
    archived = db.get_monthly_report(report_id)
    if not archived:
        raise HTTPException(status_code=404, detail="Report not found")
    return FileResponse(
        path=archived["json_path"],
        media_type="application/json",
        filename=_download_name(archived, "", "json"),
        content_disposition_type="attachment",
    )
