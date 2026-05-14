"""FastAPI companion app — entry point, middleware, and router registration."""

import asyncio
import logging
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from slowapi import Limiter
from slowapi.errors import RateLimitExceeded
from slowapi.util import get_remote_address

import auth as _auth
from auth import create_jwt, decode_jwt
from config import LOGIN_RATE_LIMIT_PER_5MIN, STORAGE_MODE, WARM_WORKERS
from db import audit_user_var, close_pool, get_conn, init_pool
from logging_config import configure_logging, request_id_ctx, user_ctx
from metrics import http_request_duration_seconds, http_requests_total
from routes import admin, annotations, cold_storage, labels, preferences, proxy, static, studies
from routes import auth as auth_routes

# Configure JSON logging before any module-level log lines fire.
configure_logging()

logger = logging.getLogger(__name__)

DIST_DIR = Path(__file__).parent / "dist"

_ALEMBIC_INI = Path(__file__).resolve().parent / "alembic.ini"


def _init_db():
    """Bring the DB schema up to date, then sync the dynamic labelled tables."""
    from alembic.config import Config

    from alembic import command
    from labelled_table_sync import ensure_labelled_tables

    cfg = Config(str(_ALEMBIC_INI))
    command.upgrade(cfg, "head")

    conn = get_conn()
    try:
        ensure_labelled_tables(conn)
        conn.commit()
    finally:
        conn.close()


async def _eviction_loop() -> None:
    from cache_manager import run_eviction

    while True:
        await asyncio.sleep(900)
        try:
            evicted = run_eviction()
            if evicted:
                for uid in evicted:
                    logger.info("eviction_loop: evicted study", extra={"study_uid": uid})
                logger.info(
                    "eviction_loop: removed %d studies (sample=%s)",
                    len(evicted), evicted[:10],
                )
        except Exception:
            logger.exception("eviction_loop: cold cache eviction failed")


@asynccontextmanager
async def lifespan(application: FastAPI):
    init_pool()
    _init_db()
    # Alembic's env.py calls logging.config.fileConfig() during the startup
    # migration run, which wipes the JSON handler.  Re-install it.
    configure_logging()
    proxy.init_client()
    # Bounded executor for backgrounded cold-storage warms. The route
    # handler for POST /api/studies/{uid}/warm submits the extraction here
    # and returns 202 immediately. Created unconditionally — legacy mode
    # never submits to it.
    application.state.warm_executor = ThreadPoolExecutor(
        max_workers=WARM_WORKERS,
        thread_name_prefix="warm",
    )
    ev_task: asyncio.Task | None = None
    if STORAGE_MODE == "cold_path_cache":
        ev_task = asyncio.create_task(_eviction_loop())
    try:
        yield
    finally:
        if ev_task is not None:
            ev_task.cancel()
            try:
                await ev_task
            except asyncio.CancelledError:
                pass
        await proxy.shutdown_client()
        # Wait for in-flight extractions — they hold a DB pool connection.
        application.state.warm_executor.shutdown(wait=True)
        close_pool()


app = FastAPI(title="SSC Series Annotations", lifespan=lifespan)

# ---------------------------------------------------------------------------
# Rate limiter
# ---------------------------------------------------------------------------

limiter = Limiter(key_func=get_remote_address)
app.state.limiter = limiter


@app.exception_handler(RateLimitExceeded)
async def _rate_limit_handler(request: Request, exc: RateLimitExceeded):
    retry_after = getattr(exc, "retry_after", None) or 60
    return JSONResponse(
        status_code=429,
        content={"detail": "Too many attempts; please wait and retry."},
        headers={"Retry-After": str(int(retry_after))},
    )


# ---------------------------------------------------------------------------
# Static assets
# ---------------------------------------------------------------------------

if DIST_DIR.is_dir():
    app.mount("/assets", StaticFiles(directory=str(DIST_DIR / "assets")), name="assets")


# ---------------------------------------------------------------------------
# Middleware: sliding JWT refresh
# ---------------------------------------------------------------------------

@app.middleware("http")
async def sliding_jwt(request, call_next):
    response = await call_next(request)
    path = request.url.path
    # Skip sliding-refresh on routes that own their own auth_token cookie
    # (login sets a fresh token; logout deletes it) and on read-only probes
    # we don't want to extend the session from (/api/me, static assets).
    if (
        path.startswith("/assets/")
        or path in ("/api/me", "/api/login", "/api/logout")
    ):
        return response
    token = request.cookies.get("auth_token")
    if token:
        payload = decode_jwt(token)
        if payload:
            response.set_cookie(
                key="auth_token",
                value=create_jwt(payload["sub"], iat=payload.get("iat")),
                httponly=True,
                secure=_auth.COOKIE_SECURE,
                samesite="lax",
                max_age=int(_auth.JWT_EXPIRY_HOURS * 3600),
            )
    return response


# ---------------------------------------------------------------------------
# Middleware: request-ID + metrics
# ---------------------------------------------------------------------------

def _matched_path_template(request: Request) -> str:
    route = request.scope.get("route")
    path = getattr(route, "path", None)
    if isinstance(path, str) and path:
        return path
    return request.url.path or "__no_route__"


@app.middleware("http")
async def request_id_middleware(request: Request, call_next):
    req_id = request.headers.get("x-request-id") or uuid.uuid4().hex
    rid_token = request_id_ctx.set(req_id)

    user_token = None
    audit_token = None
    auth_cookie = request.cookies.get("auth_token")
    if auth_cookie:
        try:
            payload = decode_jwt(auth_cookie)
        except Exception:
            payload = None
        if payload and payload.get("sub"):
            username = str(payload["sub"])
            user_token = user_ctx.set(username)
            audit_token = audit_user_var.set(username)

    start = time.perf_counter()
    status_code = 500
    try:
        response = await call_next(request)
        status_code = response.status_code
        response.headers["X-Request-ID"] = req_id
        return response
    finally:
        elapsed = time.perf_counter() - start
        path_template = _matched_path_template(request)
        if path_template != "/metrics":
            try:
                http_requests_total.labels(
                    method=request.method,
                    path_template=path_template,
                    status=str(status_code),
                ).inc()
                http_request_duration_seconds.labels(
                    method=request.method,
                    path_template=path_template,
                ).observe(elapsed)
            except Exception:
                pass
            logger.info(
                "request",
                extra={
                    "http_method": request.method,
                    "http_path": request.url.path,
                    "http_path_template": path_template,
                    "http_status": status_code,
                    "duration_seconds": round(elapsed, 6),
                },
            )
        request_id_ctx.reset(rid_token)
        if audit_token is not None:
            audit_user_var.reset(audit_token)
        if user_token is not None:
            user_ctx.reset(user_token)


# ---------------------------------------------------------------------------
# Apply rate limit to login route (slowapi needs the app instance)
# ---------------------------------------------------------------------------

@app.on_event("startup")
def _apply_rate_limits():
    for route in app.routes:
        if getattr(route, "path", None) == "/api/login" and getattr(route, "methods", None):
            route.endpoint = limiter.limit(f"{LOGIN_RATE_LIMIT_PER_5MIN}/5 minutes")(route.endpoint)


# ---------------------------------------------------------------------------
# Register routers (order matters — static catch-all must be last)
# ---------------------------------------------------------------------------

app.include_router(auth_routes.router)
app.include_router(preferences.router)
app.include_router(studies.router)
app.include_router(cold_storage.router)
app.include_router(annotations.router)
app.include_router(labels.router)
app.include_router(admin.router)
app.include_router(proxy.router)
app.include_router(static.router)
