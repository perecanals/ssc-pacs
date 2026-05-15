"""Label definitions, label listing, and label summary endpoints."""

from __future__ import annotations

import json

import psycopg2
import psycopg2.extras
import psycopg2.sql as psql
from fastapi import APIRouter, Cookie, HTTPException, Query
from pydantic import BaseModel

from auth import get_current_user
from common import LABEL_NAME_RE, PATIENT_ID_COL, VALID_LEVELS
from db import get_conn
from labelled_table_sync import (
    find_label_column_conflict,
    rebuild_labelled_tables,
    sync_labelled_schema,
)

router = APIRouter()


# ---------------------------------------------------------------------------
# Labels (from annotations table)
# ---------------------------------------------------------------------------


@router.get("/api/labels")
def list_labels(level: str | None = Query(None)):
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            if level and level in VALID_LEVELS:
                cur.execute(
                    "SELECT DISTINCT label FROM annotations WHERE level = %s ORDER BY label",
                    (level,),
                )
            else:
                cur.execute("SELECT DISTINCT label FROM annotations ORDER BY label")
            return [r[0] for r in cur.fetchall()]
    finally:
        conn.close()


_SUMMARY_COUNT_COL = {
    "patient": "patient_id",
    "study": "studyinstanceuid",
    "series": "seriesinstanceuid",
}


@router.get("/api/labels/summary")
def labels_summary(level: str | None = Query(None)):
    conn = get_conn()
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            if level and level in VALID_LEVELS:
                count_col = _SUMMARY_COUNT_COL[level]
                cur.execute(
                    f"SELECT a.label, a.level, COUNT(DISTINCT a.{count_col}) AS count, "
                    "ld.instrument "
                    "FROM annotations a "
                    "LEFT JOIN label_definitions ld "
                    "  ON ld.name = a.label AND ld.level = a.level "
                    "WHERE a.level = %s "
                    "GROUP BY a.label, a.level, ld.instrument "
                    "ORDER BY a.label",
                    (level,),
                )
            else:
                cur.execute(
                    "SELECT a.label, a.level, COUNT(*) AS count, ld.instrument "
                    "FROM annotations a "
                    "LEFT JOIN label_definitions ld "
                    "  ON ld.name = a.label AND ld.level = a.level "
                    "GROUP BY a.label, a.level, ld.instrument "
                    "ORDER BY a.label"
                )
            return cur.fetchall()
    finally:
        conn.close()


@router.get("/api/labels/{label_name}/values")
def get_label_values(label_name: str):
    """Return the distinct annotation values already used for a label."""
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT DISTINCT value FROM annotations "
                "WHERE label = %s AND value IS NOT NULL AND value != '' "
                "ORDER BY value",
                (label_name,),
            )
            return [r[0] for r in cur.fetchall()]
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Label definitions
# ---------------------------------------------------------------------------


_LABEL_DEF_COLUMNS = (
    "id, name, description, level, datatype, options, instrument, "
    "created_by, created_at"
)


def _clean_optional_text(value: str | None) -> str | None:
    """Normalize a free-text optional field: trim, treat empty as NULL."""
    if value is None:
        return None
    stripped = value.strip()
    return stripped or None


def _serialize_label_def_row(row: dict) -> dict:
    if row.get("created_at"):
        row["created_at"] = row["created_at"].isoformat()
    row["options"] = json.loads(row["options"]) if row.get("options") else []
    return row


class LabelDefinitionCreate(BaseModel):
    name: str
    description: str | None = None
    level: str = "series"
    datatype: str = "bool"
    options: list[str] | None = None
    instrument: str | None = None


class LabelDefinitionUpdate(BaseModel):
    description: str | None = None
    instrument: str | None = None


@router.get("/api/label-definitions")
def list_label_definitions(level: str | None = Query(None)):
    conn = get_conn()
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            if level and level in VALID_LEVELS:
                cur.execute(
                    f"SELECT {_LABEL_DEF_COLUMNS} "
                    "FROM label_definitions WHERE level = %s ORDER BY name",
                    (level,),
                )
            else:
                cur.execute(
                    f"SELECT {_LABEL_DEF_COLUMNS} "
                    "FROM label_definitions ORDER BY name"
                )
            return [_serialize_label_def_row(r) for r in cur.fetchall()]
    finally:
        conn.close()


