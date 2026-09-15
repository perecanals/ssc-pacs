"""Staff/admin module boundary. Register before the SPA fallback."""

import json
import logging
import threading
from dataclasses import dataclass
from uuid import UUID, uuid4

import psycopg2
from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import StreamingResponse
from psycopg2.extras import Json
from pydantic import BaseModel, Field, field_validator

from auth import require_staff
from data_exports import database
from data_exports.access import artifact_in_scope, get_access
from data_exports.exports import SERIALIZATION, ExportWorker, text_value
from data_exports.policy import RELATIONSHIPS
from data_exports.query import build_query, validate_sql
from data_exports.scope import apply_dataset_scope
from data_exports.settings import load_settings
from download_headers import content_disposition, safe_name
from download_response import DownloadResponse

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/data-exports", dependencies=[Depends(require_staff)])
_preview_slots = threading.BoundedSemaphore(2)


class QuerySpec(BaseModel):
    mode: str = "builder"
    builder: dict = Field(default_factory=dict)
    sql: str = Field(default="", max_length=100_000)
    dataset: str | None = Field(default=None, min_length=1, max_length=2000)


class ExportSpec(QuerySpec):
    name: str = Field(min_length=1, max_length=120)
    format: str = "csv"
    source_export_id: UUID | None = None

    @field_validator("name")
    @classmethod
    def required_name(cls, value):
        value = value.strip()
        if not value:
            raise ValueError("Export name is required")
        return value


