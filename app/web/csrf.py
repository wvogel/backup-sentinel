"""CSRF protection by origin verification (OWASP "verifying origin with standard headers").

Browsers send `Origin` on every POST (and a `Referer` under our
strict-origin-when-cross-origin policy). A state-changing request whose
origin is another site is rejected. Requests with neither header come from
non-browser clients — the PVE/PBS bootstrap scripts use curl and
authenticate with their enrollment secret — and are left alone.
"""

from __future__ import annotations

from collections.abc import Mapping
from urllib.parse import urlsplit

from fastapi.responses import PlainTextResponse
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request

from app.config import APP_BASE_URL

SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS", "TRACE"})


def _host(value: str) -> str:
    """Hostname[:port] of a URL or Host header, lower-cased."""
    value = value.strip().lower()
    if "://" in value:
        value = urlsplit(value).netloc
    return value.split(",")[0].strip()


def _hostname(value: str) -> str:
    return value.rsplit(":", 1)[0] if value.count(":") == 1 else value


def is_cross_site(method: str, headers: Mapping[str, str], configured_host: str | None) -> bool:
    if method.upper() in SAFE_METHODS:
        return False
    source = headers.get("origin") or headers.get("referer")
    if not source:
        return False  # non-browser client
    if source.strip().lower() == "null":
        return True  # opaque origin (sandboxed frame, file://, …)
    trusted = {_host(h) for h in (headers.get("host"), headers.get("x-forwarded-host"), configured_host) if h}
    trusted_names = {_hostname(h) for h in trusted}
    origin = _host(source)
    return origin not in trusted and _hostname(origin) not in trusted_names


class OriginCheckMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        configured = _host(APP_BASE_URL) if APP_BASE_URL else None
        headers = {key.lower(): value for key, value in request.headers.items()}
        if is_cross_site(request.method, headers, configured):
            return PlainTextResponse("Cross-site request blocked", status_code=403)
        return await call_next(request)
