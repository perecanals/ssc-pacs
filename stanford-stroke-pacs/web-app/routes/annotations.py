"""Annotation CRUD and history endpoints."""

from __future__ import annotations

import logging

import psycopg2.extras
from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException
from pydantic import BaseModel

from auth import get_current_user, get_dataset_scope, require_admin
from common import (
    VALID_LEVELS,
    ensure_patient_access,
    ensure_series_access,
    ensure_study_access,
    record_label_value,
)
from db import get_conn
from labelled_table_sync import sync_labelled_rows

logger = logging.getLogger(__name__)

router = APIRouter()


def _sync_labelled_rows_bg(level: str, entity_id: str | None) -> None:
    """Refresh the *_labelled mirror table for one entity, off the request path.

    Runs after the response is sent, on its own pooled connection — the annotation
    write has already committed, so this reads the latest state and a failure here
    only logs (it can never roll back or block the user's save). The mirror tables
    have no audit trigger, so the absence of `app.audit_user` on this connection is
    irrelevant; audit attribution happens on the in-request annotations write.
    """
    conn = get_conn()
    try:
        sync_labelled_rows(conn, level, [entity_id])
        conn.commit()
    except Exception:
        logger.exception(
            "labelled-table sync failed (level=%s, entity_id=%s)", level, entity_id
        )
        conn.rollback()
    finally:
        conn.close()


class AnnotationCreate(BaseModel):
    level: str = "series"
    seriesinstanceuid: str | None = None
    studyinstanceuid: str | None = None
    patient_id: str | None = None
    label: str
    value: str | None = None
    notes: str | None = None


_UPSERT_SQL = {
    "series": (
        "INSERT INTO annotations "
        "(level, seriesinstanceuid, studyinstanceuid, patient_id, label, value, created_by, notes) "
        "VALUES ('series', %s, %s, %s, %s, %s, %s, %s) "
        "ON CONFLICT (seriesinstanceuid, label) WHERE level = 'series' DO UPDATE "
        "SET value = EXCLUDED.value, "
        "created_by = EXCLUDED.created_by, "
        "notes = COALESCE(EXCLUDED.notes, annotations.notes), "
        "created_at = now() "
        "RETURNING id, level, label, value, created_by, created_at, notes"
    ),
    "study": (
        "INSERT INTO annotations "
        "(level, studyinstanceuid, patient_id, label, value, created_by, notes) "
        "VALUES ('study', %s, %s, %s, %s, %s, %s) "
        "ON CONFLICT (studyinstanceuid, label) WHERE level = 'study' DO UPDATE "
        "SET value = EXCLUDED.value, "
        "created_by = EXCLUDED.created_by, "
        "notes = COALESCE(EXCLUDED.notes, annotations.notes), "
        "created_at = now() "
        "RETURNING id, level, label, value, created_by, created_at, notes"
    ),
    "patient": (
        "INSERT INTO annotations "
        "(level, patient_id, label, value, created_by, notes) "
        "VALUES ('patient', %s, %s, %s, %s, %s) "
        "ON CONFLICT (patient_id, label) WHERE level = 'patient' DO UPDATE "
        "SET value = EXCLUDED.value, "
        "created_by = EXCLUDED.created_by, "
        "notes = COALESCE(EXCLUDED.notes, annotations.notes), "
        "created_at = now() "
        "RETURNING id, level, label, value, created_by, created_at, notes"
    ),
}


@router.get("/api/series/{seriesinstanceuid}/annotations")
def get_annotations(
    seriesinstanceuid: str,
    scope: list[str] | None = Depends(get_dataset_scope),
):
    conn = get_conn()
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            ensure_series_access(cur, seriesinstanceuid, scope)
            cur.execute(
                "SELECT id, level, label, value, created_by, created_at, notes "
                "FROM annotations WHERE seriesinstanceuid = %s "
                "ORDER BY created_at",
                (seriesinstanceuid,),
            )
            return cur.fetchall()
    finally:
        conn.close()


