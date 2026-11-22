"""Render the NIS2 report (nis2_report.build_nis2_report) to HTML and PDF.

One Jinja template (templates/report_pdf/report.html) serves the PDF
(WeasyPrint) and the browser preview. Charts are inline SVG built here — no JS,
no chart library. Colours follow the active UI theme.
"""

from __future__ import annotations

import base64
import logging
import math
import threading
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib import request as urllib_request
from zoneinfo import ZoneInfo

from jinja2 import Environment, FileSystemLoader, select_autoescape
from markupsafe import Markup, escape

from app.config import APP_VERSION, BASE_DIR, BRAND_FONT_URL, BRAND_LOGO_LIGHT_URL
from app.i18n import tl
from app.services import nis2_metrics as m
from app.services.theme import DEFAULT_PRESET, PRESETS, BaseColors, best_ink

logger = logging.getLogger(__name__)

TEMPLATE_DIR = BASE_DIR / "templates" / "report_pdf"
FONT_WEIGHTS = {400: "Regular", 500: "Medium", 600: "SemiBold", 700: "Bold"}


@dataclass(frozen=True)
class Palette:
    primary: str  # band, headings, ink
    rule: str  # accent rule under the band, the restore trend
    label: str  # small label in the band (theme accent where readable)
    band_ink: str  # text on the band
    ok: str = "#2f8f6b"
    warn: str = "#c48a1c"
    critical: str = "#c2333f"
    muted: str = "#6b6b85"
    sand: str = "#efe7db"
    paper: str = "#f8f5f2"
    sage: str = "#d8ddce"
    ok_soft: str = "#dff1e9"
    warn_soft: str = "#f7ebd2"
    critical_soft: str = "#f7dadd"
    cal_ok: str = "#9fd0b9"

    def tone(self, tone: str | None) -> str:
        return {"ok": self.ok, "warn": self.warn, "critical": self.critical}.get(tone or "", self.sand)

    def soft(self, tone: str | None) -> str:
        return {"ok": self.ok_soft, "warn": self.warn_soft, "critical": self.critical_soft}.get(tone or "", self.paper)


def palette_for(base: BaseColors | None) -> Palette:
    base = base or PRESETS[DEFAULT_PRESET].base
    return Palette(
        primary=base.primary,
        rule=base.running,
        label=best_ink(base.primary, base.accent, "#f6f7ff"),
        band_ink=best_ink(base.primary, "#ffffff"),
    )


# ── Remote assets (logo, font): fetched once, embedded as data URIs ─────────
_asset_cache: dict[str, str | None] = {}
_asset_lock = threading.Lock()


def _data_uri(url: str) -> str | None:
    if not url:
        return None
    with _asset_lock:
        if url in _asset_cache:
            return _asset_cache[url]
    uri = None
    try:
        req = urllib_request.Request(url, headers={"Accept": "*/*", "User-Agent": "backup-sentinel"})
        with urllib_request.urlopen(req, timeout=5) as resp:
            ctype = resp.headers.get_content_type() or "application/octet-stream"
            if url.lower().endswith(".woff2"):
                ctype = "font/woff2"
            elif url.lower().endswith(".svg"):
                ctype = "image/svg+xml"
            uri = f"data:{ctype};base64,{base64.b64encode(resp.read()).decode()}"
    except Exception as exc:  # offline, 404 … — the report falls back
        logger.warning("Report asset not available (%s): %s", url, exc)
    with _asset_lock:
        _asset_cache[url] = uri
    return uri


def _font_faces() -> list[dict[str, Any]]:
    """@font-face sources of the brand font (BSENTINEL_BRAND_FONT_URL with {weight})."""
    if not BRAND_FONT_URL or "{weight}" not in BRAND_FONT_URL:
        return []
    faces = []
    for weight, name in FONT_WEIGHTS.items():
        uri = _data_uri(BRAND_FONT_URL.replace("{weight}", name))
        if uri:
            faces.append({"weight": weight, "src": uri})
    return faces


# ── SVG charts ──────────────────────────────────────────────────────────────
def _fmt_tick(v: float) -> str:
    return str(int(v)) if float(v).is_integer() else f"{v:g}"


