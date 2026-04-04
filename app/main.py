from __future__ import annotations

import asyncio
import logging
import os as _os
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request

from app import db
from app.i18n import SUPPORTED_LANGUAGES, DEFAULT_LANGUAGE, set_language
from app.web.routes_bootstrap import router as bootstrap_router
from app.web.routes_clusters import router as clusters_router
from app.web.routes_dashboard import router as dashboard_router
from app.web.routes_reports import router as reports_router
from app.web.routes_settings import router as settings_router
from app.web.sync import auto_sync_loop, sync_executor

logging.basicConfig(
    level=logging.DEBUG if _os.getenv("BSENTINEL_DEBUG", "").lower() in ("1", "true", "yes", "on") else logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)


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


app = FastAPI(title="Backup Sentinel", lifespan=lifespan)
app.add_middleware(LanguageMiddleware)
app.mount("/static", StaticFiles(directory="static"), name="static")

app.include_router(dashboard_router)
app.include_router(settings_router)
app.include_router(clusters_router)
app.include_router(reports_router)
app.include_router(bootstrap_router)


@app.get("/healthz")
def healthz() -> JSONResponse:
    try:
        with db.connect() as conn, conn.cursor() as cur:
            cur.execute("SELECT 1")
    except Exception as exc:
        return JSONResponse({"status": "degraded", "database": str(exc)}, status_code=503)
    return JSONResponse({"status": "ok"})
