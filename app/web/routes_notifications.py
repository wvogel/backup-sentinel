from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse

from app import db
from app.web.common import common_context, templates

router = APIRouter()

ITEMS_PER_PAGE = 50


@router.get("/notifications", response_class=HTMLResponse)
def notifications_page(request: Request, page: int = 1) -> HTMLResponse:
    page = max(1, page)
    offset = (page - 1) * ITEMS_PER_PAGE
    rows, total = db.list_notification_logs(days=30, limit=ITEMS_PER_PAGE, offset=offset)
    total_pages = max(1, (total + ITEMS_PER_PAGE - 1) // ITEMS_PER_PAGE)
    context = {
        "notifications": rows,
        "page": page,
        "total_pages": total_pages,
        "has_prev": page > 1,
        "has_next": page < total_pages,
    }
    return templates.TemplateResponse("notifications.html", common_context(request) | context)