@router.post("/api/annotations", status_code=201)
def create_annotation(
    body: AnnotationCreate,
    background_tasks: BackgroundTasks,
    username: str = Depends(get_current_user),
    scope: list[str] | None = Depends(get_dataset_scope),
):
    if body.level not in VALID_LEVELS:
        raise HTTPException(status_code=400, detail="level must be patient, study, or series")
    conn = get_conn()
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            sql = _UPSERT_SQL[body.level]
            if body.level == "series":
                if not body.seriesinstanceuid:
                    raise HTTPException(status_code=400, detail="seriesinstanceuid required for series-level")
                ensure_series_access(cur, body.seriesinstanceuid, scope)
                params = (
                    body.seriesinstanceuid, body.studyinstanceuid, body.patient_id,
                    body.label, body.value, username, body.notes,
                )
            elif body.level == "study":
                if not body.studyinstanceuid:
                    raise HTTPException(status_code=400, detail="studyinstanceuid required for study-level")
                ensure_study_access(cur, body.studyinstanceuid, scope)
                params = (
                    body.studyinstanceuid, body.patient_id,
                    body.label, body.value, username, body.notes,
                )
            else:
                if not body.patient_id:
                    raise HTTPException(status_code=400, detail="patient_id required for patient-level")
                ensure_patient_access(cur, body.patient_id, scope)
                params = (
                    body.patient_id,
                    body.label, body.value, username, body.notes,
                )
            cur.execute(sql, params)
            row = cur.fetchone()
            # Record the value in the select-label vocabulary so it shows up
            # immediately in the inline dropdown and the column filter. Same
            # transaction as the annotation, so a rolled-back write leaves no
            # orphan vocabulary row. Only for select-type labels.
            if body.value and body.value.strip():
                cur.execute(
                    "SELECT 1 FROM label_definitions "
                    "WHERE name = %s AND datatype = 'select'",
                    (body.label,),
                )
                if cur.fetchone():
                    record_label_value(cur, body.label, body.value, username)
        # Commit the annotation write independently; refresh the labelled mirror
        # table off the request path so it never blocks (or rolls back) the save.
        conn.commit()
        entity_id = body.seriesinstanceuid if body.level == "series" else (
            body.studyinstanceuid if body.level == "study" else body.patient_id
        )
        background_tasks.add_task(_sync_labelled_rows_bg, body.level, entity_id)
        return row
    finally:
        conn.close()


@router.delete("/api/annotations/{annotation_id}", status_code=204)
def delete_annotation(
    annotation_id: int,
    background_tasks: BackgroundTasks,
    scope: list[str] | None = Depends(get_dataset_scope),
):
    conn = get_conn()
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(
                "SELECT id, level, patient_id, studyinstanceuid, seriesinstanceuid "
                "FROM annotations WHERE id = %s",
                (annotation_id,),
            )
            row = cur.fetchone()
            if not row:
                raise HTTPException(status_code=404, detail="Annotation not found")
            if row["level"] == "series":
                ensure_series_access(cur, row["seriesinstanceuid"], scope)
            elif row["level"] == "study":
                ensure_study_access(cur, row["studyinstanceuid"], scope)
            else:
                ensure_patient_access(cur, row["patient_id"], scope)
            cur.execute(
                "DELETE FROM annotations WHERE id = %s", (annotation_id,)
            )
        # Commit the delete independently; refresh the labelled mirror table off the
        # request path. entity_id/level are plain values captured from `row` above,
        # so they survive the connection close in `finally`.
        conn.commit()
        entity_id = row["seriesinstanceuid"] if row["level"] == "series" else (
            row["studyinstanceuid"] if row["level"] == "study" else row["patient_id"]
        )
        background_tasks.add_task(_sync_labelled_rows_bg, row["level"], entity_id)
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Annotation history (admin-only)
# ---------------------------------------------------------------------------

@router.get("/api/annotations/{annotation_id}/history")
def annotation_history(annotation_id: int, user: str = Depends(require_admin)):
    """Return the audit history for a single annotation, newest first."""
    conn = get_conn()
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(
                "SELECT history_id, operation, operation_at, operation_by, "
                "annotation_id, level, entity_id, label, "
                "value_before, value_after, notes_before, notes_after, created_by "
                "FROM annotations_history "
                "WHERE annotation_id = %s "
                "ORDER BY operation_at DESC, history_id DESC",
                (annotation_id,),
            )
            rows = cur.fetchall()
    finally:
        conn.close()
    for r in rows:
        if r["operation_at"]:
            r["operation_at"] = r["operation_at"].isoformat()
    return rows
