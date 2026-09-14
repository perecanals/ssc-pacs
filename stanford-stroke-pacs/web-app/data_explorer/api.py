"""Admin-only module boundary. Register before the SPA fallback."""
import json
import logging
import threading
from uuid import UUID, uuid4

import psycopg2
from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import StreamingResponse
from psycopg2.extras import Json
from pydantic import BaseModel, Field
from starlette.background import BackgroundTask

from auth import require_admin
from data_explorer import database
from data_explorer.exports import SERIALIZATION, ExportWorker
from data_explorer.policy import RELATIONSHIPS
from data_explorer.query import build_query, validate_sql
from data_explorer.settings import load_settings

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/data-explorer", dependencies=[Depends(require_admin)])
_preview_slots = threading.BoundedSemaphore(2)


class QuerySpec(BaseModel):
    mode: str = "builder"
    builder: dict = Field(default_factory=dict)
    sql: str = Field(default="", max_length=100_000)


class ExportSpec(QuerySpec):
    format: str = "csv"


class ReportSpec(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    configuration: QuerySpec


def start(app):
    settings = load_settings()
    app.state.explorer_settings = settings
    app.state.explorer_worker = None
    app.state.explorer_error = None
    if settings.enabled:
        worker = ExportWorker(settings)
        try:
            database.check_role()
            worker.start()
            app.state.explorer_worker = worker
        except Exception:
            worker.stop()
            app.state.explorer_error = "Data Explorer is unavailable; check its credentials, grants and spool settings"
            logger.error("Data Explorer initialization failed; module unavailable", exc_info=False)


def stop(app):
    if getattr(app.state, "explorer_worker", None):
        app.state.explorer_worker.stop()


def available(request: Request):
    if not getattr(request.app.state, "explorer_settings", None) or not request.app.state.explorer_settings.enabled:
        raise HTTPException(404, "Data Explorer is disabled")
    if request.app.state.explorer_error:
        raise HTTPException(503, request.app.state.explorer_error)
    return request.app.state.explorer_worker


@router.get("/capabilities")
def capabilities(request: Request):
    settings = getattr(request.app.state, "explorer_settings", None)
    return {"enabled": bool(settings and settings.enabled)}


@router.get("/catalog")
def catalog(worker=Depends(available)):
    try:
        return {"tables": database.catalog(), "relationships": RELATIONSHIPS,
                "limits": {"retention_hours": worker.settings.retention_hours}}
    except psycopg2.Error:
        raise HTTPException(503, "Research database is unavailable")


def prepare(spec):
    config = spec.model_dump(include={"mode", "builder", "sql"})
    if len(json.dumps(config)) > 100_000:
        raise HTTPException(422, "Query configuration is too large")
    try:
        tables = database.catalog()
        if spec.mode == "builder":
            query, params = build_query(spec.builder, tables)
        elif spec.mode == "sql":
            query = validate_sql(spec.sql, {t["name"] for t in tables})
            params = []
            config["sql"] = query
        else:
            raise ValueError("Choose builder or SQL mode")
        config["serialization"] = SERIALIZATION
        return config, query, params
    except ValueError as exc:
        raise HTTPException(422, str(exc))
    except (TypeError, KeyError, AttributeError, RecursionError):
        raise HTTPException(422, "Invalid query configuration or unsupported SQL; check the catalog and supported syntax")
    except psycopg2.Error:
        raise HTTPException(503, "Research database is unavailable")


@router.post("/preview")
def preview(spec: QuerySpec, offset: int = Query(0, ge=0, le=100_000), worker=Depends(available)):
    if not _preview_slots.acquire(blocking=False):
        raise HTTPException(429, "Two previews are already running; try again shortly")
    try:
        config, query, params = prepare(spec)
        with database.reader(worker.settings.preview_timeout_seconds) as conn, conn.cursor() as cur:
            equivalent = cur.mogrify(query, params or None).decode("utf-8")
            cur.execute(f"SELECT * FROM ({equivalent}) AS explorer_preview LIMIT 201 OFFSET {offset}")
            rows = cur.fetchall()
            # Arrays/JSON remain native in preview, decimal values remain exact.
            from data_explorer.exports import text_value
            return {"columns": [c.name for c in cur.description],
                    "rows": [[None if v is None else text_value(v) for v in row] for row in rows[:200]],
                    "has_more": len(rows) > 200, "sql": equivalent, "offset": offset}
    except psycopg2.errors.QueryCanceled:
        raise HTTPException(422, "Preview timed out; narrow the query")
    except psycopg2.Error:
        raise HTTPException(422, "Query could not run; check column names, data types and SQL syntax")
    finally:
        _preview_slots.release()


@router.get("/reports")
def reports(worker=Depends(available)):
    return database.records("SELECT * FROM explorer_reports ORDER BY name, id")


@router.post("/reports", status_code=201)
def create_report(spec: ReportSpec, user: str = Depends(require_admin), worker=Depends(available)):
    config, _, _ = prepare(spec.configuration)
    if not spec.name.strip():
        raise HTTPException(422, "Report name is required")
    return database.records("INSERT INTO explorer_reports (id,name,configuration,created_by,updated_by) "
                            "VALUES (%s,%s,%s,%s,%s) RETURNING *",
                            (str(uuid4()), spec.name.strip(), Json(config), user, user), one=True)


@router.put("/reports/{report_id}")
def update_report(report_id: UUID, spec: ReportSpec, user: str = Depends(require_admin), worker=Depends(available)):
    config, _, _ = prepare(spec.configuration)
    if not spec.name.strip():
        raise HTTPException(422, "Report name is required")
    row = database.records("UPDATE explorer_reports SET name=%s, configuration=%s, updated_by=%s, updated_at=now() "
                           "WHERE id=%s RETURNING *", (spec.name.strip(), Json(config), user, str(report_id)), one=True)
    if not row:
        raise HTTPException(404, "Report not found")
    return row


@router.delete("/reports/{report_id}")
def delete_report(report_id: UUID, worker=Depends(available)):
    row = database.records("DELETE FROM explorer_reports WHERE id=%s RETURNING id", (str(report_id),), one=True)
    if not row:
        raise HTTPException(404, "Report not found")
    return {"ok": True}


@router.post("/exports", status_code=202)
def submit(spec: ExportSpec, user: str = Depends(require_admin), worker=Depends(available)):
    if spec.format not in ("csv", "xlsx"):
        raise HTTPException(422, "Choose CSV or XLSX")
    config, query, params = prepare(spec)
    # Serialize admission with a transaction lock; prevent queue-limit races.
    conn = database.get_conn()
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("SELECT pg_advisory_xact_lock(782341,22)")
            cur.execute("SELECT count(*) AS n FROM explorer_exports WHERE status='queued'")
            if cur.fetchone()["n"] >= worker.settings.queue_limit:
                raise HTTPException(429, "Export queue is full; try again later")
            cur.execute("INSERT INTO explorer_exports (id,username,configuration,sql,parameters,format,status) "
                        "VALUES (%s,%s,%s,%s,%s,%s,'queued') RETURNING *",
                        (str(uuid4()), user, Json(config), query, Json(params), spec.format))
            row = cur.fetchone()
        conn.commit()
        return row
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


@router.get("/exports")
def history(offset: int = Query(0, ge=0), worker=Depends(available)):
    return database.records("SELECT * FROM explorer_exports ORDER BY created_at DESC, id LIMIT 50 OFFSET %s", (offset,))


@router.get("/exports/{job_id}")
def export_status(job_id: UUID, worker=Depends(available)):
    row = database.records("SELECT * FROM explorer_exports WHERE id=%s", (str(job_id),), one=True)
    if not row:
        raise HTTPException(404, "Export not found")
    row["downloads"] = database.records("SELECT username, requested_at FROM explorer_downloads "
                                        "WHERE export_id=%s ORDER BY requested_at DESC", (str(job_id),))
    with database.reader() as conn, conn.cursor() as cur:
        row["equivalent_sql"] = cur.mogrify(row["sql"], row["parameters"] or None).decode("utf-8")
    return row


@router.post("/exports/{job_id}/cancel")
def cancel(job_id: UUID, worker=Depends(available)):
    row = database.records("UPDATE explorer_exports SET cancel_requested=true, "
                           "status=CASE WHEN status='queued' THEN 'cancelled' ELSE status END, "
                           "finished_at=CASE WHEN status='queued' THEN now() ELSE finished_at END "
                           "WHERE id=%s AND status IN ('queued','running') RETURNING id", (str(job_id),), one=True)
    if not row:
        raise HTTPException(409, "Export is no longer queued or running")
    worker.cancel(job_id)
    return {"ok": True}


@router.get("/exports/{job_id}/download")
def download(job_id: UUID, user: str = Depends(require_admin), worker=Depends(available)):
    job = database.records("SELECT * FROM explorer_exports WHERE id=%s AND status='completed' AND expires_at>now()",
                           (str(job_id),), one=True)
    if not job:
        raise HTTPException(410, "Export is not available; rerun it from history")
    path = worker.root / str(job_id) / ("export." + job["format"])
    if not path.is_file():
        raise HTTPException(410, "Export file is no longer available; rerun it from history")
    # Open before recording the request so expiration cleanup cannot race the
    # response's later file open. Unix keeps this descriptor valid after unlink.
    try:
        handle = path.open("rb")
    except FileNotFoundError:
        raise HTTPException(410, "Export file expired; rerun it from history")
    try:
        database.records("INSERT INTO explorer_downloads (export_id,username) VALUES (%s,%s)", (str(job_id), user))
    except Exception:
        handle.close()
        raise
    def chunks():
        try:
            while chunk := handle.read(1024 * 1024):
                yield chunk
        finally:
            handle.close()
    return StreamingResponse(chunks(), background=BackgroundTask(handle.close),
                             media_type="text/csv" if job["format"] == "csv" else
                             "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                             headers={"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff",
                                      "Content-Length": str(job["file_size"]),
                                      "Content-Disposition": f'attachment; filename="research-export-{job_id}.{job["format"]}"'})
