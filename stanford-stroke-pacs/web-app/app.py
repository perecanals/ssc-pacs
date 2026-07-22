"""FastAPI web-app — entry point, middleware, and router registration."""

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
from slowapi.errors import RateLimitExceeded

import auth as _auth
from auth import create_jwt, decode_jwt
from config import STORAGE_MODE, WARM_WORKERS, effective_config_summary
from db import audit_user_var, close_pool, get_conn, init_pool
from logging_config import configure_logging, request_id_ctx, user_ctx
from metrics import http_request_duration_seconds, http_requests_total
from rate_limit import limiter
from routes import (
    admin,
    annotations,
    cold_storage,
    data_admin,
    labels,
    preferences,
    proxy,
    static,
    studies,
)
from routes import auth as auth_routes

# Configure JSON logging before any module-level log lines fire.
configure_logging()

logger = logging.getLogger(__name__)

DIST_DIR = Path(__file__).parent / "dist"

# Alembic lives at the stack root; the web-app runs `upgrade head` at startup.
_ALEMBIC_INI = Path(__file__).resolve().parent.parent / "alembic.ini"


def _init_db():
    """Bring the DB schema up to date, then sync the dynamic labelled tables."""
    from alembic import command
    from alembic.config import Config

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
    from cache_manager import reap_stale_warming, run_eviction

    while True:
        await asyncio.sleep(900)
        try:
            evicted = run_eviction()
            if evicted:
                for uid in evicted:
                    logger.info("eviction_loop: evicted series", extra={"series_uid": uid})
                logger.info(
                    "eviction_loop: removed %d series (sample=%s)",
                    len(evicted), evicted[:10],
                )
        except Exception:
            logger.exception("eviction_loop: cold cache eviction failed")
        try:
            reaped = reap_stale_warming()
            if reaped:
                logger.info(
                    "eviction_loop: reset %d stale-warming series to cold (sample=%s)",
                    len(reaped), reaped[:10],
                )
        except Exception:
            logger.exception("eviction_loop: stale-warming reap failed")


@asynccontextmanager
async def lifespan(application: FastAPI):
    init_pool()
    _init_db()
    # Alembic's env.py calls logging.config.fileConfig() during the startup
    # migration run, which wipes the JSON handler.  Re-install it.
    configure_logging()
    # Post-migration, so the check sees the renamed clinical_data table; after
    # configure_logging(), so a fallback WARN goes through the JSON handler.
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            studies.resolve_clinical_date_column(cur)
    finally:
        conn.close()
    logger.info("startup: effective config", extra=effective_config_summary())
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
        # A fresh process has an empty warm executor and holds no warm locks, so
        # any 'warming'/'queued' row is orphaned (e.g. a restart killed in-flight
        # extractions). Reset them to 'cold' immediately instead of leaving them
        # stuck — and disabled in the UI — until the eviction-loop timeout fires.
        # The reaper's try-advisory-lock guard still spares a warm running in
        # another live process.
        from cache_manager import reap_stale_warming
        try:
            orphaned = reap_stale_warming(min_age_minutes=0)
            if orphaned:
                logger.info(
                    "startup: reset %d orphaned warming/queued studies to cold (sample=%s)",
                    len(orphaned), orphaned[:10],
                )
        except Exception:
            logger.exception("startup: orphaned-warm reset failed")
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
# Rate limiter (the login endpoint decorates itself; slowapi requires the
# limiter on app.state plus the 429 handler below)
# ---------------------------------------------------------------------------

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
    # Content-hashed OHIF assets are static assets too, and are served with a
    # long-lived Cache-Control — a Set-Cookie on them is 54 needless HMAC
    # signings per cold viewer open. The session still slides on /dicom-web
    # frame fetches and /api calls, which run throughout a viewing session.
    if (
        path.startswith("/assets/")
        or path in ("/api/me", "/api/login", "/api/logout")
        or proxy.is_immutable_ohif_asset(path)
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
# Middleware: block app access while the user must change their password
# ---------------------------------------------------------------------------

# Paths the user may still hit while flagged must_change_password=TRUE.
# Anything else is blocked with 403 password_change_required so the UI
# isn't the only enforcer (defends against direct API access too).
_MUST_CHANGE_ALLOWLIST = frozenset({
    "/api/login",
    "/api/logout",
    "/api/me",
    "/api/auth/change-password",
    "/healthz",
    "/metrics",
})


@app.middleware("http")
async def must_change_password_gate(request: Request, call_next):
    path = request.url.path
    if path in _MUST_CHANGE_ALLOWLIST or path.startswith("/assets/"):
        return await call_next(request)
    token = request.cookies.get("auth_token")
    if token:
        payload = decode_jwt(token)
        username = payload.get("sub") if payload else None
        if username:
            conn = get_conn()
            try:
                with conn.cursor() as cur:
                    cur.execute(
                        "SELECT must_change_password FROM users WHERE username = %s",
                        (username,),
                    )
                    row = cur.fetchone()
            finally:
                conn.close()
            if row and row[0]:
                return JSONResponse(
                    status_code=403,
                    content={"detail": "password_change_required"},
                )
    return await call_next(request)


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
# Register routers (order matters — static catch-all must be last)
# ---------------------------------------------------------------------------

app.include_router(auth_routes.router)
app.include_router(preferences.router)
app.include_router(studies.router)
app.include_router(cold_storage.router)
app.include_router(annotations.router)
app.include_router(labels.router)
app.include_router(admin.router)
app.include_router(data_admin.router)
app.include_router(proxy.router)
app.include_router(static.router)
