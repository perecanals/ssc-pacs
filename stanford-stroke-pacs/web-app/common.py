"""Shared constants, SQL builders, and annotation helpers.

Every route module that deals with label filtering or annotation
attachment imports from here.
"""

from __future__ import annotations

import json
import re

from fastapi import HTTPException

from db import get_conn

VALID_LEVELS = ("patient", "study", "series")

LABEL_NAME_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_]{0,62}$")

SERIES_FROM_CLAUSE = (
    "image_series s "
    "LEFT JOIN image_study st ON s.studyinstanceuid = st.studyinstanceuid"
)

SERIES_SORT_WHITELIST = {
    "patient_id", "import_id", "import_label", "acquisitiondatetime",
    "modality", "seriesdescription", "number_of_slices",
    "slicethickness", "scanaxialcoverage_mm",
}

# ---------------------------------------------------------------------------
# Dataset (cohort) scoping
#
# A caller's scope comes from auth.get_dataset_scope: None = admin
# (unrestricted), list = allowed `patient.dataset` tags (deny-by-default —
# empty list matches nothing). Always bind scope lists as %s::text[] so
# psycopg2 adapts empty lists to a typed empty array.
# ---------------------------------------------------------------------------


def dataset_filter_sql(patient_id_expr: str) -> str:
    """WHERE fragment limiting rows to patients whose dataset overlaps the
    caller's scope. One ``%s`` placeholder: the scope list (text[])."""
    return (
        "EXISTS (SELECT 1 FROM patient dsp "
        f"WHERE dsp.patient_id = {patient_id_expr} AND dsp.dataset && %s::text[])"
    )


def _ensure_access(cur, sql: str, entity_id: str, scope: list[str] | None, detail: str):
    if scope is None:
        return
    cur.execute(sql, (entity_id, scope))
    if cur.fetchone() is None:
        # 404 (not 403) so out-of-scope entity ids are indistinguishable
        # from nonexistent ones.
        raise HTTPException(status_code=404, detail=detail)


def ensure_patient_access(cur, patient_id: str, scope: list[str] | None) -> None:
    _ensure_access(
        cur,
        "SELECT 1 FROM patient WHERE patient_id = %s AND dataset && %s::text[]",
        patient_id, scope, "Patient not found",
    )


def ensure_study_access(cur, studyinstanceuid: str, scope: list[str] | None) -> None:
    _ensure_access(
        cur,
        "SELECT 1 FROM image_study st JOIN patient p ON p.patient_id = st.patient_id "
        "WHERE st.studyinstanceuid = %s AND p.dataset && %s::text[]",
        studyinstanceuid, scope, "Study not found",
    )


def ensure_series_access(cur, seriesinstanceuid: str, scope: list[str] | None) -> None:
    _ensure_access(
        cur,
        "SELECT 1 FROM image_series s JOIN patient p ON p.patient_id = s.patient_id "
        "WHERE s.seriesinstanceuid = %s AND p.dataset && %s::text[]",
        seriesinstanceuid, scope, "Series not found",
    )


def _check_access(ensure_fn, entity_id: str, scope: list[str] | None) -> None:
    """Conn-opening variant of the ``ensure_*`` checks, for handlers that
    have no cursor of their own. No-op for admins (scope None)."""
    if scope is None:
        return
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            ensure_fn(cur, entity_id, scope)
    finally:
        conn.close()


def check_patient_access(patient_id: str, scope: list[str] | None) -> None:
    _check_access(ensure_patient_access, patient_id, scope)


def check_study_access(studyinstanceuid: str, scope: list[str] | None) -> None:
    _check_access(ensure_study_access, studyinstanceuid, scope)


def check_series_access(seriesinstanceuid: str, scope: list[str] | None) -> None:
    _check_access(ensure_series_access, seriesinstanceuid, scope)


# ---------------------------------------------------------------------------
# Select-label value vocabulary (label_value_options)
# ---------------------------------------------------------------------------


def record_label_value(cur, label: str, value: str | None, created_by: str | None) -> None:
    """Upsert one value into the select-label vocabulary (label_value_options).

    Used on annotation writes and label-definition seeding so the inline-edit
    dropdown and the column filter share one fast, indexed source of truth.
    No-ops for empty values. Runs on the caller's cursor so it commits atomically
    with the surrounding write. Idempotent via ON CONFLICT.
    """
    if value is None:
        return
    trimmed = value.strip()
    if not trimmed:
        return
    cur.execute(
        "INSERT INTO label_value_options (label, value, created_by) "
        "VALUES (%s, %s, %s) ON CONFLICT (label, value) DO NOTHING",
        (label, trimmed, created_by),
    )


