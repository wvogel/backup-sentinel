"""Shared HTTP client for Proxmox PVE and PBS API calls."""

from __future__ import annotations

import http.client
import io
import json
import logging
import ssl
import threading
import time
from typing import Any
from urllib import error, parse

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


# One open connection per server and thread (keep-alive): a sync asks the
# same PVE/PBS dozens of times, and each new connection costs a name lookup
# and a TLS handshake — occasionally seconds.
_local = threading.local()


def _pooled(scheme: str, host: str, port: int | None, timeout: float) -> http.client.HTTPConnection:
    pool: dict[tuple[str, str, int | None], http.client.HTTPConnection] = _local.__dict__.setdefault("pool", {})
    key = (scheme, host, port)
    conn = pool.get(key)
    if conn is None:
        if scheme == "https":
            conn = http.client.HTTPSConnection(host, port, timeout=timeout, context=_get_ssl_context())
        else:
            conn = http.client.HTTPConnection(host, port, timeout=timeout)
        pool[key] = conn
    conn.timeout = timeout
    if conn.sock is not None:
        conn.sock.settimeout(timeout)
    return conn


def _send(url: str, headers: dict[str, str], timeout: float) -> bytes:
    """GET over the pooled connection; errors as urllib raises them (HTTPError
    for status >= 400, URLError otherwise), so callers handle both alike."""
    parts = parse.urlsplit(url)
    target = parts.path + (f"?{parts.query}" if parts.query else "")
    for first in (True, False):
        conn = _pooled(parts.scheme, parts.hostname or "", parts.port, timeout)
        reused = conn.sock is not None
        try:
            conn.request("GET", target, headers=headers)
            response = conn.getresponse()
            body = response.read()
        except (ConnectionResetError, BrokenPipeError, http.client.BadStatusLine) as exc:
            conn.close()
            if first and reused:
                continue  # the server closed the idle connection: open a new one at once
            raise error.URLError(exc) from exc
        except (OSError, http.client.HTTPException) as exc:
            conn.close()
            raise error.URLError(exc) from exc
        if response.will_close:
            conn.close()
        if response.status >= 400:
            raise error.HTTPError(url, response.status, response.reason, response.headers, io.BytesIO(body))
        return body
    raise AssertionError("unreachable")


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
    retries: int = _MAX_RETRIES,
) -> Any:
    """Perform an authenticated GET and return the parsed JSON 'data' field."""
    return api_get_payload(base_url, path, auth_header, timeout=timeout, query=query, label=label, retries=retries).get(
        "data"
    )


def api_get_payload(
    base_url: str,
    path: str,
    auth_header: str,
    *,
    timeout: float = API_TIMEOUT,
    query: dict[str, str] | None = None,
    label: str = "API",
    retries: int = _MAX_RETRIES,
) -> dict[str, Any]:
    """Perform an authenticated GET and return the whole JSON body ('data' plus
    fields such as 'total'). Retries transient network errors and 5xx/429
    responses with exponential backoff; `retries=0` for calls whose failure
    is expected or has a fallback (no waiting for the same answer)."""
    query_string = f"?{parse.urlencode(query)}" if query else ""
    url = f"{base_url}{path}{query_string}"
    headers = {"Authorization": auth_header, "Accept": "application/json"}

    short_path = path.replace("/api2/json", "")
    qs_short = f"?{parse.urlencode(query)}" if query else ""
    logger.info("→ %s %s%s …", label, short_path, qs_short)

    last_exc: Exception | None = None
    for attempt in range(retries + 1):
        t0 = time.monotonic()
        try:
            payload = json.loads(_send(url, headers, timeout))
            elapsed = time.monotonic() - t0
            if not isinstance(payload, dict):
                raise APIError(f"Unexpected {label} response for {path}")
            data = payload.get("data")
            logger.info("← %s %s%s → %s (%.1fs)", label, short_path, qs_short, type(data).__name__, elapsed)
            return payload
        except error.HTTPError as exc:
            elapsed = time.monotonic() - t0
            if attempt < retries and _is_retryable_http(exc.code):
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
            if attempt < retries and not is_ssl_error:
                delay = _RETRY_BACKOFF * (2**attempt)
                logger.warning(
                    "← %s %s%s → ERROR (%.1fs): %s, retry in %.1fs",
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
            logger.warning("← %s %s%s → ERROR (%.1fs): %s", label, short_path, qs_short, elapsed, exc.reason)
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
    retries: int = _MAX_RETRIES,
) -> list[dict[str, Any]]:
    """Like api_get but asserts the result is a list."""
    data = api_get(base_url, path, auth_header, timeout=timeout, query=query, label=label, retries=retries)
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
    retries: int = _MAX_RETRIES,
) -> dict[str, Any]:
    """Like api_get but asserts the result is a dict."""
    data = api_get(base_url, path, auth_header, timeout=timeout, query=query, label=label, retries=retries)
    if not isinstance(data, dict):
        raise APIError(f"Unexpected {label} response for {path}")
    return data
