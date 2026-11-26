from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request

from app import db
from app.config import APP_VERSION, DEBUG, SECRET_KEY
from app.i18n import DEFAULT_LANGUAGE, SUPPORTED_LANGUAGES, set_language
from app.web import auth as auth_module
from app.web.auth import RoleMiddleware
from app.web.csrf import OriginCheckMiddleware
from app.web.routes_bootstrap import router as bootstrap_router
from app.web.routes_clusters import router as clusters_router
from app.web.routes_dashboard import router as dashboard_router
from app.web.routes_metrics import router as metrics_router
from app.web.routes_notifications import router as notifications_router
from app.web.routes_reports import router as reports_router
from app.web.routes_restore import router as restore_router
from app.web.routes_settings import router as settings_router
from app.web.sync import auto_sync_loop, restore_executor, restore_queue_loop, sync_executor

logging.basicConfig(
    level=logging.DEBUG if DEBUG else logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)


def _validate_config() -> None:
    if not SECRET_KEY or SECRET_KEY == "changeme":
        raise RuntimeError(
            "BSENTINEL_SECRET_KEY is not set. Generate with: "
            'python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"'
        )
    try:
        from cryptography.fernet import Fernet

        Fernet(SECRET_KEY.encode() if isinstance(SECRET_KEY, str) else SECRET_KEY)
    except Exception as exc:
        raise RuntimeError(f"BSENTINEL_SECRET_KEY is not a valid Fernet key: {exc}") from exc


_validate_config()

if auth_module.ALLOW_ANONYMOUS:
    logging.getLogger(__name__).warning(
        "BSENTINEL_ALLOW_ANONYMOUS is set: requests without a signed-in user are accepted. "
        "For local development only — never behind a public address."
    )


@asynccontextmanager
async def lifespan(app: FastAPI):
    await asyncio.get_event_loop().run_in_executor(None, db.init_db)
    # A restore that was running when the app stopped (e.g. a deploy) must not be
    # interrupted: re-attach to its still-running PVE task instead of orphaning
    # (and reaping) it. Best-effort; resume runs on the restore executor.
    try:
        from app.web.sync import reconcile_and_resume_restores

        await asyncio.get_event_loop().run_in_executor(None, reconcile_and_resume_restores)
    except Exception as exc:
        logging.getLogger(__name__).error("restore reconcile/resume on startup failed: %s", exc)
    try:
        from app.services.report_archive import seal_archived_reports

        await asyncio.get_event_loop().run_in_executor(None, seal_archived_reports)
    except Exception as exc:
        logging.getLogger(__name__).error("recording report hashes on startup failed: %s", exc)
    # A performance test of a restore storage runs in a thread of the app: one
    # that was running when the app stopped is gone, only its marker is left.
    try:
        from app.services import restore_perf

        await asyncio.get_event_loop().run_in_executor(None, restore_perf.recover_orphaned)
    except Exception as exc:
        logging.getLogger(__name__).error("releasing an interrupted performance test failed: %s", exc)
    sync_task = asyncio.create_task(auto_sync_loop())
    restore_queue_task = asyncio.create_task(restore_queue_loop())
    yield
    for task in (sync_task, restore_queue_task):
        task.cancel()
    for task in (sync_task, restore_queue_task):
        try:
            await task
        except asyncio.CancelledError:
            pass
    sync_executor.shutdown(wait=True)
    restore_executor.shutdown(wait=False)


class LanguageMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        lang = request.cookies.get("bsentinel-lang", DEFAULT_LANGUAGE)
        if lang not in SUPPORTED_LANGUAGES:
            lang = DEFAULT_LANGUAGE
        set_language(lang)
        return await call_next(request)


class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    """Adds standard security headers to every response.

    The CSP allows scripts and styles from the app's own files only — no
    inline script, handler, style block or style attribute (values from the
    data reach the page as data-css-*, see static/js/common.js). Brand logos
    are loaded from an externally configured URL, so img-src allows https:.
    The two standalone previews (report, weekly mail) set their own policy
    (app.web.common.PREVIEW_CSP).
    """

    async def dispatch(self, request: Request, call_next):
        response = await call_next(request)
        response.headers.setdefault(
            "Content-Security-Policy",
            "default-src 'self'; "
            "script-src 'self'; "
            "style-src 'self'; "
            "img-src 'self' data: https:; "
            "font-src 'self' data:; "
            "connect-src 'self'; "
            "frame-ancestors 'none'; "
            "base-uri 'self'; "
            "form-action 'self'",
        )
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("X-Frame-Options", "DENY")
        response.headers.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
        response.headers.setdefault("Permissions-Policy", "geolocation=(), microphone=(), camera=()")
        # Never cache HTML pages: use no-store (not just no-cache), so the browser
        # also skips the back/forward cache (bfcache). With no-cache the back
        # button could still show a STALE page from bfcache — e.g. a setting
        # auto-saved on one page, then "back" showed its old value until a hard
        # reload. no-store forces a fresh fetch on every navigation, so the page
        # always reflects the current DB state (and picks up the asset
        # cache-buster after a deploy). Static assets stay cacheable (busted by ?v=).
        if response.headers.get("content-type", "").startswith("text/html"):
            response.headers["Cache-Control"] = "no-store"
        return response


class RequestIDMiddleware(BaseHTTPMiddleware):
    """Adds a short request ID header for log correlation."""

    async def dispatch(self, request: Request, call_next):
        import uuid

        rid = request.headers.get("x-request-id") or uuid.uuid4().hex[:12]
        response = await call_next(request)
        response.headers["X-Request-ID"] = rid
        return response


app = FastAPI(title="Backup Sentinel", lifespan=lifespan)
app.add_middleware(LanguageMiddleware)
app.add_middleware(RoleMiddleware)
app.add_middleware(OriginCheckMiddleware)
app.add_middleware(SecurityHeadersMiddleware)
app.add_middleware(RequestIDMiddleware)
app.mount("/static", StaticFiles(directory="static"), name="static")

app.include_router(dashboard_router)
app.include_router(settings_router)
app.include_router(clusters_router)
app.include_router(reports_router)
app.include_router(restore_router)
app.include_router(notifications_router)
app.include_router(bootstrap_router)
app.include_router(metrics_router)


@app.get("/favicon.ico", include_in_schema=False)
def favicon() -> FileResponse:
    return FileResponse("static/favicon.ico", media_type="image/x-icon")


@app.get("/healthz")
def healthz() -> JSONResponse:
    try:
        with db.connect() as conn, conn.cursor() as cur:
            cur.execute("SELECT 1")
    except Exception as exc:
        # Reached without a login: the reason (host, user, driver text) goes to the log only.
        logging.getLogger(__name__).error("healthz: database check failed: %s", exc)
        return JSONResponse({"status": "degraded", "version": APP_VERSION, "database": "unreachable"}, status_code=503)
    return JSONResponse({"status": "ok", "version": APP_VERSION})