def ring_svg(pct: int | None, tone: str | None, pal: Palette, size: int = 100, stroke: int = 10) -> Markup:
    r = (size - stroke) / 2
    c = 2 * math.pi * r
    dash = c * (pct or 0) / 100
    label = "–" if pct is None else f"{pct}%"
    return Markup(
        f'<svg class="ring" width="{size}" height="{size}" viewBox="0 0 {size} {size}">'
        f'<circle cx="{size / 2}" cy="{size / 2}" r="{r}" fill="none" stroke="{pal.sand}" stroke-width="{stroke}"/>'
        f'<circle cx="{size / 2}" cy="{size / 2}" r="{r}" fill="none" stroke="{pal.tone(tone)}" stroke-width="{stroke}"'
        f' stroke-linecap="round" stroke-dasharray="{dash:.2f} {c:.2f}" transform="rotate(-90 {size / 2} {size / 2})"/>'
        f'<text x="{size / 2}" y="{size / 2 + size * 0.06:.1f}" text-anchor="middle" font-size="{size * 0.19:.0f}"'
        f' font-weight="700" fill="{pal.primary}">{label}</text></svg>'
    )


def trend_svg(
    values: list[int | None],
    labels: list[str],
    lo: int,
    hi: int,
    target: int,
    color: str,
    pal: Palette,
    w: int = 300,
    h: int = 96,
) -> Markup:
    """Area/line chart of whole percents; None is a hole ("keine Daten")."""
    pl, pr, pt, pb = 26, 6, 8, 16
    iw, ih = w - pl - pr, h - pt - pb
    step = iw / max(1, len(values))

    def y(v: float) -> float:
        v = min(max(v, lo), hi)
        return pt + ih - ih * (v - lo) / (hi - lo)

    out = [f'<svg class="trend" width="{w}" height="{h}" viewBox="0 0 {w} {h}" font-size="8" fill="{pal.muted}">']
    for g in (lo, hi):
        out.append(
            f'<line x1="{pl}" x2="{w - pr}" y1="{y(g):.1f}" y2="{y(g):.1f}" stroke="{pal.sage}" stroke-width=".6"/>'
        )
        out.append(f'<text x="{pl - 5}" y="{y(g) + 3:.1f}" text-anchor="end">{_fmt_tick(g)}</text>')
    out.append(
        f'<line x1="{pl}" x2="{w - pr}" y1="{y(target):.1f}" y2="{y(target):.1f}" stroke="{color}"'
        f' stroke-width=".9" stroke-dasharray="3 2"/>'
    )
    # Contiguous runs of known values: one area + line each.
    runs: list[list[tuple[float, float]]] = []
    current: list[tuple[float, float]] = []
    for i, v in enumerate(values):
        if v is None:
            if current:
                runs.append(current)
            current = []
            continue
        current.append((pl + i * step + step / 2, y(v)))
    if current:
        runs.append(current)
    for run in runs:
        base = pt + ih
        if len(run) > 1:
            area = (
                f"{run[0][0]:.1f},{base} " + " ".join(f"{a:.1f},{b:.1f}" for a, b in run) + f" {run[-1][0]:.1f},{base}"
            )
            out.append(f'<polygon points="{area}" fill="{color}" fill-opacity=".12"/>')
            pts = " ".join(f"{a:.1f},{b:.1f}" for a, b in run)
            out.append(
                f'<polyline points="{pts}" fill="none" stroke="{color}" stroke-width="1.8" stroke-linejoin="round"/>'
            )
        else:
            out.append(f'<circle cx="{run[0][0]:.1f}" cy="{run[0][1]:.1f}" r="2" fill="{color}"/>')
    if values and values[-1] is not None:
        out.append(
            f'<circle cx="{pl + (len(values) - 1) * step + step / 2:.1f}" cy="{y(values[-1]):.1f}" r="3" fill="{color}"/>'
        )
    for i, lab in enumerate(labels):
        out.append(f'<text x="{pl + i * step + step / 2:.1f}" y="{h - 4}" text-anchor="middle">{escape(lab)}</text>')
    out.append("</svg>")
    return Markup("".join(out))


