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


_SIZE_UNITS_SHORT = ("B", "KB", "MB", "GB", "TB", "PB", "EB")


def _floor_2sig(v: float) -> float:
    """Round down to two significant figures: 436→430, 1.92→1.9, 36.72→36."""
    if v <= 0:
        return 0.0
    factor = 10.0 ** (math.floor(math.log10(v)) - 1)
    return math.floor(v / factor) * factor


def format_size_coarse(value: object, lang: str | None = None) -> str:
    """Bytes at two significant figures, rounded down, for at-a-glance storage
    sizes: "430 GB", "1,9 TB", "36 TB". No noisy decimals ("436,72 GB" → "430 GB").
    Binary scaling like the Proxmox GUI, with short (GB/TB) labels; "–" when unknown."""
    try:
        v = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return "–"
    if not math.isfinite(v) or v < 0:
        return "–"
    unit = 0
    while unit < len(_SIZE_UNITS_SHORT) - 1 and v >= 1000:
        v /= 1024
        unit += 1
    f = _floor_2sig(v)
    number = str(int(f)) if f == int(f) else f"{f:.1f}"
    return f"{_localize(number, lang)} {_SIZE_UNITS_SHORT[unit]}"


def format_rate(bytes_per_second: object, lang: str | None = None) -> str:
    """Throughput as "188 MiB/s"; "–" when unknown."""
    size = format_size(bytes_per_second, lang)
    return size if size == "–" else f"{size}/s"


def format_bitrate(bytes_per_second: object, lang: str | None = None) -> str:
    """A network rate in *decimal* bits, the way links and transfers are measured
    (1 Gb = 1000 Mb, not 1024): Gigabit with at most one decimal, dropped when it
    is whole ("10 Gb/s", "2,5 Gb/s"), and below 1 Gb/s whole Megabit ("940 Mb/s").
    Takes bytes/s so it pairs with the byte counters; "–" when unknown."""
    try:
        bits = float(bytes_per_second) * 8  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return "–"
    if not math.isfinite(bits) or bits < 0:
        return "–"
    if bits >= 1_000_000_000:
        text = f"{bits / 1_000_000_000:.1f}"
        if text.endswith(".0"):
            text = text[:-2]  # 10,0 → 10, but 2,5 keeps its decimal
        return f"{_localize(text, lang)} Gb/s"
    return f"{_localize(str(int(bits // 1_000_000)), lang)} Mb/s"


def format_count(value: object, lang: str | None = None) -> str:
    """Whole numbers with a thousands separator: "1.234" / "1,234"."""
    try:
        return _localize(f"{int(value):,}", lang)  # type: ignore[call-overload]
    except (TypeError, ValueError):
        return str(value)
