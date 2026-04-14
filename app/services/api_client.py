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


_MAX_RETRIES = 2  # 1 initial + 2 retries = 3 attempts total
_RETRY_BACKOFF = 1.5  # seconds; doubled on each retry


def _is_retryable_http(code: int) -> bool:
    """Server errors and rate-limits are worth retrying; 4xx client errors are not."""
    return code in (502, 503, 504, 429) or code >= 500


def api_get(
    base_url: str,
    path: str,
    auth_header: str,
    *,
    timeout: float = API_TIMEOUT,
    query: dict[str, str] | None = None,
    label: str = "API",
) -> Any:
    """Perform an authenticated GET and return the parsed JSON 'data' field.
    Retries transient network errors and 5xx/429 responses with exponential backoff."""
    query_string = f"?{parse.urlencode(query)}" if query else ""
    url = f"{base_url}{path}{query_string}"
    req = request.Request(
        url,
        headers={"Authorization": auth_header, "Accept": "application/json"},
        method="GET",
    )
    context = _get_ssl_context()

    short_path = path.replace("/api2/json", "")
    qs_short = f"?{parse.urlencode(query)}" if query else ""
    logger.info("→ %s %s%s …", label, short_path, qs_short)

    last_exc: Exception | None = None
    for attempt in range(_MAX_RETRIES + 1):
        t0 = time.monotonic()
        try:
            with request.urlopen(req, timeout=timeout, context=context) as response:
                payload = json.load(response)
            elapsed = time.monotonic() - t0
            data = payload.get("data")
            logger.info("← %s %s%s → %s (%.1fs)", label, short_path, qs_short, type(data).__name__, elapsed)
            return data
        except error.HTTPError as exc:
            elapsed = time.monotonic() - t0
            if attempt < _MAX_RETRIES and _is_retryable_http(exc.code):
                delay = _RETRY_BACKOFF * (2**attempt)
                logger.warning(
                    "← %s %s%s → HTTP %d (%.1fs), retry in %.1fs", label, short_path, qs_short, exc.code, elapsed, delay
                )
                time.sleep(delay)
                last_exc = exc
                continue
            logger.warning("← %s %s%s → HTTP %d (%.1fs)", label, short_path, qs_short, exc.code, elapsed)
            detail = exc.read().decode("utf-8", errors="replace")
            raise APIError(f"{label} API returned HTTP {exc.code} for {path}: {detail}") from exc
        except error.URLError as exc:
            elapsed = time.monotonic() - t0
            # SSL / cert errors are never transient — fail fast.
            reason_str = str(exc.reason)
            is_ssl_error = isinstance(exc.reason, ssl.SSLError) or "SSL" in reason_str or "certificate" in reason_str
            if attempt < _MAX_RETRIES and not is_ssl_error:
                delay = _RETRY_BACKOFF * (2**attempt)
                logger.warning(
                    "← %s %s%s → FEHLER (%.1fs): %s, retry in %.1fs",
                    label,
                    short_path,
                    qs_short,
                    elapsed,
                    exc.reason,
                    delay,
                )
                time.sleep(delay)
                last_exc = exc
                continue
            logger.warning("← %s %s%s → FEHLER (%.1fs): %s", label, short_path, qs_short, elapsed, exc.reason)
            raise APIError(f"{label} API request failed for {path}: {exc.reason}") from exc

    # Should be unreachable, but satisfy type checker
    raise APIError(f"{label} API request failed for {path}") from last_exc


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
