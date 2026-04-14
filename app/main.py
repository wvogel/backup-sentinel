from __future__ import annotations

import asyncio
import logging
import os as _os
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request

from app import db
from app.config import SECRET_KEY
from app.i18n import SUPPORTED_LANGUAGES, DEFAULT_LANGUAGE, set_language
from app.web.routes_bootstrap import router as bootstrap_router
from app.web.routes_clusters import router as clusters_router
from app.web.routes_dashboard import router as dashboard_router
from app.web.routes_metrics import router as metrics_router
from app.web.routes_notifications import router as notifications_router
from app.web.routes_reports import router as reports_router
from app.web.routes_settings import router as settings_router
from app.web.sync import auto_sync_loop, sync_executor

logging.basicConfig(
    level=logging.DEBUG if _os.getenv("BSENTINEL_DEBUG", "").lower() in ("1", "true", "yes", "on") else logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)


def _validate_config() -> None:
    if not SECRET_KEY or SECRET_KEY == "changeme":
        raise RuntimeError(
            "BSENTINEL_SECRET_KEY is not set. Generate with: "
            "python -c \"from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())\""
        )
    try:
        from cryptography.fernet import Fernet
        Fernet(SECRET_KEY.encode() if isinstance(SECRET_KEY, str) else SECRET_KEY)
    except Exception as exc:
        raise RuntimeError(f"BSENTINEL_SECRET_KEY is not a valid Fernet key: {exc}")


_validate_config()


@asynccontextmanager
async def lifespan(app: FastAPI):
    await asyncio.get_event_loop().run_in_executor(None, db.init_db)
    sync_task = asyncio.create_task(auto_sync_loop())
    yield
    sync_task.cancel()
    sync_executor.shutdown(wait=True)


class LanguageMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        lang = request.cookies.get("bsentinel-lang", DEFAULT_LANGUAGE)
        if lang not in SUPPORTED_LANGUAGES:
            lang = DEFAULT_LANGUAGE
        set_language(lang)
        return await call_next(request)


class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    """Adds standard security headers to every response.

    CSP allows inline scripts/styles because the app inlines bootstrap JS
    (theme toggle, i18n) and style attributes. Brand logos are loaded from
    an externally configured URL, so img-src allows https:.
    """

    async def dispatch(self, request: Request, call_next):
        response = await call_next(request)
        response.headers.setdefault(
            "Content-Security-Policy",
            "default-src 'self'; "
            "script-src 'self' 'unsafe-inline'; "
            "style-src 'self' 'unsafe-inline'; "
            "img-src 'self' data: https:; "
            "font-src 'self' data:; "
            "connect-src 'self'; "
            "frame-ancestors 'none'; "
            "base-uri 'self'; "
            "form-action 'self'"
        )
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("X-Frame-Options", "DENY")
        response.headers.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
        response.headers.setdefault("Permissions-Policy", "geolocation=(), microphone=(), camera=()")
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
app.add_middleware(SecurityHeadersMiddleware)
app.add_middleware(RequestIDMiddleware)
app.mount("/static", StaticFiles(directory="static"), name="static")

app.include_router(dashboard_router)
app.include_router(settings_router)
app.include_router(clusters_router)
app.include_router(reports_router)
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
        return JSONResponse({"status": "degraded", "database": str(exc)}, status_code=503)
    return JSONResponse({"status": "ok"})
