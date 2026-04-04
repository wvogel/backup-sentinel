"""Shared HTTP client for Proxmox PVE and PBS API calls."""
from __future__ import annotations

import json
import logging
import ssl
import time
from typing import Any
from urllib import error, parse, request

from app.config import API_TIMEOUT, INSECURE_SSL

logger = logging.getLogger(__name__)

_ssl_context: ssl.SSLContext | None = None


def _get_ssl_context() -> ssl.SSLContext:
    global _ssl_context
    if _ssl_context is None:
        if INSECURE_SSL:
            _ssl_context = ssl._create_unverified_context()
        else:
            _ssl_context = ssl.create_default_context()
    return _ssl_context


class APIError(RuntimeError):
    """Base error for Proxmox/PBS API calls."""


def api_get(
    base_url: str,
    path: str,
    auth_header: str,
    *,
    timeout: float = API_TIMEOUT,
    query: dict[str, str] | None = None,
    label: str = "API",
) -> Any:
    """Perform an authenticated GET and return the parsed JSON 'data' field."""
    query_string = f"?{parse.urlencode(query)}" if query else ""
    url = f"{base_url}{path}{query_string}"
    req = request.Request(
        url,
        headers={"Authorization": auth_header, "Accept": "application/json"},
        method="GET",
    )
    context = _get_ssl_context()

    t0 = time.monotonic()
    short_path = path.replace("/api2/json", "")
    qs_short = f"?{parse.urlencode(query)}" if query else ""
    logger.info("→ %s %s%s …", label, short_path, qs_short)
    try:
        with request.urlopen(req, timeout=timeout, context=context) as response:
            payload = json.load(response)
    except error.HTTPError as exc:
        elapsed = time.monotonic() - t0
        logger.warning("← %s %s%s → HTTP %d (%.1fs)", label, short_path, qs_short, exc.code, elapsed)
        detail = exc.read().decode("utf-8", errors="replace")
        raise APIError(f"{label} API returned HTTP {exc.code} for {path}: {detail}") from exc
    except error.URLError as exc:
        elapsed = time.monotonic() - t0
        logger.warning("← %s %s%s → FEHLER (%.1fs): %s", label, short_path, qs_short, elapsed, exc.reason)
        raise APIError(f"{label} API request failed for {path}: {exc.reason}") from exc

    elapsed = time.monotonic() - t0
    data = payload.get("data")
    logger.info("← %s %s%s → %s (%.1fs)", label, short_path, qs_short, type(data).__name__, elapsed)
    return data


def api_get_json(
    base_url: str,
    path: str,
    auth_header: str,
    *,
    timeout: float = API_TIMEOUT,
    query: dict[str, str] | None = None,
    label: str = "API",
) -> list[dict[str, Any]]:
    """Like api_get but asserts the result is a list."""
    data = api_get(base_url, path, auth_header, timeout=timeout, query=query, label=label)
    if not isinstance(data, list):
        raise APIError(f"Unexpected {label} response for {path}")
    return data


def api_get_dict(
    base_url: str,
    path: str,
    auth_header: str,
    *,
    timeout: float = API_TIMEOUT,
    query: dict[str, str] | None = None,
    label: str = "API",
) -> dict[str, Any]:
    """Like api_get but asserts the result is a dict."""
    data = api_get(base_url, path, auth_header, timeout=timeout, query=query, label=label)
    if not isinstance(data, dict):
        raise APIError(f"Unexpected {label} response for {path}")
    return data


def api_test_connection(
    url: str,
    auth_header: str,
    *,
    timeout: float = 5.0,
    label: str = "API",
) -> None:
    """Test connectivity. Raises APIError on failure."""
    req = request.Request(
        url,
        headers={"Authorization": auth_header, "Accept": "application/json"},
        method="GET",
    )
    context = _get_ssl_context()
    try:
        with request.urlopen(req, timeout=timeout, context=context):
            pass
    except error.HTTPError as exc:
        raise APIError(f"HTTP {exc.code}") from exc
    except error.URLError as exc:
        raise APIError(f"Verbindungsfehler: {exc.reason}") from exc
