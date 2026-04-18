from __future__ import annotations

import json
import logging
from collections import OrderedDict
from datetime import UTC, datetime
from pathlib import Path

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse
from starlette import status

from app import db
from app.config import REPORT_DIR, REPORT_LANGUAGE
from app.services.pdf_reports import write_backup_period_pdf
from app.services.reporting import build_backup_period_report, build_month_comparison, serialize_report_payload
from app.web.common import common_context, templates

logger = logging.getLogger(__name__)
router = APIRouter()


@router.get("/reports", response_class=HTMLResponse)
def reports_page(request: Request) -> HTMLResponse:
    events = db.list_all_report_backup_events()
    current_report = build_backup_period_report(datetime.min.replace(tzinfo=UTC), datetime.now(UTC), events)
    current_report["period_label"] = "Alle wiederherstellbaren Backups"
    clusters_grouped: dict[str, dict] = OrderedDict()
    for vm_row in current_report["rows"]:
        cluster_name = vm_row["cluster_name"]
        if cluster_name not in clusters_grouped:
            clusters_grouped[cluster_name] = {
                "vms": [],
                "event_count": 0,
                "ok_count": 0,
                "failed_count": 0,
                "deleted_count": 0,
            }
        group = clusters_grouped[cluster_name]
        group["vms"].append(vm_row)
        group["event_count"] += vm_row["event_count"]
        group["ok_count"] += vm_row["ok_count"]
        group["failed_count"] += vm_row["failed_count"]
        group["deleted_count"] += vm_row["deleted_count"]
    return templates.TemplateResponse(
        request,
        "report.html",
        common_context(request)
        | {
            "current_report": current_report,
            "clusters_grouped": clusters_grouped,
            "archived_reports": db.list_monthly_reports(),
        },
    )


@router.post("/reports/generate")
async def generate_report() -> RedirectResponse:
    import asyncio
    from datetime import timedelta

    loop = asyncio.get_event_loop()

    def _build_report():
        now = datetime.now(UTC)
        events = db.list_all_report_backup_events()
        report = build_backup_period_report(datetime.min.replace(tzinfo=UTC), now, events)
        report["period_label"] = "Alle wiederherstellbaren Backups"
        try:
            cur_month = now.date().replace(day=1)
            cur_events = db.backup_events_for_month(cur_month.year, cur_month.month)
            prev_month = (cur_month - timedelta(days=1)).replace(day=1)
            prev_events = db.backup_events_for_month(prev_month.year, prev_month.month)
            report["comparison"] = build_month_comparison(cur_events, prev_events, now)
        except Exception as exc:
            logger.warning("Monatsvergleich fehlgeschlagen: %s", exc)
        snapshot = serialize_report_payload(report)
        month_date = now.date().replace(day=1)
        month_slug = month_date.strftime("%Y-%m")
        timestamp_slug = report["generated_at"].strftime("%Y%m%d-%H%M%S")
        pdf_path = REPORT_DIR / f"backup-report-{month_slug}-{timestamp_slug}.pdf"
        json_path = REPORT_DIR / f"backup-report-{month_slug}-{timestamp_slug}.json"
        try:
            from app.db_notifications import list_notification_logs_for_period

            notifs = list_notification_logs_for_period(
                datetime(month_date.year, month_date.month, 1, tzinfo=UTC),
                now,
            )
        except Exception:
            notifs = []
        write_backup_period_pdf(report, pdf_path, notifications=notifs, lang=REPORT_LANGUAGE)
        json_path.write_text(json.dumps(snapshot, ensure_ascii=False, indent=2), encoding="utf-8")
        db.archive_monthly_report(month_date, "adhoc", pdf_path, json_path, report)

    await loop.run_in_executor(None, _build_report)
    return RedirectResponse(url="/reports", status_code=status.HTTP_303_SEE_OTHER)


@router.post("/reports/archive/{report_id}/delete")
def delete_report(report_id: int) -> RedirectResponse:
    deleted = db.delete_monthly_report(report_id)
    if deleted:
        for path_key in ("pdf_path", "json_path"):
            path = Path(deleted[path_key])
            if path.exists():
                path.unlink(missing_ok=True)
    return RedirectResponse(url="/reports", status_code=status.HTTP_303_SEE_OTHER)


@router.get("/reports/archive/{report_id}/pdf")
def download_report_pdf(report_id: int) -> FileResponse:
    archived = db.get_monthly_report(report_id)
    if not archived:
        raise HTTPException(status_code=404, detail="Report not found")
    try:
        date_str = archived["generated_at"].strftime("%Y%m%d")
    except Exception:
        date_str = str(report_id)
    return FileResponse(
        path=archived["pdf_path"],
        media_type="application/pdf",
        filename=f"backup-report-{date_str}.pdf",
        content_disposition_type="attachment",
    )


@router.get("/reports/archive/{report_id}/json")
def download_report_json(report_id: int) -> FileResponse:
    archived = db.get_monthly_report(report_id)
    if not archived:
        raise HTTPException(status_code=404, detail="Report not found")
    try:
        date_str = archived["generated_at"].strftime("%Y%m%d")
    except Exception:
        date_str = str(report_id)
    return FileResponse(
        path=archived["json_path"],
        media_type="application/json",
        filename=f"backup-report-{date_str}.json",
        content_disposition_type="attachment",
    )
