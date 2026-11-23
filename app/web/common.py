from __future__ import annotations

import hashlib
import logging
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

from fastapi import Request
from fastapi.templating import Jinja2Templates

from app import db
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
from app.services.human_format import (
    format_bitrate,
    format_count,
    format_rate,
    format_size,
    format_size_coarse,
)
from app.services.netdiag_format import format_run_log
from app.services.restore_probe import check_chips as restore_check_chips
from app.services.restore_probe import green_check_labels
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


def restore_status_tooltip(run: dict) -> str:
    """Full per-check breakdown for a run's status hover: the result word, then
    ONE line per probed IP with ICMP and each TCP port individually (✓/✗/–). So a
    'limited' run shows exactly what worked and what didn't, across every probed
    IP of a multi-homed guest — not just a bucket word. Rendered multi-line
    (the tooltip CSS uses white-space: pre-line)."""
    head = (run.get("result") or run.get("status") or "").strip()
    checks = run.get("checks")
    if not isinstance(checks, dict):
        return head
    if checks.get("error"):
        return f"{head}\n{checks['error']}".strip()
    lines: list[str] = []
    for ip, r in checks.items():
        if not isinstance(r, dict) or "icmp" not in r:
            continue
        icmp = "–" if r.get("icmp") is None else ("✓" if r.get("icmp") else "✗")
        parts = [f"ICMP {icmp}"]
        if "arp" in r:  # L2 liveness (answers even behind a guest firewall, e.g. Windows)
            parts.append(f"ARP {'✓' if r.get('arp') else '✗'}")
        for port, ok in (r.get("ports") or {}).items():
            parts.append(f":{port} {'✓' if ok else '✗'}")
        lines.append(f"{ip}: " + " · ".join(parts))
    return (head + "\n" + "\n".join(lines)).strip() if lines else head


def restore_eta_text(run: dict, now: datetime | None = None) -> str:
    """Estimated time remaining for a running restore as a short string
    (e.g. "~12 min"), or "" when it does not apply. Only during the data copy
    (``restoring`` phase) with real progress — the post-copy finalize/boot/probe
    have no meaningful byte estimate. Derived from elapsed × (100−%)/%, so it
    needs no throughput bookkeeping and shrinks on its own as the percent climbs.
    Returned without the "ETA" label, so the caller can place the label once."""
    if (run.get("phase") or "restoring") != "restoring":
        return ""
    pct = run.get("percent") or 0
    started = run.get("started_at")
    if not isinstance(started, datetime) or pct <= 0 or pct >= 100:
        return ""
    if started.tzinfo is None:
        started = started.replace(tzinfo=UTC)
    elapsed = ((now or datetime.now(UTC)) - started).total_seconds()
    if elapsed <= 0:
        return ""
    remaining = elapsed * (100 - pct) / pct
    eta = (now or datetime.now(UTC)) + timedelta(seconds=remaining)
    # A point in time like the running-backup tiles ("until 16:20"), with the
    # day once it is not today.
    local, today = eta.astimezone(APP_TIMEZONE), (now or datetime.now(UTC)).astimezone(APP_TIMEZONE).date()
    return t("cluster.running_until", time=local.strftime("%H:%M" if local.date() == today else "%d.%m. %H:%M"))


# Phases after the data copy: every disk is fully restored by then.
_POST_COPY_PHASES = ("finalizing", "network", "booting", "probing")


def restore_disk_fills(run: dict) -> dict[str, int] | None:
    """Per-disk fill (0–100) for the disk bar of a running restore, by disk key
    ("scsi0"), or None when the bar shows plain sizes. From the per-disk progress
    the worker records; a single disk falls back to the run's percent (older runs,
    or before the first disk line)."""
    if run.get("status") != "running":
        return None
    disks = run.get("disks") or []
    if run.get("phase") in _POST_COPY_PHASES:
        return {d["dev"]: 100 for d in disks}
    fills = {k: int(v) for k, v in (run.get("disk_progress") or {}).items()}
    if not fills and len(disks) == 1:
        fills = {disks[0]["dev"]: int(run.get("percent") or 0)}
    return {d["dev"]: fills.get(d["dev"], 0) for d in disks} if disks else fills


