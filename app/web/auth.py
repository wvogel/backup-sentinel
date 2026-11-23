"""Roles from the identity oauth2-proxy forwards: viewer < operator < admin.

``BSENTINEL_ADMINS`` and ``BSENTINEL_OPERATORS`` list e-mail addresses, user
names or IdP groups (comma-separated, case-insensitive). Whoever signs in and
is in neither list may only read. With both lists empty roles are off and
every signed-in user is admin — the behaviour before roles existed.

The check is one middleware with a default of *admin* for every state-changing
request, so a new route is closed until it is listed here.
"""

from __future__ import annotations

import os
import re
from collections.abc import Mapping

from fastapi.responses import JSONResponse, PlainTextResponse
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request

ROLES = ("viewer", "operator", "admin")
_RANK = {role: rank for rank, role in enumerate(ROLES)}

_USER_HEADERS = (
    "x-forwarded-email",
    "x-auth-request-email",
    "x-forwarded-user",
    "x-auth-request-user",
    "x-forwarded-preferred-username",
    "x-auth-request-preferred-username",
)
_GROUP_HEADERS = ("x-forwarded-groups", "x-auth-request-groups")

SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})

# Reached without a login (oauth2-proxy skips them); each checks its own secret.
PUBLIC_PATHS = frozenset({"/api/bootstrap/finalize", "/api/bootstrap/pbs-finalize"})

# Day-to-day operation: run things and record results. Everything else that
# changes state — settings, targets, clusters, deleting evidence — is admin.
OPERATOR_ROUTES = tuple(
    re.compile(pattern)
    for pattern in (
        r"^/clusters/[^/]+/sync$",
        r"^/clusters/[^/]+/restore-tests$",
        r"^/reports/generate$",
        r"^/restore/vm/\d+/test$",
        r"^/restore/runs/\d+/(delete|cancel|reap|promote|retest)$",
        r"^/restore/queue/(pause|reorder)$",
    )
)


def parse_names(raw: str) -> frozenset[str]:
    return frozenset(part.strip().lower() for part in raw.split(",") if part.strip())


ADMINS = parse_names(os.getenv("BSENTINEL_ADMINS", ""))
OPERATORS = parse_names(os.getenv("BSENTINEL_OPERATORS", ""))


def roles_enabled(admins: frozenset[str] | None = None, operators: frozenset[str] | None = None) -> bool:
    return bool((ADMINS if admins is None else admins) or (OPERATORS if operators is None else operators))


def identities(headers: Mapping[str, str]) -> set[str]:
    """Every name the signed-in user goes by: e-mail, user name and groups."""
    names = {headers.get(h, "").strip().lower() for h in _USER_HEADERS}
    for header in _GROUP_HEADERS:
        names |= parse_names(headers.get(header, ""))
    names.discard("")
    return names


def role_for(
    headers: Mapping[str, str],
    admins: frozenset[str] | None = None,
    operators: frozenset[str] | None = None,
) -> str:
    admins = ADMINS if admins is None else admins
    operators = OPERATORS if operators is None else operators
    if not (admins or operators):
        return "admin"
    names = identities(headers)
    if names & admins:
        return "admin"
    if names & operators:
        return "operator"
    return "viewer"


def required_role(method: str, path: str) -> str | None:
    """The least role a request needs; None for the routes that carry their own secret."""
    if path in PUBLIC_PATHS:
        return None
    if method.upper() in SAFE_METHODS:
        return "admin" if path == "/settings" or path.startswith("/settings/") else "viewer"
    if any(pattern.match(path) for pattern in OPERATOR_ROUTES):
        return "operator"
    return "admin"


def allowed(role: str, needed: str | None) -> bool:
    return needed is None or _RANK[role] >= _RANK[needed]


class RoleMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        headers = {key.lower(): value for key, value in request.headers.items()}
        role = role_for(headers)
        request.state.role = role
        needed = required_role(request.method, request.url.path)
        if not allowed(role, needed):
            message = f"Forbidden: this action needs the role '{needed}' (yours: '{role}')."
            if "application/json" in headers.get("accept", "") or request.url.path.startswith("/api/"):
                return JSONResponse({"detail": message}, status_code=403)
            return PlainTextResponse(message, status_code=403)
        return await call_next(request)
