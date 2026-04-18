from __future__ import annotations

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from starlette import status

from app import db
from app.config import APP_BASE_URL
from app.services.crypto import encrypt
from app.services.notifications import test_email_delivery, test_gotify_notification
from app.web.common import common_context, mask_setting_value, settings_actor, templates

router = APIRouter()


@router.get("/settings", response_class=HTMLResponse)
def settings(request: Request) -> HTMLResponse:
    clusters = sorted(db.list_clusters(), key=lambda c: c["name"].lower())
    for cluster in clusters:
        cluster["pbs_connections"] = db.list_pbs_connections(cluster["id"])
    all_settings = db.get_settings_dict()
    all_settings["gotify_token"] = ""
    all_settings["email_password"] = ""
    context = {
        "clusters": clusters,
        "app_base_url": APP_BASE_URL.rstrip("/"),
        "app_settings": all_settings,
        "settings_audit": db.list_settings_audit_entries(50),
    }
    return templates.TemplateResponse(request, "settings.html", common_context(request) | context)


@router.post("/settings/notifications")
def save_notification_settings(
    request: Request,
    gotify_url: str = Form(default=""),
    gotify_token: str = Form(default=""),
    notify_gotify_enabled: str = Form(default="false"),
    email_host: str = Form(default=""),
    email_port: str = Form(default="465"),
    email_user: str = Form(default=""),
    email_password: str = Form(default=""),
    email_tls: str = Form(default="ssl"),
    email_from: str = Form(default=""),
    email_recipients: str = Form(default=""),
    notify_email_enabled: str = Form(default="false"),
    notify_quiet_enabled: str = Form(default="false"),
    notify_quiet_start: str = Form(default="22:00"),
    notify_quiet_end: str = Form(default="07:00"),
) -> RedirectResponse:
    existing = db.get_settings_dict()
    gotify_token_value = gotify_token.strip()
    email_password_value = email_password.strip()
    new_settings = {
        "gotify_url": gotify_url.strip().rstrip("/"),
        "gotify_token": encrypt(gotify_token_value) if gotify_token_value else existing.get("gotify_token", ""),
        "notify_gotify_enabled": "true" if notify_gotify_enabled == "true" else "false",
        "email_host": email_host.strip(),
        "email_port": email_port.strip(),
        "email_user": email_user.strip(),
        "email_password": encrypt(email_password_value) if email_password_value else existing.get("email_password", ""),
        "email_tls": email_tls.strip(),
        "email_from": email_from.strip(),
        "email_recipients": email_recipients.strip(),
        "notify_email_enabled": "true" if notify_email_enabled == "true" else "false",
        "notify_quiet_enabled": "true" if notify_quiet_enabled == "true" else "false",
        "notify_quiet_start": notify_quiet_start.strip(),
        "notify_quiet_end": notify_quiet_end.strip(),
    }
    audit_entries: list[dict[str, str]] = []
    actor = settings_actor(request)
    for key, new_value in new_settings.items():
        old_value = existing.get(key, "")
        if old_value == new_value:
            continue
        audit_entries.append(
            {
                "actor": actor,
                "key": key,
                "old_value": mask_setting_value(key, old_value),
                "new_value": mask_setting_value(key, new_value),
            }
        )
    db.save_settings(new_settings)
    db.append_settings_audit_entries(audit_entries)
    return RedirectResponse(url="/settings#benachrichtigungen", status_code=status.HTTP_303_SEE_OTHER)


@router.post("/settings/notifications/toggle")
async def toggle_notification_setting(request: Request) -> JSONResponse:
    body = await request.json()
    key = body.get("key", "")
    value = body.get("value", "")
    allowed_keys = {"notify_gotify_enabled", "notify_email_enabled", "notify_quiet_enabled"}
    if key not in allowed_keys:
        return JSONResponse({"ok": False, "detail": "Invalid key"}, status_code=400)
    clean_value = "true" if value is True or value == "true" else "false"
    existing = db.get_settings_dict()
    old_value = existing.get(key, "")
    if old_value != clean_value:
        db.save_settings({key: clean_value})
        db.append_settings_audit_entries(
            [
                {
                    "actor": settings_actor(request),
                    "key": key,
                    "old_value": old_value,
                    "new_value": clean_value,
                }
            ]
        )
    return JSONResponse({"ok": True, "key": key, "value": clean_value})


@router.post("/settings/notifications/test-gotify")
def test_gotify() -> JSONResponse:
    ok, detail = test_gotify_notification()
    return JSONResponse({"ok": ok, "detail": detail}, status_code=200 if ok else 503)


@router.post("/settings/notifications/test-email")
def test_email() -> JSONResponse:
    ok, detail = test_email_delivery()
    return JSONResponse({"ok": ok, "detail": detail}, status_code=200 if ok else 503)