class ReportSpec(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    configuration: QuerySpec


def start(app):
    settings = load_settings()
    app.state.data_exports_settings = settings
    app.state.data_exports_worker = None
    app.state.data_exports_error = None
    if settings.enabled:
        worker = ExportWorker(settings)
        try:
            database.check_role()
            worker.start()
            app.state.data_exports_worker = worker
        except Exception:
            worker.stop()
            app.state.data_exports_error = "Data Exports is unavailable; check its credentials, grants and spool settings"
            logger.error("Data Exports initialization failed; module unavailable", exc_info=False)


def stop(app):
    if getattr(app.state, "data_exports_worker", None):
        app.state.data_exports_worker.stop()


def available(request: Request):
    if not getattr(request.app.state, "data_exports_settings", None) or not request.app.state.data_exports_settings.enabled:
        raise HTTPException(404, "Data Exports is disabled")
    if request.app.state.data_exports_error:
        raise HTTPException(503, request.app.state.data_exports_error)
    return request.app.state.data_exports_worker


@router.get("/capabilities")
def capabilities(request: Request):
    settings = getattr(request.app.state, "data_exports_settings", None)
    return {"enabled": bool(settings and settings.enabled)}


@router.get("/catalog")
def catalog(worker=Depends(available), access=Depends(get_access)):
    try:
        return {
            "tables": database.catalog(),
            "relationships": RELATIONSHIPS,
            "datasets": database.datasets(access["scope"]),
            "limits": {"retention_hours": worker.settings.retention_hours},
        }
    except psycopg2.Error:
        raise HTTPException(503, "Research database is unavailable")


@router.get("/values")
def values(
    column: str = Query(min_length=1, max_length=256),
    operator: str = Query("eq", pattern="^(eq|ne|in|contains|lt|le|gt|ge)$"),
    search: str = Query("", max_length=2000),
    dataset: str | None = Query(None, min_length=1, max_length=2000),
    worker=Depends(available),
    access=Depends(get_access),
):
    check_dataset(dataset, access["scope"])
    if not _preview_slots.acquire(blocking=False):
        raise HTTPException(429, "Two previews or value lookups are already running; try again shortly")
    try:
        return database.distinct_values(
            column, operator, search, worker.settings.preview_timeout_seconds, dataset, access["scope"]
        )
    except ValueError as exc:
        raise HTTPException(422, str(exc))
    except psycopg2.errors.QueryCanceled:
        raise HTTPException(422, "Value lookup timed out; try a narrower search or enter a value manually")
    except psycopg2.Error:
        raise HTTPException(503, "Existing values are unavailable; you can enter a value manually")
    finally:
        _preview_slots.release()


def check_dataset(dataset, scope):
    if scope is not None and dataset is not None and dataset not in scope:
        raise HTTPException(403, "Dataset access required")


@dataclass(frozen=True)
class PreparedQuery:
    configuration: dict
    sql: str
    parameters: list
    base_sql: str
    base_parameters: list


def prepare(spec, scope=None):
    check_dataset(spec.dataset, scope)
    config = spec.model_dump(include={"mode", "builder", "sql", "dataset"})
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
        base_sql, base_parameters = query, params
        if spec.dataset is not None or scope is not None:
            with database.reader() as conn, conn.cursor() as cur:
                source = cur.mogrify(query, params or None).decode("utf-8")
                literal = cur.mogrify("%s", (spec.dataset,)).decode("utf-8") if spec.dataset is not None else None
                allowed = cur.mogrify("%s", (scope,)).decode("utf-8") if scope is not None else None
            query = apply_dataset_scope(source, literal, allowed)
            params = []
        config["serialization"] = SERIALIZATION
        config["authorized_datasets"] = [spec.dataset] if spec.dataset is not None else scope
        return PreparedQuery(config, query, params, base_sql, base_parameters)
    except ValueError as exc:
        raise HTTPException(422, str(exc))
    except (TypeError, KeyError, AttributeError, RecursionError):
        raise HTTPException(
            422, "Invalid query configuration or unsupported SQL; check the catalog and supported syntax"
        )
    except psycopg2.Error:
        raise HTTPException(503, "Research database is unavailable")


@router.post("/preview")
def preview(
    spec: QuerySpec, offset: int = Query(0, ge=0, le=100_000), worker=Depends(available), access=Depends(get_access)
):
    if not _preview_slots.acquire(blocking=False):
        raise HTTPException(429, "Two previews are already running; try again shortly")
    try:
        prepared = prepare(spec, access["scope"])
        with database.reader(worker.settings.preview_timeout_seconds) as conn, conn.cursor() as cur:
            base_sql = cur.mogrify(prepared.base_sql, prepared.base_parameters or None).decode("utf-8")
            equivalent = cur.mogrify(prepared.sql, prepared.parameters or None).decode("utf-8")
            cur.execute(f"SELECT * FROM ({equivalent}) AS data_exports_preview LIMIT 201 OFFSET {offset}")
            rows = cur.fetchall()
            # Text serialization preserves decimal precision and structured values.
            return {
                "columns": [c.name for c in cur.description],
                "rows": [[None if v is None else text_value(v) for v in row] for row in rows[:200]],
                "has_more": len(rows) > 200,
                "sql": equivalent,
                "base_sql": base_sql,
                "offset": offset,
            }
    except psycopg2.errors.QueryCanceled:
        raise HTTPException(422, "Preview timed out; narrow the query")
    except psycopg2.Error:
        raise HTTPException(422, "Query could not run; check column names, data types and SQL syntax")
    finally:
        _preview_slots.release()


@router.get("/reports")
def reports(worker=Depends(available), access=Depends(get_access)):
    return database.records(
        "SELECT * FROM data_exports_reports WHERE (%s OR created_by=%s) ORDER BY name, id",
        (access["scope"] is None, access["user"]),
    )


@router.post("/reports", status_code=201)
def create_report(
    spec: ReportSpec, user: str = Depends(require_staff), worker=Depends(available), access=Depends(get_access)
):
    config = prepare(spec.configuration, access["scope"]).configuration
    if not spec.name.strip():
        raise HTTPException(422, "Report name is required")
    return database.records(
        "INSERT INTO data_exports_reports (id,name,configuration,created_by,updated_by) "
        "VALUES (%s,%s,%s,%s,%s) RETURNING *",
        (str(uuid4()), spec.name.strip(), Json(config), user, user),
        one=True,
    )


@router.put("/reports/{report_id}")
def update_report(
    report_id: UUID,
    spec: ReportSpec,
    user: str = Depends(require_staff),
    worker=Depends(available),
    access=Depends(get_access),
):
    config = prepare(spec.configuration, access["scope"]).configuration
    if not spec.name.strip():
        raise HTTPException(422, "Report name is required")
    row = database.records(
        "UPDATE data_exports_reports SET name=%s, configuration=%s, updated_by=%s, updated_at=now() "
        "WHERE id=%s AND (%s OR created_by=%s) RETURNING *",
        (spec.name.strip(), Json(config), user, str(report_id), access["scope"] is None, user),
        one=True,
    )
    if not row:
        raise HTTPException(404, "Report not found")
    return row


@router.delete("/reports/{report_id}")
def delete_report(report_id: UUID, worker=Depends(available), access=Depends(get_access)):
    row = database.records(
        "DELETE FROM data_exports_reports WHERE id=%s AND (%s OR created_by=%s) RETURNING id",
        (str(report_id), access["scope"] is None, access["user"]),
        one=True,
    )
    if not row:
        raise HTTPException(404, "Report not found")
    return {"ok": True}


@router.post("/exports", status_code=202)
def submit(spec: ExportSpec, user: str = Depends(require_staff), worker=Depends(available), access=Depends(get_access)):
    if spec.format not in ("csv", "xlsx"):
        raise HTTPException(422, "Choose CSV or XLSX")
    if spec.source_export_id is not None:
        get_export(spec.source_export_id, access)
    prepared = prepare(spec, access["scope"])
    config = prepared.configuration
    if spec.source_export_id is not None:
        config["source_export_id"] = str(spec.source_export_id)
    # Serialize admission with a transaction lock; prevent queue-limit races.
    conn = database.get_conn()
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("SELECT pg_advisory_xact_lock(782341,22)")
            cur.execute("SELECT count(*) AS n FROM data_exports_jobs WHERE status='queued'")
            if cur.fetchone()["n"] >= worker.settings.queue_limit:
                raise HTTPException(429, "Export queue is full; try again later")
            cur.execute(
                "INSERT INTO data_exports_jobs (id,name,username,configuration,sql,parameters,format,status) "
                "VALUES (%s,%s,%s,%s,%s,%s,%s,'queued') RETURNING *",
                (str(uuid4()), spec.name, user, Json(config), prepared.sql, Json(prepared.parameters), spec.format),
            )
            row = cur.fetchone()
        conn.commit()
        return row
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


@router.get("/exports")
def history(
    offset: int = Query(0, ge=0),
    dataset: str | None = Query(None, min_length=1, max_length=2000),
    worker=Depends(available),
    access=Depends(get_access),
):
    return database.records(
        "SELECT * FROM data_exports_jobs WHERE (%s IS NULL OR configuration->>'dataset'=%s) "
        "AND (%s OR username=%s) ORDER BY created_at DESC, id LIMIT 50 OFFSET %s",
        (dataset, dataset, access["scope"] is None, access["user"], offset),
    )


def get_export(job_id, access):
    row = database.records(
        "SELECT * FROM data_exports_jobs WHERE id=%s AND (%s OR username=%s)",
        (str(job_id), access["scope"] is None, access["user"]),
        one=True,
    )
    if not row:
        raise HTTPException(404, "Export not found")
    return row


@router.get("/exports/{job_id}")
def export_status(job_id: UUID, worker=Depends(available), access=Depends(get_access)):
    row = get_export(job_id, access)
    row["downloads"] = database.records(
        "SELECT username, requested_at FROM data_exports_downloads WHERE export_id=%s ORDER BY requested_at DESC",
        (str(job_id),),
    )
    with database.reader() as conn, conn.cursor() as cur:
        row["equivalent_sql"] = cur.mogrify(row["sql"], row["parameters"] or None).decode("utf-8")
    return row


@router.post("/exports/{job_id}/cancel")
def cancel(job_id: UUID, worker=Depends(available), access=Depends(get_access)):
    get_export(job_id, access)
    row = database.records(
        "UPDATE data_exports_jobs SET cancel_requested=true, "
        "status=CASE WHEN status='queued' THEN 'cancelled' ELSE status END, "
        "finished_at=CASE WHEN status='queued' THEN now() ELSE finished_at END "
        "WHERE id=%s AND status IN ('queued','running') RETURNING id",
        (str(job_id),),
        one=True,
    )
    if not row:
        raise HTTPException(409, "Export is no longer queued or running")
    worker.cancel(job_id)
    return {"ok": True}


@router.get("/exports/{job_id}/download")
def download(job_id: UUID, user: str = Depends(require_staff), worker=Depends(available), access=Depends(get_access)):
    owned = get_export(job_id, access)
    if not artifact_in_scope(owned, access["scope"]):
        raise HTTPException(403, "Dataset access has changed; create a new export")
    job = database.records(
        "SELECT * FROM data_exports_jobs WHERE id=%s AND status='completed' AND expires_at>now()",
        (str(job_id),),
        one=True,
    )
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
        database.records("INSERT INTO data_exports_downloads (export_id,username) VALUES (%s,%s)", (str(job_id), user))
    except Exception:
        handle.close()
        raise
    response = StreamingResponse(
        iter(lambda: handle.read(1024 * 1024), b""),
        media_type="text/csv"
        if job["format"] == "csv"
        else "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={
            "Cache-Control": "no-store",
            "X-Content-Type-Options": "nosniff",
            "Content-Length": str(job["file_size"]),
            "Content-Disposition": content_disposition(f"{safe_name(job['name'])}-{job_id}.{job['format']}"),
        },
    )
    return DownloadResponse(response, handle.close)
