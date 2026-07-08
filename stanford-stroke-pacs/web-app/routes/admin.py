"""Admin / observability endpoints: /healthz, /metrics, reconciliation."""

from __future__ import annotations

import json
import os
import shutil
from pathlib import Path

import psycopg2
import psycopg2.extras
from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel

import dataset_access
from auth import require_admin
from config import DICOM_DATA_ROOT, STORAGE_MODE
from db import DB_CONFIG, get_conn
from orthanc_client import orthanc_system_check

router = APIRouter()

_GIT_SHA: str | None = None
# Outer git root (ssc-pacs/), four levels up from routes/admin.py:
# routes/ -> web-app/ -> stanford-stroke-pacs/ -> ssc-pacs/
_GIT_ROOT = Path(__file__).resolve().parent.parent.parent.parent


def _resolve_git_sha() -> str:
    global _GIT_SHA
    if _GIT_SHA is not None:
        return _GIT_SHA
    head = _GIT_ROOT / ".git" / "HEAD"
    try:
        if head.is_file():
            ref = head.read_text().strip()
            if ref.startswith("ref: "):
                ref_path = _GIT_ROOT / ".git" / ref[5:]
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
        p = DICOM_DATA_ROOT
        while not p.exists():
            if p.parent == p:
                break
            p = p.parent
        du = shutil.disk_usage(p)
        body["disk_free_percent_dicom_data_root"] = round(du.free * 100.0 / du.total, 1)
        body["disk_free_bytes_dicom_data_root"] = int(du.free)
    except Exception as e:
        body["disk_free_percent_dicom_data_root"] = None
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

    refresh_cold_storage_gauges(get_conn, DICOM_DATA_ROOT)
    return _Response(
        content=generate_latest(METRICS_REGISTRY),
        media_type=CONTENT_TYPE_LATEST,
    )


# ---------------------------------------------------------------------------
# Reconciliation report (admin-only)
# ---------------------------------------------------------------------------

_REPORTS_DIR = _GIT_ROOT / "maintenance" / "reconciliation-reports"


@router.get("/api/admin/reconciliation/latest")
def reconciliation_latest(user: str = Depends(require_admin)):
    """Return the most recent reconciliation JSON report.  Admin-only."""
    if not _REPORTS_DIR.is_dir():
        return JSONResponse(
            status_code=404,
            content={"detail": "No reconciliation reports found"},
        )
    reports = sorted(_REPORTS_DIR.glob("*.json"), key=lambda p: p.name)
    if not reports:
        return JSONResponse(
            status_code=404,
            content={"detail": "No reconciliation reports found"},
        )
    latest = reports[-1]
    return json.loads(latest.read_text())


# ---------------------------------------------------------------------------
# User dataset permissions (admin-only)
# ---------------------------------------------------------------------------


class DatasetGrants(BaseModel):
    datasets: list[str]


def _serialize_user_row(row: dict) -> dict:
    if row.get("created_at"):
        row["created_at"] = row["created_at"].isoformat()
    row["allowed_datasets"] = sorted(row.get("allowed_datasets") or [])
    return row


@router.get("/api/admin/users")
def list_users(user: str = Depends(require_admin)):
    """All users with their dataset grants, for the /admin permissions page."""
    conn = get_conn()
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(
                "SELECT username, is_admin, allowed_datasets, created_at "
                "FROM users ORDER BY username"
            )
            return [_serialize_user_row(r) for r in cur.fetchall()]
    finally:
        conn.close()


@router.put("/api/admin/users/{username}/datasets")
def set_user_datasets(
    username: str,
    body: DatasetGrants,
    admin: str = Depends(require_admin),
):
    """Replace a user's dataset grants.

    Values must be existing `patient.dataset` tags (422 otherwise — catches
    typos; grant-ahead-of-ingest is a script-only affordance). Invalidates
    the proxy's cached scope so the change applies immediately.
    """
    datasets = sorted({d.strip() for d in body.datasets if d.strip()})
    conn = get_conn()
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(
                "SELECT DISTINCT unnest(dataset) AS ds FROM patient"
            )
            known = {r["ds"] for r in cur.fetchall()}
            unknown = [d for d in datasets if d not in known]
            if unknown:
                raise HTTPException(
                    status_code=422,
                    detail=f"Unknown dataset(s): {', '.join(unknown)}",
                )
            cur.execute(
                "UPDATE users SET allowed_datasets = %s::text[] "
                "WHERE username = %s "
                "RETURNING username, is_admin, allowed_datasets, created_at",
                (datasets, username),
            )
            row = cur.fetchone()
            if row is None:
                raise HTTPException(status_code=404, detail="User not found")
        conn.commit()
    finally:
        conn.close()
    dataset_access.invalidate_user_scope(username)
    return _serialize_user_row(row)
