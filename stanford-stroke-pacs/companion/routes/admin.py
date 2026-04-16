"""Admin / observability endpoints: /healthz, /metrics."""

from __future__ import annotations

import os
import shutil
from pathlib import Path

import psycopg2
from fastapi import APIRouter
from fastapi.responses import JSONResponse

from config import LEGACY_DICOM_ROOT, STORAGE_MODE
from db import DB_CONFIG, get_conn
from orthanc_client import orthanc_system_check

router = APIRouter()

_GIT_SHA: str | None = None
_ROOT_DIR = Path(__file__).resolve().parent.parent.parent


def _resolve_git_sha() -> str:
    global _GIT_SHA
    if _GIT_SHA is not None:
        return _GIT_SHA
    head = _ROOT_DIR / ".git" / "HEAD"
    try:
        if head.is_file():
            ref = head.read_text().strip()
            if ref.startswith("ref: "):
                ref_path = _ROOT_DIR / ".git" / ref[5:]
                if ref_path.is_file():
                    _GIT_SHA = ref_path.read_text().strip()[:12]
                    return _GIT_SHA
            if len(ref) >= 7:
                _GIT_SHA = ref[:12]
                return _GIT_SHA
    except OSError:
        pass
    _GIT_SHA = "unknown"
    return _GIT_SHA


def _check_db(dsn_kwargs: dict) -> tuple[str, str | None]:
    try:
        conn = psycopg2.connect(connect_timeout=3, **dsn_kwargs)
        try:
            with conn.cursor() as cur:
                cur.execute("SELECT 1")
                cur.fetchone()
        finally:
            conn.close()
        return "ok", None
    except Exception as e:
        return "error", str(e)[:200]


def _orthanc_db_kwargs() -> dict | None:
    """Build connect kwargs for `orthanc_db` if PG_ORTHANC_* are configured."""
    user = os.getenv("PG_ORTHANC_USER")
    password = os.getenv("PG_ORTHANC_PASSWORD")
    dbname = os.getenv("PG_ORTHANC_DB", "orthanc_db")
    if not user or not password:
        return None
    return dict(
        host=DB_CONFIG["host"],
        port=DB_CONFIG["port"],
        dbname=dbname,
        user=user,
        password=password,
    )


@router.get("/healthz")
def healthz():
    """Liveness + dependency check.

    Returns 200 when every critical check passes, 503 otherwise.
    """
    body: dict[str, object] = {"status": "ok", "version": _resolve_git_sha()}

    db_status, db_err = _check_db(DB_CONFIG)
    body["db_stanford_stroke"] = db_status
    if db_err:
        body["db_stanford_stroke_error"] = db_err

    o_kwargs = _orthanc_db_kwargs()
    if o_kwargs is not None:
        o_status, o_err = _check_db(o_kwargs)
        body["db_orthanc"] = o_status
        if o_err:
            body["db_orthanc_error"] = o_err
    else:
        body["db_orthanc"] = "unconfigured"

    orthanc_status, orthanc_err = orthanc_system_check()
    body["orthanc_api"] = orthanc_status
    if orthanc_err:
        body["orthanc_api_error"] = orthanc_err

    try:
        p = LEGACY_DICOM_ROOT
        while not p.exists():
            if p.parent == p:
                break
            p = p.parent
        du = shutil.disk_usage(p)
        body["disk_free_percent_legacy_dicom_root"] = round(du.free * 100.0 / du.total, 1)
        body["disk_free_bytes_legacy_dicom_root"] = int(du.free)
    except Exception as e:
        body["disk_free_percent_legacy_dicom_root"] = None
        body["disk_error"] = str(e)[:200]

    critical_ok = body["db_stanford_stroke"] == "ok"
    if STORAGE_MODE == "cold_path_cache":
        critical_ok = critical_ok and body["orthanc_api"] == "ok"

    if not critical_ok:
        body["status"] = "degraded"
        return JSONResponse(status_code=503, content=body)
    return body


@router.get("/metrics")
def metrics_endpoint():
    """Prometheus exposition — unauthenticated, same as /healthz."""
    from fastapi.responses import Response as _Response
    from prometheus_client import CONTENT_TYPE_LATEST, generate_latest

    from metrics import REGISTRY as METRICS_REGISTRY
    from metrics import refresh_cold_storage_gauges

    refresh_cold_storage_gauges(get_conn, LEGACY_DICOM_ROOT)
    return _Response(
        content=generate_latest(METRICS_REGISTRY),
        media_type=CONTENT_TYPE_LATEST,
    )
