from __future__ import annotations

import csv
import io
from urllib.parse import urlencode

from fastapi import APIRouter, Query, Request
from fastapi.responses import HTMLResponse, StreamingResponse

from app import db
from app.web.common import common_context, templates

router = APIRouter()

ITEMS_PER_PAGE = 50


@router.get("/notifications", response_class=HTMLResponse)
def notifications_page(
    request: Request,
    page: int = 1,
    notification_type: str | None = Query(None),
    cluster_name: str | None = Query(None),
    channel: str | None = Query(None),
) -> HTMLResponse:
    page = max(1, page)
    offset = (page - 1) * ITEMS_PER_PAGE
    rows, total = db.list_notification_logs(
        days=30,
        limit=ITEMS_PER_PAGE,
        offset=offset,
        notification_type=notification_type or None,
        cluster_name=cluster_name or None,
        channel=channel or None,
    )
    filters = db.list_distinct_filters(days=30)
    total_pages = max(1, (total + ITEMS_PER_PAGE - 1) // ITEMS_PER_PAGE)
    filter_params = {
        k: v
        for k, v in [
            ("notification_type", notification_type),
            ("cluster_name", cluster_name),
            ("channel", channel),
        ]
        if v
    }
    context = {
        "notifications": rows,
        "page": page,
        "total_pages": total_pages,
        "has_prev": page > 1,
        "has_next": page < total_pages,
        "filter_types": filters["types"],
        "filter_clusters": filters["clusters"],
        "filter_channels": filters["channels"],
        "active_filters": filter_params,
        "filter_query_string": urlencode(filter_params),
    }
    return templates.TemplateResponse("notifications.html", common_context(request) | context)


@router.get("/notifications.csv")
def notifications_csv(
    notification_type: str | None = Query(None),
    cluster_name: str | None = Query(None),
    channel: str | None = Query(None),
) -> StreamingResponse:
    rows, _ = db.list_notification_logs(
        days=30,
        limit=100_000,
        offset=0,
        notification_type=notification_type or None,
        cluster_name=cluster_name or None,
        channel=channel or None,
    )
    buf = io.StringIO()
    writer = csv.writer(buf, delimiter=";", quoting=csv.QUOTE_ALL)
    writer.writerow(["created_at", "notification_type", "cluster_name", "channel", "title", "message"])
    for r in rows:
        writer.writerow(
            [
                r["created_at"].isoformat(),
                r["notification_type"],
                r["cluster_name"] or "",
                r["channel"],
                r["title"],
                r["message"],
            ]
        )
    buf.seek(0)
    return StreamingResponse(
        iter([buf.getvalue()]),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": 'attachment; filename="notifications.csv"'},
    )