@router.post("/api/label-definitions", status_code=201)
def create_label_definition(body: LabelDefinitionCreate, auth_token: str | None = Cookie(None)):
    username = get_current_user(auth_token)
    if body.datatype not in ("bool", "int", "text", "select"):
        raise HTTPException(status_code=400, detail="datatype must be bool, int, text, or select")
    if body.level not in VALID_LEVELS:
        raise HTTPException(status_code=400, detail="level must be patient, study, or series")
    if not LABEL_NAME_RE.match((body.name or "").strip()):
        raise HTTPException(
            status_code=400,
            detail="name must match ^[A-Za-z][A-Za-z0-9_]{0,62}$ (letters, digits, underscores; must start with a letter; max 63 chars)",
        )
    options_json = json.dumps(body.options) if body.options else None
    instrument = _clean_optional_text(body.instrument)
    conn = get_conn()
    try:
        conflict = find_label_column_conflict(conn, body.level, body.name.strip())
        if conflict:
            raise HTTPException(
                status_code=409,
                detail=f"Label name conflicts with existing column generated from '{conflict}'",
            )
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(
                "INSERT INTO label_definitions "
                "(name, description, level, datatype, options, instrument, created_by) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s) "
                f"RETURNING {_LABEL_DEF_COLUMNS}",
                (
                    body.name.strip(),
                    _clean_optional_text(body.description),
                    body.level,
                    body.datatype,
                    options_json,
                    instrument,
                    username,
                ),
            )
            row = _serialize_label_def_row(cur.fetchone())
        sync_labelled_schema(conn, body.level)
        conn.commit()
        return row
    except psycopg2.errors.UniqueViolation:
        raise HTTPException(status_code=409, detail="Label with this name already exists")
    finally:
        conn.close()


@router.patch("/api/label-definitions/{label_id}")
def update_label_definition(
    label_id: int,
    body: LabelDefinitionUpdate,
    auth_token: str | None = Cookie(None),
):
    """Edit `description` and/or `instrument` on an existing label.

    Editing `name`, `level`, `datatype`, or `options` is intentionally
    out of scope — those are baked into the labelled-table sync and
    annotation entity-id constraints; renaming/retyping belongs in a
    dedicated migration flow.
    """
    get_current_user(auth_token)

    updates: list[str] = []
    params: list[object] = []
    fields = body.model_dump(exclude_unset=True)
    if "description" in fields:
        updates.append("description = %s")
        params.append(_clean_optional_text(fields["description"]))
    if "instrument" in fields:
        updates.append("instrument = %s")
        params.append(_clean_optional_text(fields["instrument"]))

    if not updates:
        raise HTTPException(
            status_code=400,
            detail="No editable fields provided (allowed: description, instrument)",
        )

    params.append(label_id)
    conn = get_conn()
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(
                f"UPDATE label_definitions SET {', '.join(updates)} "
                f"WHERE id = %s RETURNING {_LABEL_DEF_COLUMNS}",
                params,
            )
            row = cur.fetchone()
            if row is None:
                raise HTTPException(status_code=404, detail="Label definition not found")
            row = _serialize_label_def_row(row)
        conn.commit()
        return row
    finally:
        conn.close()


@router.get("/api/instruments")
def list_instruments():
    """Distinct non-null instrument values from label_definitions with counts."""
    conn = get_conn()
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(
                "SELECT instrument AS name, COUNT(*) AS count "
                "FROM label_definitions "
                "WHERE instrument IS NOT NULL AND instrument <> '' "
                "GROUP BY instrument "
                "ORDER BY count DESC, instrument ASC"
            )
            return cur.fetchall()
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Snapshot & labelled-table refresh
# ---------------------------------------------------------------------------


