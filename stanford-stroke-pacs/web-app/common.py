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
    "series_type", "timepoint",
}

# Machine-derived classification, read-only. A separate axis from the human
# `series_type` / `timepoint` annotation labels; neither derives from the other.
# series_label is series_type + the per-patient preference rank (NCCT_1 = the
# NCCT to open for that patient), so filtering and sorting key on it.
SERIES_AUTO_COLS = (
    "s.series_type, s.series_type_rank, s.series_label, "
    "s.series_type_rule, s.series_type_version"
)
STUDY_AUTO_COLS = (
    "st.timepoint, st.timepoint_anchor_source, st.hours_to_event, st.timepoint_version"
)

# The frontend column's key is `series_type`, but its useful ordering is by
# label: series_label sorts by type first, then rank within the type.
SERIES_SORT_OVERRIDES = {"series_type": "series_label"}

# Match the *label* so "NCCT" finds every NCCT and "NCCT_1" narrows to each
# patient's preferred one.
SERIES_TYPE_MATCH_EXPR = "COALESCE(s.series_label, s.series_type, '')"
TIMEPOINT_MATCH_EXPR = "COALESCE(st.timepoint, '')"


def auto_match_sql(expr: str, values: list[str] | None) -> tuple[str, list[str]]:
    """OR-group of case-insensitive substring matches for an Auto column filter.

    One value is the column-header text filter; several are the sidebar
    multi-select, which ORs them (NCCT *or* CTA). Returns ("", []) for no
    usable value, so callers can skip the condition.
    """
    vals = [v.strip() for v in (values or []) if v and v.strip()]
    if not vals:
        return "", []
    ors = " OR ".join(f"UPPER({expr}) LIKE UPPER(%s)" for _ in vals)
    return f"({ors})", [f"%{v}%" for v in vals]


def table_exists(cur, name: str) -> bool:
    """Whether `public.<name>` is present.

    Lets callers treat a table as optional: `clinical_data` is an optional
    clinical import that a deployment may not have at all, and a
    not-yet-migrated DB or a stripped test fixture may be missing side-tables.
    """
    cur.execute("SELECT to_regclass(%s) AS reg", (f"public.{name}",))
    row = cur.fetchone()
    # Tolerate either a RealDictCursor (the list endpoints) or a plain cursor.
    return (row["reg"] if isinstance(row, dict) else row[0]) is not None


def column_exists(cur, table: str, column: str) -> bool:
    """Whether `public.<table>` has `column` (both assumed lower-case)."""
    cur.execute(
        "SELECT 1 FROM information_schema.columns "
        "WHERE table_schema = 'public' AND table_name = %s AND column_name = %s",
        (table, column),
    )
    return cur.fetchone() is not None


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
# Per-label edit permissions (label_definitions.edit_policy / edit_users)
#
# Answers "who may set or clear this label's values" — Alembic 0019. Read
# inline per request (one indexed lookup), like require_admin and
# get_dataset_scope; the dataset_access TTL cache exists only for the per-frame
# DICOMweb proxy, which this is not.
# ---------------------------------------------------------------------------

EDIT_POLICIES = ("everyone", "nobody", "users")

# Columns every permission decision needs. Callers fetch these once and pass the
# row to both helpers rather than re-querying.
LABEL_PERMISSION_COLUMNS = "datatype, edit_policy, edit_users, created_by"


def fetch_label_def(cur, label: str) -> dict | None:
    """The permission-relevant label_definitions row, or None if undefined."""
    cur.execute(
        f"SELECT {LABEL_PERMISSION_COLUMNS} FROM label_definitions WHERE name = %s",
        (label,),
    )
    return cur.fetchone()


def can_edit_label(label_def: dict | None, username: str) -> bool:
    """Whether ``username`` may set or clear values of this label.

    ``label_def`` is None for a label with no definition row: annotations for
    undefined labels are already accepted, so they stay editable — this function
    restricts, it never newly forbids.

    **No admin bypass.** ``nobody`` means nobody. An admin who must correct a
    value changes the policy first, which is deliberate and audited; a silent
    bypass would reintroduce the stray-click overwrite this exists to prevent.
    """
    if label_def is None:
        return True
    policy = label_def["edit_policy"]
    if policy == "everyone":
        return True
    if policy == "nobody":
        return False
    return username in (label_def["edit_users"] or [])


def can_change_label_policy(label_def: dict, username: str, is_admin: bool) -> bool:
    """Whether ``username`` may change this label's edit policy: owner or admin.

    Being *listed* in ``edit_users`` grants value edits, not control over who
    else may edit. Note bulk-created labels have a ``bulk:<user>`` owner, which
    matches no real login — so they are admin-only to unlock, for free.
    """
    return is_admin or label_def["created_by"] == username


# ---------------------------------------------------------------------------
# Select-label value vocabulary (label_value_options)
# ---------------------------------------------------------------------------


def record_label_value(cur, label: str, value: str | None, created_by: str | None) -> None:
    """Upsert one value into the select-label vocabulary (label_value_options).
    Runs on the caller's cursor so it commits atomically with the surrounding
    write; no-ops for empty values; idempotent via ON CONFLICT."""
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
    """WHERE fragment filtering *entity_id_expr* (at *entity_level*) by an
    annotation at *label_level* (falls back to entity_level; cross-level goes
    through the parent column or an intermediate table). ``operator`` is
    ``"IN"``/``"NOT IN"`` (boolean false filters). ``value_predicate`` is extra
    SQL after ``label = %s`` — e.g. ``"AND COALESCE(value, '') = ANY(%s)"`` —
    making two ``%s`` placeholders total; the caller binds the params.
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