# ---------------------------------------------------------------------------
# Unified label-filter SQL builder  (replaces _label_filter_sql,
# _label_value_filter_sql, _label_select_values_filter_sql,
# _label_bool_filter_sql)
# ---------------------------------------------------------------------------

_ANNOTATION_KEY = {
    "patient": "patient_id",
    "study": "studyinstanceuid",
    "series": "seriesinstanceuid",
}

# When the entity level is BELOW the label level (filtering UP), use the
# entity table's parent-column to reach the annotation level directly.
_PARENT_COL: dict[tuple[str, str], str] = {
    ("study", "patient"): "st.patient_id",
    ("series", "patient"): "s.patient_id",
    ("series", "study"): "s.studyinstanceuid",
}

# When the entity level is ABOVE the label level (filtering DOWN), join
# through an intermediate table.
# key → (intermediate_table, column_to_select, column_to_match_annotation)
_DOWN_JOIN: dict[tuple[str, str], tuple[str, str, str]] = {
    ("patient", "study"): ("image_study", "patient_id", "studyinstanceuid"),
    ("patient", "series"): ("image_series", "patient_id", "seriesinstanceuid"),
    ("study", "series"): ("image_series", "studyinstanceuid", "seriesinstanceuid"),
}


def build_label_filter_sql(
    entity_level: str,
    label_level: str | None,
    entity_id_expr: str,
    *,
    operator: str = "IN",
    value_predicate: str = "",
) -> str:
    """Return a SQL fragment that filters *entity_id_expr* by an annotation.

    Parameters
    ----------
    entity_level : str
        Level of the current listing (``"patient"``, ``"study"``, ``"series"``).
    label_level : str | None
        Level of the annotation to filter on.  Falls back to *entity_level*.
    entity_id_expr : str
        Column expression to match (e.g. ``"p.patient_id"``, ``"st.studyinstanceuid"``).
    operator : str
        ``"IN"`` or ``"NOT IN"`` — for boolean false filters.
    value_predicate : str
        Additional SQL appended inside the innermost ``SELECT``, after
        ``label = %s``.  For example ``"AND LOWER(COALESCE(value, '')) LIKE
        LOWER(%s)"`` (two ``%s`` placeholders total) or
        ``"AND COALESCE(value, '') = ANY(%s)"`` (two ``%s`` placeholders).

    Returns
    -------
    str
        A SQL fragment suitable for use in a WHERE clause.  The caller must
        supply the corresponding parameters.
    """
    ll = label_level if label_level in VALID_LEVELS else entity_level
    ann_key = _ANNOTATION_KEY[ll]

    ann_subq = f"SELECT {ann_key} FROM annotations WHERE level = '{ll}' AND label = %s"
    if value_predicate:
        ann_subq += f" {value_predicate}"

    # Same level: direct match.
    if entity_level == ll:
        return f"{entity_id_expr} {operator} ({ann_subq})"

    key = (entity_level, ll)

    # Filtering UP: entity is below the annotation level.
    if key in _PARENT_COL:
        parent_col = _PARENT_COL[key]
        return f"{parent_col} {operator} ({ann_subq})"

    # Filtering DOWN: entity is above the annotation level.
    if key in _DOWN_JOIN:
        table, entity_col, ann_col = _DOWN_JOIN[key]
        return (
            f"{entity_id_expr} {operator} ("
            f"SELECT {entity_col} FROM {table} WHERE {ann_col} IN ({ann_subq}))"
        )

    # Fallback (should not be reachable with valid levels).
    return f"{entity_id_expr} {operator} ({ann_subq})"


# ---------------------------------------------------------------------------
# Label-filter parsing (from query-string JSON)
# ---------------------------------------------------------------------------


def parse_label_filters(raw: str | None) -> list[dict[str, object]]:
    """Parse the ``label_filters`` JSON query param into a validated list."""
    if not raw:
        return []
    try:
        parsed = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return []
    if not isinstance(parsed, list):
        return []
    out: list[dict[str, object]] = []
    for item in parsed:
        if not isinstance(item, dict):
            continue
        label = str(item.get("label", "")).strip()
        value = str(item.get("value", "")).strip()
        raw_values = item.get("values")
        values = []
        if isinstance(raw_values, list):
            values = [str(v).strip() for v in raw_values if str(v).strip()]
        lvl = str(item.get("level", "")).strip()
        datatype = str(item.get("datatype", "")).strip()
        if datatype == "select":
            if not values and value:
                values = [value]
            if not label or not values:
                continue
        elif not label or not value:
            continue
        out.append({
            "label": label,
            "value": value,
            "values": values,
            "level": lvl if lvl in VALID_LEVELS else "",
            "datatype": datatype,
        })
    return out