def _rebuild_snapshots(conn):
    """Rebuild the three snapshot tables from source data + annotations."""
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(
            "SELECT name, level, datatype FROM label_definitions ORDER BY level, name"
        )
        label_defs = cur.fetchall()

    bad_names = [
        ld["name"] for ld in label_defs
        if not ld.get("name") or not LABEL_NAME_RE.match(ld["name"])
    ]
    if bad_names:
        raise HTTPException(
            status_code=400,
            detail=f"Refusing to rebuild snapshots; label name(s) violate allowlist {LABEL_NAME_RE.pattern}: {bad_names}",
        )

    counts = {}

    levels = [
        ("patient", "lvo_clinical_data", "patient_id",
         psql.SQL("{pid} AS patient_id, stroke_date").format(
             pid=psql.Identifier(PATIENT_ID_COL)
         )),
        ("study", "image_study", "studyinstanceuid",
         psql.SQL("patient_id, acquisitiondatetime, study_type, studyinstanceuid")),
        ("series", "image_series", "seriesinstanceuid",
         psql.SQL("patient_id, acquisitiondatetime, modality, seriesdescription, seriesinstanceuid")),
    ]

    for level_name, source_table, id_col, base_cols in levels:
        snapshot_table = f"snapshot_{level_name}s"
        snapshot_id = psql.Identifier(snapshot_table)
        source_id = psql.Identifier(source_table)
        id_col_id = psql.Identifier(id_col)
        level_lit = psql.Literal(level_name)
        level_labels = [ld for ld in label_defs if ld["level"] == level_name]

        pivot_col_parts = []
        pivot_join_parts = []
        for i, ld in enumerate(level_labels):
            alias_id = psql.Identifier(f"a{i}")
            col_alias_id = psql.Identifier(f"label_{ld['name'].lower()}")
            label_lit = psql.Literal(ld["name"])
            pivot_col_parts.append(
                psql.SQL("{a}.value AS {c}").format(a=alias_id, c=col_alias_id)
            )
            pivot_join_parts.append(
                psql.SQL(
                    "LEFT JOIN annotations {a} ON {a}.level = {lvl} "
                    "AND {a}.{idc} = src.{idc} "
                    "AND {a}.label = {lbl}"
                ).format(
                    a=alias_id, lvl=level_lit, idc=id_col_id, lbl=label_lit,
                )
            )

        pivot_cols = (
            psql.SQL(", ") + psql.SQL(", ").join(pivot_col_parts)
            if pivot_col_parts else psql.SQL("")
        )
        pivot_joins = (
            psql.SQL(" ") + psql.SQL(" ").join(pivot_join_parts)
            if pivot_join_parts else psql.SQL("")
        )

        with conn.cursor() as cur:
            cur.execute(psql.SQL("DROP TABLE IF EXISTS {}").format(snapshot_id))
            cur.execute(
                psql.SQL(
                    "CREATE TABLE {snap} AS "
                    "SELECT DISTINCT ON (src.{idc}) src.*{pcols} "
                    "FROM (SELECT {base} FROM {src}) src{pjoins}"
                ).format(
                    snap=snapshot_id,
                    idc=id_col_id,
                    pcols=pivot_cols,
                    base=base_cols,
                    src=source_id,
                    pjoins=pivot_joins,
                )
            )
            cur.execute(psql.SQL("SELECT COUNT(*) FROM {}").format(snapshot_id))
            counts[snapshot_table] = cur.fetchone()[0]

    conn.commit()
    return counts


@router.post("/api/snapshots/refresh")
def refresh_snapshots(auth_token: str | None = Cookie(None)):
    get_current_user(auth_token)
    conn = get_conn()
    try:
        counts = _rebuild_snapshots(conn)
        return {"ok": True, "counts": counts}
    finally:
        conn.close()


@router.post("/api/labelled-tables/refresh")
def refresh_labelled_tables(
    auth_token: str | None = Cookie(None),
    level: list[str] | None = Query(None),
):
    get_current_user(auth_token)
    conn = get_conn()
    try:
        counts = rebuild_labelled_tables(conn, levels=level)
        conn.commit()
        return {"ok": True, "counts": counts}
    finally:
        conn.close()
