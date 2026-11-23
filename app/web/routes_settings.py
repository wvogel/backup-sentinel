from __future__ import annotations

import json
from dataclasses import asdict

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from starlette import status

from app import db
from app.config import APP_BASE_URL
from app.services import theme, tls_trust
from app.services.crypto import encrypt
from app.services.notifications import test_email_delivery, test_gotify_notification
from app.services.theme_store import active_theme
from app.web.common import common_context, mask_setting_value, settings_actor, templates

router = APIRouter()


@router.get("/settings", response_class=HTMLResponse)
def settings(request: Request) -> HTMLResponse:
    clusters = sorted(db.list_clusters(), key=lambda c: c["name"].lower())
    for cluster in clusters:
        cluster["pbs_connections"] = db.list_pbs_connections(cluster["id"])
        cluster["enrollment_active"] = db.enrollment_active(cluster)
    all_settings = db.get_settings_dict()
    all_settings["gotify_token"] = ""
    all_settings["email_password"] = ""
    from app.models import DISK_CLASSES
    from app.services import restore_config, restore_orchestrator

    _rs = restore_orchestrator.ui_target_storages() if restore_config.is_configured() else {"rows": [], "error": None}
    context = {
        "clusters": clusters,
        "app_base_url": APP_BASE_URL.rstrip("/"),
        "app_settings": all_settings,
        "settings_audit": db.list_settings_audit_entries(50),
        "audit_chain": db.verify_audit_log(),
        "tls_pins": db.list_tls_pins(),
        "tls_enforced": tls_trust.ENFORCE,
        "enroll_open": int(request.query_params.get("enroll") or 0)
        if (request.query_params.get("enroll") or "").isdigit()
        else 0,
        "rc": restore_config.public_config(),
        "restore_storages": _rs.get("rows", []),
        "restore_storage_error": _rs.get("error"),
        "restore_storage_note": _rs.get("note"),
        "restore_fio_available": _rs.get("fio_available", False),
        "restore_perf_running": _rs.get("perf_running", ""),
        "restore_perf_progress": _rs.get("perf_progress") or {},
        "disk_classes": DISK_CLASSES,
        "restore_test": _load_restore_test(all_settings),
        "restore_saved": request.query_params.get("restore") == "saved",
        "restore_error": (
            all_settings.get("restore_server.last_error") or "Invalid configuration"
            if request.query_params.get("restore_error")
            else ""
        ),
        **_appearance_context(request),
    }
    return templates.TemplateResponse(request, "settings.html", common_context(request) | context)


@router.post("/settings/tls/accept")
def tls_accept(request: Request, host: str = Form(...), port: int = Form(...)) -> RedirectResponse:
    """Pin the certificate the host presented last (admin only, app.web.auth)."""
    accepted = db.accept_tls_change(host, port)
    if accepted:
        tls_trust.forget(host, port)
        db.append_audit_event(settings_actor(request), "tls.accepted", f"{host}:{port}", accepted)
    return RedirectResponse(url="/settings#certificates", status_code=status.HTTP_303_SEE_OTHER)


@router.post("/settings/tls/dismiss")
def tls_dismiss(request: Request, host: str = Form(...), port: int = Form(...)) -> RedirectResponse:
    """Drop the report of a changed certificate; the pinned one stays."""
    if db.dismiss_tls_change(host, port):
        tls_trust.forget(host, port)
        db.append_audit_event(settings_actor(request), "tls.dismissed", f"{host}:{port}")
    return RedirectResponse(url="/settings#certificates", status_code=status.HTTP_303_SEE_OTHER)


def _load_restore_test(all_settings: dict[str, str]) -> dict | None:
    raw = all_settings.get("restore_server.last_test")
    if not raw:
        return None
    try:
        return json.loads(raw)
    except (ValueError, TypeError):
        return None


