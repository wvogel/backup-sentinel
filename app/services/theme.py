"""Colour theme: four base colours → light/dark CSS tokens, WCAG checks."""

from __future__ import annotations

import re
from dataclasses import dataclass

_HEX_RE = re.compile(r"^#?([0-9a-f]{3}|[0-9a-f]{6})$")
WHITE, BLACK = "#ffffff", "#000000"


def normalize_hex(value: str | None) -> str | None:
    match = _HEX_RE.match((value or "").strip().lower())
    if not match:
        return None
    digits = match.group(1)
    if len(digits) == 3:
        digits = "".join(c * 2 for c in digits)
    return f"#{digits}"


def _rgb(hex_color: str) -> tuple[int, int, int]:
    h = hex_color.lstrip("#")
    return int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)


def _hex(rgb: tuple[float, float, float]) -> str:
    return "#" + "".join(f"{max(0, min(255, round(c))):02x}" for c in rgb)


def mix(a: str, b: str, weight_b: float) -> str:
    """sRGB mix: weight_b of `b` into `a`."""
    ra, rb = _rgb(a), _rgb(b)
    return _hex(tuple(x * (1 - weight_b) + y * weight_b for x, y in zip(ra, rb, strict=True)))


def rgba(hex_color: str, alpha: float) -> str:
    r, g, b = _rgb(hex_color)
    return f"rgba({r}, {g}, {b}, {alpha})"


def _luminance(hex_color: str) -> float:
    def channel(c: int) -> float:
        s = c / 255
        return s / 12.92 if s <= 0.03928 else ((s + 0.055) / 1.055) ** 2.4

    r, g, b = (channel(c) for c in _rgb(hex_color))
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def contrast_ratio(a: str, b: str) -> float:
    la, lb = sorted((_luminance(a), _luminance(b)), reverse=True)
    return (la + 0.05) / (lb + 0.05)


def best_ink(surface: str, *candidates: str) -> str:
    """First candidate with ≥ 4.5:1 on `surface`, else the best of black/white."""
    for ink in candidates:
        if contrast_ratio(ink, surface) >= 4.5:
            return ink
    return max((BLACK, WHITE), key=lambda ink: contrast_ratio(ink, surface))


@dataclass(frozen=True)
class BaseColors:
    primary: str
    accent: str
    background: str
    running: str


@dataclass(frozen=True)
class Preset:
    base: BaseColors
    line: str  # borders / dividers (not overridable)
    sage: str  # calm info surfaces (not overridable)


PRESETS: dict[str, Preset] = {
    "smile_eyes": Preset(
        base=BaseColors(primary="#141450", accent="#ffff00", background="#f8f5f2", running="#4a7eb5"),
        line="#efe7db",
        sage="#d8ddce",
    ),
    "neutral": Preset(
        base=BaseColors(primary="#141450", accent="#141450", background="#f2f5fa", running="#1f5fbf"),
        line="#d9e1ec",
        sage="#edf1f7",
    ),
}
DEFAULT_PRESET = "smile_eyes"
BASE_KEYS = ("primary", "accent", "background", "running")


def resolve(preset_name: str | None, overrides: dict[str, str]) -> tuple[Preset, BaseColors]:
    preset = PRESETS.get(preset_name or "", PRESETS[DEFAULT_PRESET])
    values = {key: normalize_hex(overrides.get(key)) or getattr(preset.base, key) for key in BASE_KEYS}
    return preset, BaseColors(**values)


def _darkening_for(color: str, ink: str, start: float, target: float = 7.0) -> float:
    """Smallest black-mix ≥ `start` that gives `ink` a `target` contrast on `color`."""
    k = start
    while k < 0.95 and contrast_ratio(ink, mix(color, BLACK, k)) < target:
        k = round(k + 0.05, 2)
    return k