def restore_probe_countdown(run: dict, now: datetime | None = None) -> dict | None:
    """Live countdown of the reachability window while a run boots/probes:
    ``{"remaining", "total", "text", "pct"}`` (pct = share already elapsed), or
    None when no window is open. ``text`` is m:ss (h:mm:ss from an hour)."""
    if run.get("status") != "running" or run.get("phase") not in ("booting", "probing"):
        return None
    began, ends = run.get("probe_started_at"), run.get("probe_ends_at")
    if not isinstance(began, datetime) or not isinstance(ends, datetime):
        return None
    total = (ends - began).total_seconds()
    if total <= 0:
        return None
    remaining = max(0, int((ends - (now or datetime.now(UTC))).total_seconds()))
    h, rest = divmod(remaining, 3600)
    text = f"{h}:{rest // 60:02d}:{rest % 60:02d}" if h else f"{rest // 60}:{rest % 60:02d}"
    return {
        "remaining": remaining,
        "total": int(total),
        "text": text,
        "pct": round(min(100.0, max(0.0, 100 * (1 - remaining / total))), 1),
    }


# Pipeline order of the phases a run passes through, for the per-phase timing
# breakdown. Unknown keys (future phases, or a resumed run's partial set) are
# appended after these, in input order, so nothing is silently dropped.
_PHASE_ORDER = ("setup", "preflight", "helper", "restoring", "network", "booting", "probing")


def _fmt_phase_secs(seconds: float) -> str:
    """A phase duration at second granularity (``format_duration`` rounds to whole
    minutes, which hides the preflight/helper/boot/probe phases): ``"45 s"`` below
    a minute, ``"M:SS"`` below an hour, ``"H:MM:SS"`` above."""
    s = round(seconds)
    if s < 60:
        return f"{s} s"
    if s < 3600:
        return f"{s // 60}:{s % 60:02d}"
    return f"{s // 3600}:{(s % 3600) // 60:02d}:{s % 60:02d}"


def restore_phase_timings(run: dict) -> list[dict]:
    """Per-phase wall-clock breakdown for a finished run, from its stored
    ``phase_timings`` ({phase: seconds}). Ordered along the pipeline; each entry is
    ``{"phase", "seconds", "text"}``, and the data-copy phase also carries a
    ``"rate_text"`` (bytes copied ÷ its seconds) so a slow copy is obvious at a
    glance. Empty when no timings were recorded (e.g. a run resumed after a
    restart). The template translates the phase key to a label."""
    pt = run.get("phase_timings")
    if not isinstance(pt, dict) or not pt:
        return []
    copied = run.get("bytes_done") or run.get("disk_bytes")
    known = [p for p in _PHASE_ORDER if p in pt]
    extra = [p for p in pt if p not in _PHASE_ORDER]
    out: list[dict] = []
    for phase in known + extra:
        secs = pt.get(phase)
        if secs is None:  # booting without any green check: how long is unknown
            out.append({"phase": phase, "seconds": None, "text": "–"})
            continue
        if not isinstance(secs, (int, float)) or isinstance(secs, bool):
            continue
        item = {"phase": phase, "seconds": round(secs), "text": _fmt_phase_secs(secs)}
        if phase == "restoring" and copied and secs > 0:
            item["rate_text"] = format_rate(copied / secs)
        out.append(item)
    return out


templates.env.filters["datetime_local"] = datetime_local
templates.env.filters["restore_status_tooltip"] = restore_status_tooltip
templates.env.filters["restore_eta"] = restore_eta_text
templates.env.filters["disk_fills"] = restore_disk_fills
templates.env.filters["run_log"] = format_run_log
templates.env.filters["restore_check_chips"] = restore_check_chips
templates.env.filters["format_bytes"] = format_size
templates.env.filters["size_coarse"] = format_size_coarse
templates.env.filters["bitrate"] = format_bitrate
templates.env.filters["green_checks"] = green_check_labels
templates.env.filters["restore_phase_timings"] = restore_phase_timings
templates.env.filters["probe_countdown"] = restore_probe_countdown
templates.env.filters["spark_tooltip"] = spark_tooltip
templates.env.filters["format_number"] = format_count


def vol_seg_class(storage: object) -> str:
    """Deterministic colour slot (vol-s0..vol-s5) for a storage name, so the same
    storage keeps its colour across the segmented volume bars and legends."""
    name = str(storage or "")
    return f"vol-s{sum(name.encode('utf-8')) % 6}"


templates.env.filters["vol_seg_class"] = vol_seg_class


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


