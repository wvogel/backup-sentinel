"""Numbers for humans: sizes, rates and counts without noise.

One decimal below 10, whole numbers above ("5,3 TiB", "188 MiB/s"), the unit
scaled so the number stays below 1000. Binary units like the Proxmox GUI.
German uses a decimal comma and a thousands dot, English the other way round.
Only for display — exports, API fields and sort keys keep the raw values.
"""

from __future__ import annotations

import math

from app.i18n import get_language

_SIZE_UNITS = ("B", "KiB", "MiB", "GiB", "TiB", "PiB", "EiB")


def _localize(text: str, lang: str | None) -> str:
    """Python formats "1,234.5"; German wants "1.234,5"."""
    if (lang or get_language()).lower() == "en":
        return text
    return text.translate(str.maketrans({",": ".", ".": ","}))


def short_number(value: float, *, grouping: bool = True) -> str:
    """ "5.3", "10", "188" — one decimal only while it carries information.
    `grouping` adds the thousands separator ("1,234"); unlocalized labels
    that are shown in every language leave it out."""
    if value < 9.95:
        text = f"{value:.1f}"
        return text[:-2] if text.endswith(".0") else text
    rounded = int(value + 0.5)
    return f"{rounded:,}" if grouping else str(rounded)


def format_size(value: object, lang: str | None = None) -> str:
    """Bytes as "5,3 TiB" / "5.3 TiB"; "–" when unknown."""
    try:
        v = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return "–"
    if not math.isfinite(v):
        return "–"
    sign, v = ("-", -v) if v < 0 else ("", v)
    unit = 0
    # Scale on the rounded value so 999.6 GiB becomes "1 TiB", not "1.000 GiB".
    while unit < len(_SIZE_UNITS) - 1 and v >= 999.5:
        v /= 1024
        unit += 1
    number = f"{int(v + 0.5):,}" if unit == 0 else short_number(v)
    return f"{sign}{_localize(number, lang)} {_SIZE_UNITS[unit]}"


def format_rate(bytes_per_second: object, lang: str | None = None) -> str:
    """Throughput as "188 MiB/s"; "–" when unknown."""
    size = format_size(bytes_per_second, lang)
    return size if size == "–" else f"{size}/s"


def format_count(value: object, lang: str | None = None) -> str:
    """Whole numbers with a thousands separator: "1.234" / "1,234"."""
    try:
        return _localize(f"{int(value):,}", lang)  # type: ignore[call-overload]
    except (TypeError, ValueError):
        return str(value)
