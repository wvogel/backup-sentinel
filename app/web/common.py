from __future__ import annotations

import json
from datetime import UTC, datetime

from fastapi import Request
from fastapi.templating import Jinja2Templates
from zoneinfo import ZoneInfo

from app.config import (
    APP_VERSION,
    BRAND_LOGO_DARK_URL,
    BRAND_LOGO_LIGHT_URL,
    CACHE_BUSTER,
    COPYRIGHT_TEXT,
    DEFAULT_TIMEZONE,
    FOOTER_LINKS,
    LOGOUT_URL,
)
from app.i18n import t, get_language, get_translations, SUPPORTED_LANGUAGES

templates = Jinja2Templates(directory="templates")
APP_TIMEZONE = ZoneInfo(DEFAULT_TIMEZONE)


def datetime_local(value: object, fmt: str = "%d.%m.%Y %H:%M") -> str:
    if value is None:
        return t("common.never")
    if isinstance(value, datetime):
        dt = value if value.tzinfo else value.replace(tzinfo=UTC)
        return dt.astimezone(APP_TIMEZONE).strftime(fmt)
    if hasattr(value, "strftime"):
        return value.strftime(fmt)
    return str(value)


def format_bytes(value: object) -> str:
    if value is None:
        return "–"
    try:
        v = int(value)
    except (TypeError, ValueError):
        return "–"
    if v >= 1_073_741_824:
        return f"{v / 1_073_741_824:,.0f} GB".replace(",", ".")
    if v >= 1_048_576:
        return f"{v / 1_048_576:,.0f} MB".replace(",", ".")
    return f"{v / 1024:,.0f} KB".replace(",", ".")


def format_number(value: object) -> str:
    try:
        return f"{int(value):,}".replace(",", ".")
    except (TypeError, ValueError):
        return str(value)


templates.env.filters["datetime_local"] = datetime_local
templates.env.filters["format_bytes"] = format_bytes
templates.env.filters["format_number"] = format_number

def _parse_footer_links(s: str) -> list[dict[str, str]]:
    if not s:
        return []
    links = []
    for part in s.split(","):
        if "|" in part:
            label, url = part.split("|", 1)
            links.append({"label": label.strip(), "url": url.strip()})
    return links


_footer_links = _parse_footer_links(FOOTER_LINKS)

templates.env.globals["t"] = t
templates.env.globals["get_language"] = get_language
templates.env.globals["get_translations_json"] = lambda: json.dumps(get_translations(), ensure_ascii=False)
templates.env.globals["supported_languages"] = SUPPORTED_LANGUAGES


def common_context(request: Request) -> dict:
    user_name = (
        request.headers.get("x-forwarded-email")
        or request.headers.get("x-auth-request-email")
        or request.headers.get("x-forwarded-user")
        or t("common.logged_in")
    )
    return {
        "request": request,
        "logout_url": LOGOUT_URL,
        "brand_logo_light_url": BRAND_LOGO_LIGHT_URL,
        "brand_logo_dark_url": BRAND_LOGO_DARK_URL,
        "current_user": user_name,
        "app_version": APP_VERSION,
        "footer_links": _footer_links,
        "copyright_text": COPYRIGHT_TEXT,
        "v": CACHE_BUSTER,
    }


def settings_actor(request: Request) -> str:
    return (
        request.headers.get("x-forwarded-email")
        or request.headers.get("x-auth-request-email")
        or request.headers.get("x-forwarded-user")
        or t("common.unknown_user")
    )


def mask_setting_value(key: str, value: str) -> str:
    if key in {"gotify_token", "email_password"}:
        return t("common.set") if value else t("common.empty")
    return value