def derive_tokens(preset: Preset, base: BaseColors) -> dict[str, dict[str, str]]:
    p, a, bg, run = base.primary, base.accent, base.background, base.running
    accent_ink = best_ink(a, p)
    header_text = best_ink(p, "#f6f7ff")
    light = {
        "--bg": bg,
        "--panel": WHITE,
        "--panel-alt": mix(bg, WHITE, 0.5),
        "--ink": p,
        "--muted": mix(p, bg, 0.42),
        "--accent": p,
        "--line": preset.line,
        "--chip": mix(p, WHITE, 0.94),
        "--chip-border": mix(p, WHITE, 0.82),
        "--header-bg": p,
        "--header-border": mix(p, WHITE, 0.15),
        "--header-link": header_text,
        "--header-link-muted": mix(header_text, p, 0.25),
        "--btn-bg": p,
        "--btn-text": header_text,
        "--btn-border": mix(p, WHITE, 0.22),
        "--accent-fill": a,
        "--accent-ink": accent_ink,
        "--running": run,
        "--running-soft": rgba(run, 0.10),
        "--running-fill": rgba(run, 0.20),
        "--sage": preset.sage,
        "--sage-ink": p,
    }
    dark_ink = mix(WHITE, p, 0.08)
    # Darken mid/light primaries until light text reads well; a dark primary
    # (smile eyes Dark Blue) keeps the base amounts.
    panel_k = _darkening_for(p, dark_ink, 0.10)
    dark_bg = mix(p, BLACK, max(0.45, min(0.95, panel_k + 0.35)))
    dark_panel = mix(p, BLACK, panel_k)
    dark_header = mix(p, BLACK, max(0.20, panel_k))
    dark_btn = mix(p, WHITE, 0.15)
    dark_run = mix(run, WHITE, 0.35)
    dark = {
        "--bg": dark_bg,
        "--panel": dark_panel,
        "--panel-alt": mix(p, BLACK, 0.30),
        "--ink": dark_ink,
        "--muted": mix(WHITE, p, 0.40),
        "--accent": dark_ink,
        "--line": mix(p, WHITE, 0.14),
        "--chip": mix(p, BLACK, 0.20),
        "--chip-border": mix(p, WHITE, 0.20),
        "--header-bg": dark_header,
        "--header-border": rgba(WHITE, 0.12),
        "--header-link": best_ink(dark_header, dark_ink),
        "--header-link-muted": mix(dark_ink, p, 0.30),
        "--btn-bg": dark_btn,
        "--btn-text": best_ink(dark_btn, dark_ink),
        "--btn-border": mix(p, WHITE, 0.30),
        "--accent-fill": a,
        "--accent-ink": accent_ink,
        "--running": dark_run,
        "--running-soft": rgba(dark_run, 0.10),
        "--running-fill": rgba(dark_run, 0.22),
        "--sage": mix(preset.sage, dark_panel, 0.80),
        "--sage-ink": mix(preset.sage, WHITE, 0.30),
    }
    return {"light": light, "dark": dark}


@dataclass(frozen=True)
class ContrastWarning:
    key: str
    ratio: float
    minimum: float


def check_contrast(base: BaseColors) -> list[ContrastWarning]:
    checks = [
        ("background", contrast_ratio(base.primary, base.background), 4.5),
        ("accent", contrast_ratio(best_ink(base.accent, base.primary), base.accent), 4.5),
        ("primary", contrast_ratio(best_ink(base.primary, "#f6f7ff"), base.primary), 4.5),
        ("running", contrast_ratio(base.running, base.background), 3.0),
    ]
    return [ContrastWarning(key, round(ratio, 2), minimum) for key, ratio, minimum in checks if ratio < minimum]


def render_css(tokens: dict[str, dict[str, str]]) -> str:
    def block(values: dict[str, str]) -> str:
        return "".join(f"{k}:{v};" for k, v in values.items())

    return f':root{{{block(tokens["light"])}}}html[data-theme="dark"]{{{block(tokens["dark"])}}}'
