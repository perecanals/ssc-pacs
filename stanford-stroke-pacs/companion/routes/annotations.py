"""Annotation CRUD endpoints."""

from __future__ import annotations

import psycopg2.extras
from fastapi import APIRouter, Cookie, HTTPException
from pydantic import BaseModel

from auth import get_current_user
from common import VALID_LEVELS
from db import get_conn
from labelled_table_sync import sync_labelled_rows

router = APIRouter()


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
def get_annotations(seriesinstanceuid: str):
    conn = get_conn()
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
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
def create_annotation(body: AnnotationCreate, auth_token: str | None = Cookie(None)):
    username = get_current_user(auth_token)
    if body.level not in VALID_LEVELS:
        raise HTTPException(status_code=400, detail="level must be patient, study, or series")
    conn = get_conn()
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            sql = _UPSERT_SQL[body.level]
            if body.level == "series":
                if not body.seriesinstanceuid:
                    raise HTTPException(status_code=400, detail="seriesinstanceuid required for series-level")
                params = (
                    body.seriesinstanceuid, body.studyinstanceuid, body.patient_id,
                    body.label, body.value, username, body.notes,
                )
            elif body.level == "study":
                if not body.studyinstanceuid:
                    raise HTTPException(status_code=400, detail="studyinstanceuid required for study-level")
                params = (
                    body.studyinstanceuid, body.patient_id,
                    body.label, body.value, username, body.notes,
                )
            else:
                if not body.patient_id:
                    raise HTTPException(status_code=400, detail="patient_id required for patient-level")
                params = (
                    body.patient_id,
                    body.label, body.value, username, body.notes,
                )
            cur.execute(sql, params)
            row = cur.fetchone()
        entity_id = body.seriesinstanceuid if body.level == "series" else (
            body.studyinstanceuid if body.level == "study" else body.patient_id
        )
        sync_labelled_rows(conn, body.level, [entity_id])
        conn.commit()
        return row
    finally:
        conn.close()


@router.delete("/api/annotations/{annotation_id}", status_code=204)
def delete_annotation(annotation_id: int, auth_token: str | None = Cookie(None)):
    get_current_user(auth_token)
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
            cur.execute(
                "DELETE FROM annotations WHERE id = %s", (annotation_id,)
            )
        entity_id = row["seriesinstanceuid"] if row["level"] == "series" else (
            row["studyinstanceuid"] if row["level"] == "study" else row["patient_id"]
        )
        sync_labelled_rows(conn, row["level"], [entity_id])
        conn.commit()
    finally:
        conn.close()