def save_appearance(form: dict[str, str], actor: str) -> tuple[list[str], list[theme.ContrastWarning]]:
    """Validate and store the theme settings.

    Returns (invalid field names, contrast warnings). Nothing is saved when a
    field is invalid; contrast problems only warn. Colours equal to the
    preset are stored empty so the stylesheet defaults keep applying.
    """
    preset_name = (form.get("preset") or "").strip()
    if preset_name not in theme.PRESETS:
        return ["preset"], []
    preset = theme.PRESETS[preset_name]
    reset = bool(form.get("reset"))
    errors: list[str] = []
    values: dict[str, str] = {}
    for key in theme.BASE_KEYS:
        raw = "" if reset else (form.get(key) or "").strip()
        normalized = theme.normalize_hex(raw) if raw else ""
        if normalized is None:
            errors.append(key)
            continue
        values[key] = "" if normalized == getattr(preset.base, key) else normalized
    custom_css = theme.normalize_custom_css(form.get("custom_css"))
    if custom_css is None:
        errors.append("custom_css")
    if errors:
        return errors, []

    new_settings = {
        "theme_preset": preset_name,
        **{f"theme_{key}": value for key, value in values.items()},
        "theme_custom_css": custom_css,
    }
    existing = db.get_settings_dict("theme_")
    audit_entries = [
        {"actor": actor, "key": key, "old_value": existing.get(key, ""), "new_value": value}
        for key, value in new_settings.items()
        if existing.get(key, "") != value
    ]
    db.save_settings(new_settings)
    db.append_settings_audit_entries(audit_entries)
    _, base = theme.resolve(preset_name, values)
    return [], theme.check_contrast(base)


@router.post("/settings/appearance")
def update_appearance(
    request: Request,
    preset: str = Form(default=""),
    primary: str = Form(default=""),
    accent: str = Form(default=""),
    background: str = Form(default=""),
    running: str = Form(default=""),
    custom_css: str = Form(default=""),
    reset: str = Form(default=""),
) -> RedirectResponse:
    # Plain `def`: FastAPI runs it in the threadpool, so the DB calls don't block the event loop.
    form = {
        "preset": preset,
        "primary": primary,
        "accent": accent,
        "background": background,
        "running": running,
        "custom_css": custom_css,
        "reset": reset,
    }
    errors, warnings = save_appearance(form, settings_actor(request))
    if errors:
        query = "appearance_error=" + ",".join(errors)
    else:
        query = "appearance=saved"
        if warnings:
            query += "&warn=" + ",".join(f"{w.key}:{w.ratio}:{w.minimum}" for w in warnings)
    return RedirectResponse(url=f"/settings?{query}#appearance", status_code=status.HTTP_303_SEE_OTHER)


def _appearance_context(request: Request) -> dict:
    preset, base = active_theme()
    preset_name = next((name for name, p in theme.PRESETS.items() if p == preset), theme.DEFAULT_PRESET)
    warnings = []
    for item in (request.query_params.get("warn") or "").split(","):
        # The values come from the URL: accept only numbers so a crafted link
        # can't put arbitrary text into a warning.
        parts = item.split(":")
        if len(parts) != 3 or parts[0] not in theme.BASE_KEYS:
            continue
        try:
            ratio, minimum = float(parts[1]), float(parts[2])
        except ValueError:
            continue
        warnings.append({"key": parts[0], "ratio": f"{ratio:g}", "minimum": f"{minimum:g}"})
    errors = [e for e in (request.query_params.get("appearance_error") or "").split(",") if e]
    return {
        "theme_preset_name": preset_name,
        "theme_base": base,
        "theme_presets": list(theme.PRESETS),
        "theme_presets_json": json.dumps({name: asdict(p.base) for name, p in theme.PRESETS.items()}),
        "theme_keys": theme.BASE_KEYS,
        "theme_custom_css": db.get_settings_dict("theme_custom_css").get("theme_custom_css", ""),
        "appearance_saved": request.query_params.get("appearance") == "saved",
        "appearance_warnings": warnings,
        "appearance_errors": errors,
    }


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
    notify_weekly_report: str = Form(default="false"),
    notify_restore_overdue: str = Form(default="false"),
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
        "notify_weekly_report": "true" if notify_weekly_report == "true" else "false",
        "notify_restore_overdue": "true" if notify_restore_overdue == "true" else "false",
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


@router.get("/settings/notifications/weekly-preview", response_class=HTMLResponse)
def weekly_report_preview() -> HTMLResponse:
    """The weekly report email as it would be sent now."""
    from app.web.sync import weekly_report_email

    _subject, _text, html, _pngs = weekly_report_email(inline_images=False)
    return HTMLResponse(html)
