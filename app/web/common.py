from __future__ import annotations

import json
import logging
from datetime import UTC, datetime
from zoneinfo import ZoneInfo

from fastapi import Request
from fastapi.templating import Jinja2Templates

from app.config import (
    APP_VERSION,
    BRAND_LOGO_DARK_URL,
    BRAND_LOGO_LIGHT_URL,
    CACHE_BUSTER,
    COPYRIGHT_TEXT,
    DEFAULT_TIMEZONE,
    FOOTER_LINKS,
    LOGOUT_URL,
    SYNC_INTERVAL_MINUTES,
)
from app.i18n import SUPPORTED_LANGUAGES, get_language, get_translations, t
from app.services import theme
from app.services.backup_status import format_duration, pbs_sync_state, pve_sync_state
from app.services.human_format import format_count, format_rate, format_size
from app.services.theme_store import active_theme

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


def spark_tooltip(day: dict) -> str:
    """One line per bar: "08.10.: ok · 42 GiB · 5 min · ⌀ 44 MiB/s · 90 % reused"."""
    parts = [f"{day['date'].strftime('%d.%m.')}: {t('spark.' + day['status'])}"]
    if day.get("size_bytes"):
        parts.append(format_size(day["size_bytes"]))
    if day.get("duration_seconds"):
        parts.append(format_duration(day["duration_seconds"]))
    if day.get("throughput_bps"):
        parts.append(f"⌀ {format_rate(day['throughput_bps'])}")
    if day.get("full_backup"):
        parts.append(t("spark.full"))
    elif day.get("reused_pct") is not None:
        parts.append(t("spark.reused", pct=day["reused_pct"]))
    return " · ".join(parts)


templates.env.filters["datetime_local"] = datetime_local
templates.env.filters["format_bytes"] = format_size
templates.env.filters["spark_tooltip"] = spark_tooltip
templates.env.filters["format_number"] = format_count


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
templates.env.globals["pve_sync_state"] = lambda cluster: pve_sync_state(
    cluster, datetime.now(UTC), SYNC_INTERVAL_MINUTES
)
templates.env.globals["pbs_sync_state"] = lambda conn: pbs_sync_state(conn, datetime.now(UTC), SYNC_INTERVAL_MINUTES)
templates.env.globals["get_language"] = get_language
templates.env.globals["get_translations_json"] = lambda: json.dumps(get_translations(), ensure_ascii=False)
templates.env.globals["supported_languages"] = SUPPORTED_LANGUAGES


logger = logging.getLogger(__name__)


def theme_css() -> str:
    """Inline CSS for a non-default theme; '' when the stylesheet defaults apply."""
    try:
        preset, base = active_theme()
        if preset is theme.PRESETS[theme.DEFAULT_PRESET] and base == preset.base:
            return ""  # style.css already is the default theme
        return theme.render_css(theme.derive_tokens(preset, base))
    except Exception as exc:
        logger.warning("Theme rendering failed, using stylesheet defaults: %s", exc)
        return ""


def _forwarded_user(request: Request) -> str:
    """The signed-in user as oauth2-proxy passes it on; empty without a proxy."""
    return (
        request.headers.get("x-forwarded-email")
        or request.headers.get("x-auth-request-email")
        or request.headers.get("x-forwarded-user")
        or ""
    )


def common_context(request: Request) -> dict:
    return {
        "request": request,
        "logout_url": LOGOUT_URL,
        "brand_logo_light_url": BRAND_LOGO_LIGHT_URL,
        "brand_logo_dark_url": BRAND_LOGO_DARK_URL,
        "current_user": _forwarded_user(request) or t("common.logged_in"),
        "app_version": APP_VERSION,
        "footer_links": _footer_links,
        "copyright_text": COPYRIGHT_TEXT,
        "v": CACHE_BUSTER,
        "theme_css": theme_css(),
    }


def settings_actor(request: Request) -> str:
    return _forwarded_user(request) or t("common.unknown_user")


def mask_setting_value(key: str, value: str) -> str:
    if key in {"gotify_token", "email_password"}:
        return t("common.set") if value else t("common.empty")
    return value