def donut_svg(parts: list[tuple[int, str]], pal: Palette, size: int = 92, stroke: int = 15) -> Markup:
    total = sum(v for v, _ in parts)
    r = (size - stroke) / 2
    c = 2 * math.pi * r
    out = [f'<svg class="donut" width="{size}" height="{size}" viewBox="0 0 {size} {size}">']
    out.append(
        f'<circle cx="{size / 2}" cy="{size / 2}" r="{r}" fill="none" stroke="{pal.sand}" stroke-width="{stroke}"/>'
    )
    offset = 0.0
    for value, color in parts:
        if not value or not total:
            continue
        length = c * value / total
        out.append(
            f'<circle cx="{size / 2}" cy="{size / 2}" r="{r}" fill="none" stroke="{color}" stroke-width="{stroke}"'
            f' stroke-dasharray="{length:.2f} {c:.2f}" stroke-dashoffset="{-offset:.2f}"'
            f' transform="rotate(-90 {size / 2} {size / 2})"/>'
        )
        offset += length
    out.append(
        f'<text x="{size / 2}" y="{size / 2 + 6}" text-anchor="middle" font-size="17" font-weight="700"'
        f' fill="{pal.primary}">{total}</text></svg>'
    )
    return Markup("".join(out))


def _wordmark_png() -> str | None:
    """The header wordmark as in the emails (Impact is missing on servers)."""
    path = BASE_DIR / "static" / "email" / "wordmark.png"
    try:
        return "data:image/png;base64," + base64.b64encode(path.read_bytes()).decode()
    except OSError:
        return None


# ── Template ────────────────────────────────────────────────────────────────
_env = Environment(loader=FileSystemLoader(str(TEMPLATE_DIR)), autoescape=select_autoescape(["html"]))


def _month_label(month, lang: str) -> str:
    return tl(lang, f"nis2.month_{month.month}") + f" {month.year}"


def _short_month(month, lang: str) -> str:
    return tl(lang, f"nis2.month_short_{month.month}")


def _date(value, tz: str, lang: str, with_year: bool = True) -> str:
    if isinstance(value, datetime):
        value = value.astimezone(ZoneInfo(tz))
    if lang == "en":
        return value.strftime("%Y-%m-%d" if with_year else "%m-%d")
    return value.strftime("%d.%m.%Y" if with_year else "%d.%m.")


def render_html(report: dict[str, Any], *, theme_colors: BaseColors | None = None, preview: bool = False) -> str:
    lang = report["lang"]
    pal = palette_for(theme_colors)
    period = report["period"]
    gen_local = report["generated_at"].astimezone(ZoneInfo(report["tz"]))
    start_label = _date(period["start"], report["tz"], lang, with_year=False)
    end_label = _date(period["reference_date"], report["tz"], lang)

    def tr(key: str, **kw: Any) -> str:
        return tl(lang, f"nis2.{key}", **kw)

    ctx = {
        "r": report,
        "pal": pal,
        "tr": tr,
        "lang": lang,
        "preview": preview,
        "version": APP_VERSION,
        "fonts": _font_faces(),
        "logo": _data_uri(BRAND_LOGO_LIGHT_URL),
        "ring": lambda pct, tone, size=100: ring_svg(pct, tone, pal, size),
        "donut": lambda parts: donut_svg(parts, pal),
        "wordmark_png": _wordmark_png(),
        "trend_labels": [_short_month(mo, lang) for mo in report["trend_months"]],
        # The trends take the room the cluster table leaves (≈7 mm per extra cluster).
        "trend": lambda vals, lo, hi, target, color: trend_svg(
            vals,
            [_short_month(mo, lang) for mo in report["trend_months"]],
            lo,
            hi,
            target,
            color,
            pal,
            h=max(96, 176 - 26 * max(0, len(report["clusters"]) - 4)),
        ),
        "month_label": _month_label(period["month"], lang),
        "range_label": f"{start_label} – {end_label}",
        "generated_label": gen_local.strftime("%Y-%m-%d %H:%M" if lang == "en" else "%d.%m.%Y, %H:%M"),
        "day_label": lambda d: str(d.day),
        "targets": {"rpo": m.RPO_TARGETS, "restore": m.RESTORE_TARGETS, "day": m.DAY_TARGETS},
        "tolerance": m.TOLERANCE_HOURS,
    }
    return _env.get_template("report.html").render(ctx)


def write_pdf(html: str, target: Path) -> None:
    from weasyprint import HTML

    target.parent.mkdir(parents=True, exist_ok=True)
    HTML(string=html, base_url=str(TEMPLATE_DIR)).write_pdf(str(target))