def apply_label_filters(parsed_filters, entity_level, entity_id_expr, conditions, params):
    """Append SQL conditions + params for parsed label filters."""
    for lf in parsed_filters:
        if lf["datatype"] == "bool":
            exists = lf["value"] == "true"
            conditions.append(
                build_label_filter_sql(
                    entity_level, lf["level"], entity_id_expr,
                    operator="IN" if exists else "NOT IN",
                )
            )
            params.append(lf["label"])
        elif lf["datatype"] == "select":
            values = [str(v).strip() for v in lf.get("values", []) if str(v).strip()]
            if not values and lf.get("value"):
                values = [str(lf["value"]).strip()]
            if not values:
                continue
            conditions.append(
                build_label_filter_sql(
                    entity_level, lf["level"], entity_id_expr,
                    value_predicate="AND COALESCE(value, '') = ANY(%s)",
                )
            )
            params.extend([lf["label"], values])
        else:
            conditions.append(
                build_label_filter_sql(
                    entity_level, lf["level"], entity_id_expr,
                    value_predicate="AND LOWER(COALESCE(value, '')) LIKE LOWER(%s)",
                )
            )
            params.extend([lf["label"], f"%{lf['value']}%"])


# ---------------------------------------------------------------------------
# Annotation formatting and attachment
# ---------------------------------------------------------------------------


def format_ann(a: dict) -> dict:
    return {
        "id": a["id"],
        "level": a.get("level", "series"),
        "label": a["label"],
        "value": a["value"],
        "created_by": a["created_by"],
        "created_at": a["created_at"].isoformat() if a["created_at"] else None,
        "notes": a["notes"],
    }


def attach_annotations(cur, rows, level, id_col):
    """Fetch annotations for a batch of rows keyed by *id_col* at *level*."""
    if not rows:
        return
    ids = [r[id_col] for r in rows]
    cur.execute(
        f"SELECT {id_col}, id, level, label, value, created_by, created_at, notes "
        f"FROM annotations WHERE level = %s AND {id_col} = ANY(%s) "
        f"ORDER BY created_at",
        (level, ids),
    )
    ann_map: dict[str, list] = {}
    for a in cur.fetchall():
        ann_map.setdefault(a[id_col], []).append(format_ann(a))
    for r in rows:
        r["annotations"] = ann_map.get(r[id_col], [])


def attach_inherited_annotations(cur, rows, child_level):
    """Attach parent-level annotations inherited from above."""
    if not rows:
        return
    if child_level == "series":
        study_uids = list({r["studyinstanceuid"] for r in rows if r.get("studyinstanceuid")})
        patient_ids = list({r["patient_id"] for r in rows if r.get("patient_id")})
        study_anns: dict[str, list] = {}
        patient_anns: dict[str, list] = {}
        if study_uids:
            cur.execute(
                "SELECT studyinstanceuid, id, level, label, value, created_by, created_at, notes "
                "FROM annotations WHERE level = 'study' AND studyinstanceuid = ANY(%s) "
                "ORDER BY created_at",
                (study_uids,),
            )
            for a in cur.fetchall():
                study_anns.setdefault(a["studyinstanceuid"], []).append(format_ann(a))
        if patient_ids:
            cur.execute(
                "SELECT patient_id, id, level, label, value, created_by, created_at, notes "
                "FROM annotations WHERE level = 'patient' AND patient_id = ANY(%s) "
                "ORDER BY created_at",
                (patient_ids,),
            )
            for a in cur.fetchall():
                patient_anns.setdefault(a["patient_id"], []).append(format_ann(a))
        for r in rows:
            inherited = []
            inherited.extend(patient_anns.get(r.get("patient_id", ""), []))
            inherited.extend(study_anns.get(r.get("studyinstanceuid", ""), []))
            r["inherited_annotations"] = inherited
    elif child_level == "study":
        patient_ids = list({r["patient_id"] for r in rows if r.get("patient_id")})
        patient_anns_s: dict[str, list] = {}
        if patient_ids:
            cur.execute(
                "SELECT patient_id, id, level, label, value, created_by, created_at, notes "
                "FROM annotations WHERE level = 'patient' AND patient_id = ANY(%s) "
                "ORDER BY created_at",
                (patient_ids,),
            )
            for a in cur.fetchall():
                patient_anns_s.setdefault(a["patient_id"], []).append(format_ann(a))
        for r in rows:
            r["inherited_annotations"] = patient_anns_s.get(r.get("patient_id", ""), [])
    else:
        for r in rows:
            r["inherited_annotations"] = []