def runs_filter_href(filt: dict, key: str, value: str, per: int | None = None) -> str:
    """URL for the restore runs log with ``key`` set to ``value`` (empty clears
    it) and all OTHER active filters preserved — for the per-column (Excel-style)
    header filter menus. Page is dropped, so a filter change returns to page 1;
    the chosen page size (``per``) is kept when it is not the default."""
    from urllib.parse import urlencode

    params = {k: v for k, v in (filt or {}).items() if v and k != key}
    if value:
        params[key] = value
    if per and per != 25:
        params["per"] = per
    qs = urlencode(params)
    return "/restore?" + qs if qs else "/restore"


templates.env.globals["t"] = t
templates.env.globals["runs_filter_href"] = runs_filter_href
templates.env.globals["pve_sync_state"] = lambda cluster: pve_sync_state(
    cluster, datetime.now(UTC), SYNC_INTERVAL_MINUTES
)
templates.env.globals["pbs_sync_state"] = lambda conn: pbs_sync_state(conn, datetime.now(UTC), SYNC_INTERVAL_MINUTES)
templates.env.globals["get_language"] = get_language
# Language and translations for the scripts, rendered as a JSON data block (base.html).
templates.env.globals["i18n_payload"] = lambda: {
    "lang": get_language(),
    "languages": SUPPORTED_LANGUAGES,
    "translations": get_translations(),
}
templates.env.globals["supported_languages"] = SUPPORTED_LANGUAGES


logger = logging.getLogger(__name__)


# The report preview and the weekly-mail preview are standalone documents made
# for PDF and mail clients, which need their styles inline. They carry no
# script; this policy allows them nothing but styles, images and fonts.
PREVIEW_CSP = (
    "default-src 'none'; style-src 'unsafe-inline'; img-src 'self' data: https:; font-src 'self' data:; "
    "frame-ancestors 'none'; base-uri 'none'; form-action 'none'"
)


def theme_css_version() -> str:
    """Changes with the theme CSS (cache key of /theme.css); '' when there is none."""
    css = theme_css()
    return hashlib.sha256(css.encode()).hexdigest()[:12] if css else ""


def theme_css() -> str:
    """CSS for a non-default theme (served as /theme.css); '' when the stylesheet defaults apply."""
    try:
        preset, base = active_theme()
        css = ""
        if not (preset is theme.PRESETS[theme.DEFAULT_PRESET] and base == preset.base):
            css = theme.render_css(theme.derive_tokens(preset, base))  # else style.css already is it
        custom = theme.normalize_custom_css(db.get_settings_dict("theme_custom_css").get("theme_custom_css"))
        return css + ("\n" + custom if custom else "")
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


def _restore_queue_state() -> dict | None:
    """Top-bar restore-icon state: down/red (last connection test failed),
    paused/yellow, running/animated (queue busy), else idle/green. None when no
    restore server is configured (icon keeps its normal colour)."""
    try:
        import json as _json

        from app import db
        from app.db_restore import queue_state
        from app.services import restore_config

        if not restore_config.is_configured():
            return None
        # No connection? Use the last stored connection-test result (cheap; a live
        # probe on every page render would be far too costly).
        raw = db.get_settings_dict().get("restore_server.last_test")
        if raw:
            try:
                lt = _json.loads(raw)
                if lt.get("configured") and lt.get("ok") is False:
                    return {"cls": "down"}
            except (ValueError, TypeError):
                pass
        if restore_config.queue_paused():
            return {"cls": "paused"}
        return {"cls": "running" if queue_state() in ("running", "queued") else "idle"}
    except Exception:
        return None


def common_context(request: Request) -> dict:
    return {
        "request": request,
        "restore_queue": _restore_queue_state(),
        "logout_url": LOGOUT_URL,
        "brand_logo_light_url": BRAND_LOGO_LIGHT_URL,
        "brand_logo_dark_url": BRAND_LOGO_DARK_URL,
        "current_user": _forwarded_user(request) or t("common.logged_in"),
        "role": getattr(request.state, "role", "admin"),
        "app_version": APP_VERSION,
        "footer_links": _footer_links,
        "copyright_text": COPYRIGHT_TEXT,
        "v": CACHE_BUSTER,
        "theme_css_version": theme_css_version(),
    }


def settings_actor(request: Request) -> str:
    return _forwarded_user(request) or t("common.unknown_user")


def mask_setting_value(key: str, value: str) -> str:
    if key in {"gotify_token", "email_password"}:
        return t("common.set") if value else t("common.empty")
    return value
