"""Images for HTML emails: the brand logo and the Backup Sentinel wordmark as PNG.

Mail clients block remote images by default and many (Gmail, Outlook) don't
render SVG, so emails embed PNGs as inline (cid:) parts.
"""

from __future__ import annotations

import base64
import logging
from pathlib import Path
from urllib import request as urllib_request

import resvg_py

from app.config import APP_BASE_URL, BRAND_LOGO_LIGHT_URL

logger = logging.getLogger(__name__)

STATIC_DIR = Path("static")
LOGO_HEIGHT_PX = 84  # shown at 28 px — sharp on high-DPI screens


def wordmark_png() -> bytes:
    """The "// BACKUP SENTINEL" wordmark (rendered by scripts/render_email_wordmark.py)."""
    return (STATIC_DIR / "email" / "wordmark.png").read_bytes()


def _read_logo(url: str) -> tuple[bytes, str]:
    """(data, content type) of the logo at `url` — a local /static path or a URL."""
    if url.startswith("/static/"):
        path = (STATIC_DIR / url.removeprefix("/static/")).resolve()
        if STATIC_DIR.resolve() not in path.parents:
            raise ValueError(f"logo path outside static: {url}")
        return path.read_bytes(), "image/svg+xml" if path.suffix == ".svg" else ""
    if url.startswith("/"):
        if not APP_BASE_URL:
            raise ValueError("relative logo URL needs APP_URL")
        url = APP_BASE_URL.rstrip("/") + url
    req = urllib_request.Request(url, headers={"Accept": "image/*"})
    with urllib_request.urlopen(req, timeout=5) as resp:
        return resp.read(), resp.headers.get("Content-Type", "")


def brand_logo_png(url: str = BRAND_LOGO_LIGHT_URL) -> bytes | None:
    """The brand logo for the dark email header as PNG, or None (no logo configured or unreadable).

    Uses the logo of the web header in the light theme, which also sits on the primary colour.
    """
    if not url:
        return None
    try:
        data, content_type = _read_logo(url)
        if data.startswith(b"\x89PNG"):
            return data
        if "svg" in content_type or url.lower().split("?")[0].endswith(".svg") or data.lstrip().startswith(b"<"):
            return bytes(resvg_py.svg_to_bytes(svg_string=data.decode("utf-8"), height=LOGO_HEIGHT_PX))
        logger.info("Brand logo %s is neither PNG nor SVG — left out of emails", url)
    except Exception as exc:
        logger.warning("Brand logo for emails unavailable (%s): %s", url, exc)
    return None


def data_uri(png: bytes) -> str:
    """For the browser preview, where cid: references don't resolve."""
    return "data:image/png;base64," + base64.b64encode(png).decode("ascii")
