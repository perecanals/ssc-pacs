"""Patient, study, and series browsing endpoints, OHIF link, imaging downloads."""

from __future__ import annotations

import logging
import os
from urllib.parse import urlencode

import psycopg2.extras
from fastapi import APIRouter, Depends, HTTPException, Query

from auth import get_dataset_scope, require_admin, require_staff
from cache_manager import (
    get_cache_status,
    get_series_cache_status,
    touch_access,
    touch_access_series,
)
from common import (
    SERIES_AUTO_COLS,
    SERIES_FROM_CLAUSE,
    SERIES_SORT_OVERRIDES,
    SERIES_SORT_WHITELIST,
    SERIES_TYPE_MATCH_EXPR,
    STUDY_AUTO_COLS,
    TIMEPOINT_MATCH_EXPR,
    apply_label_filters,
    attach_annotations,
    attach_enrollment_display,
    attach_inherited_annotations,
    auto_match_sql,
    build_label_filter_sql,
    column_exists,
    enrollment_match_sql,
    ensure_patient_access,
    ensure_study_access,
    parse_label_filters,
    scope_literal,
    subject_scope_sql,
    table_exists,
)
from config import CLINICAL_DATA_DATASET, CLINICAL_EPISODE_DATE_COLUMN, STORAGE_MODE
from db import get_conn
from imaging_downloads import imaging_download
from orthanc_client import orthanc_lookup

logger = logging.getLogger(__name__)

router = APIRouter()

# Count all series in the study, independently of browsing filters.
STUDY_SERIES_COUNT = (
    "(SELECT COUNT(*) FROM image_series s "
    "WHERE s.studyinstanceuid = st.studyinstanceuid)"
)
STUDY_MODALITY = "array_to_string(st.modalities, ', ')"

# Effective episode-date column on the optional clinical_data table. Frozen at
# startup by resolve_clinical_date_column() (called from app.py's lifespan,
# after migrations); table *presence* stays a per-request check so the table
# can come and go without a restart.
_effective_clinical_date_column: str = CLINICAL_EPISODE_DATE_COLUMN


def resolve_clinical_date_column(cur) -> str:
    """Validate the configured episode-date column against the live schema.

    Runs once at startup. Falls back to ``stroke_date`` with a WARN when
    clinical_data exists but lacks the configured column — a typo'd config
    must degrade, not 500 on every /api/patients. When the table is absent
    the configured value is kept verbatim: there is nothing to validate
    against, the identifier is already injection-safe (config.py), and it is
    only interpolated into SQL once a table exists.
    """
    global _effective_clinical_date_column
    col = CLINICAL_EPISODE_DATE_COLUMN
    if (
        col != "stroke_date"
        and table_exists(cur, "clinical_data")
        and not column_exists(cur, "clinical_data", col)
    ):
        logger.warning(
            "clinical_episode_date_column %r not found on clinical_data; "
            "falling back to stroke_date", col,
        )
        col = "stroke_date"
    _effective_clinical_date_column = col
    return col


def _clinical_join_sql(cur, patient_alias: str) -> str:
    """ON-clause for joining clinical_data to an enrollment.

    clinical_data.study_id carries one dataset's patient ids but has no dataset
    column; restricting the join to config's ``clinical_data_dataset`` keeps a
    same-id patient from another dataset off that clinical row.
    """
    on = f"c.study_id = {patient_alias}.patient_id"
    if CLINICAL_DATA_DATASET:
        literal = cur.mogrify("%s", (CLINICAL_DATA_DATASET,)).decode().replace("%", "%%")
        on += f" AND {patient_alias}.dataset = {literal}"
    return on


# ---------------------------------------------------------------------------
# Patient browsing
# ---------------------------------------------------------------------------


@router.get("/api/patients")
def list_patients(
    patient_id: str | None = Query(None),
    stroke_date: str | None = Query(None),
    study_import_label: str | None = Query(
        None,
        description=(
            "Exact match on import_label across image_study/image_series; "
            "patient included if any study/series has this label."
        ),
    ),
    dataset: str | None = Query(
        None,
        description="Exact match on the enrollment's dataset name.",
    ),
    series_type: list[str] | None = Query(
        None,
        description="Only patients having a series with this machine-derived label.",
    ),
    timepoint: list[str] | None = Query(
        None,
        description="Only patients having a study at this machine-derived timepoint.",
    ),
    label: str | None = Query(None),
    label_level: str | None = Query(None),
    label_filters: str | None = Query(None),
    sort_by: str = Query("patient_id"),
    sort_dir: str = Query("asc"),
    page: int = Query(1, ge=1),
    per_page: int = Query(50, ge=1, le=500),
    scope: list[str] | None = Depends(get_dataset_scope),
):
    conn = get_conn()
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            conditions = []
            params: list = []

            if scope is not None:
                conditions.append("p.dataset = ANY(%s::text[])")
                params.append(scope)

            # The Auto filters are series-/study-level, so at the patient level
            # they ask "has one": a patient matches if any of their series (or
            # studies) does — including imaging owned by a linked enrollment
            # of the same subject.
            st_sql, st_params = auto_match_sql(SERIES_TYPE_MATCH_EXPR, series_type)
            if st_sql:
                conditions.append(
                    "EXISTS (SELECT 1 FROM image_series s "
                    f"WHERE s.subject_id = p.subject_id AND {st_sql})"
                )
                params.extend(st_params)
            tp_sql, tp_params = auto_match_sql(TIMEPOINT_MATCH_EXPR, timepoint)
            if tp_sql:
                conditions.append(
                    "EXISTS (SELECT 1 FROM image_study st "
                    f"WHERE st.subject_id = p.subject_id AND {tp_sql})"
                )
                params.extend(tp_params)

            # Patient level is sourced from the `patient` registry (one row per
            # patient, comprehensive). clinical_data is an optional clinical
            # import that a deployment may not have at all; when it is present
            # it is joined only to prefer its clinical episode date for
            # clinically-matched patients. Without it every patient falls back to
            # the imaging-derived patient.stroke_date — the same value a patient
            # with no clinical row already gets. (Its patient-id column is
            # historically named study_id, and the join is restricted to the
            # dataset it belongs to.) Which clinical column supplies the
            # episode date is config.toml [web-app] clinical_episode_date_column,
            # resolved once at startup by resolve_clinical_date_column().
            #
            # The configured column may not be TEXT (the historical stroke_date
            # is); ::text keeps the COALESCE in text space either way — prefer
            # the clinical string, fall back to the imaging date as YYYY-MM-DD —
            # preserving the prior text contract and lexicographic date sort.
            # Filter, sort, and SELECT all reuse stroke_date_expr, so the two
            # branches cannot drift apart.
            if table_exists(cur, "clinical_data"):
                from_clause = (
                    "FROM patient p "
                    f"LEFT JOIN clinical_data c ON {_clinical_join_sql(cur, 'p')}"
                )
                stroke_date_expr = (
                    f"COALESCE(c.{_effective_clinical_date_column}::text, "
                    "p.stroke_date::date::text)"
                )
            else:
                from_clause = "FROM patient p"
                stroke_date_expr = "p.stroke_date::date::text"

            if patient_id:
                conditions.append("p.patient_id::text LIKE %s")
                params.append(f"%{patient_id}%")
            if stroke_date:
                conditions.append(f"{stroke_date_expr}::text LIKE %s")
                params.append(f"%{stroke_date}%")
            sil = (study_import_label or "").strip()
            if sil:
                conditions.append(
                    "p.subject_id IN ("
                    "SELECT subject_id FROM image_study st WHERE st.import_label = %s "
                    "UNION "
                    "SELECT subject_id FROM image_series s WHERE s.import_label = %s)"
                )
                params.append(sil)
                params.append(sil)
            ds = (dataset or "").strip()
            if ds:
                conditions.append("p.dataset = %s")
                params.append(ds)
            if label:
                conditions.append(
                    build_label_filter_sql("patient", label_level, "p.patient_key")
                )
                params.append(label)
            apply_label_filters(
                parse_label_filters(label_filters),
                "patient", "p.patient_key", conditions, params,
            )

            where = ("WHERE " + " AND ".join(conditions)) if conditions else ""
            offset = (page - 1) * per_page

            cur.execute(
                f"SELECT COUNT(*) {from_clause} {where}", params
            )
            total = cur.fetchone()["count"]

            order_expr = (
                stroke_date_expr if sort_by == "stroke_date" else "p.patient_id"
            )
            direction = "DESC" if sort_dir.lower() == "desc" else "ASC"

            study_labels_agg = (
                "COALESCE(("
                "  SELECT string_agg(lbl, ', ' ORDER BY lbl) FROM ("
                "    SELECT DISTINCT TRIM(sti.import_label) AS lbl "
                "    FROM image_study sti "
                "    WHERE sti.subject_id = p.subject_id "
                "      AND sti.import_label IS NOT NULL AND TRIM(sti.import_label) <> '' "
                "    UNION "
                "    SELECT DISTINCT TRIM(s.import_label) AS lbl "
                "    FROM image_series s "
                "    WHERE s.subject_id = p.subject_id "
                "      AND s.import_label IS NOT NULL AND TRIM(s.import_label) <> '' "
                "  ) u"
                "), '') AS study_import_labels"
            )
            cur.execute(
                "SELECT p.patient_key, p.subject_id, p.patient_id AS patient_id, "
                # Where a patient-label edit on this row writes (study/series
                # rows get theirs from attach_inherited_annotations).
                "p.patient_key AS edit_patient_key, "
                f"{stroke_date_expr} AS stroke_date, "
                f"{study_labels_agg}, "
                "p.dataset "
                f"{from_clause} {where} "
                f"ORDER BY {order_expr} {direction} NULLS LAST, p.patient_id ASC, "
                "p.patient_key ASC "
                f"LIMIT %s OFFSET %s",
                params + [per_page, offset],
            )
            rows = cur.fetchall()

            attach_annotations(cur, rows, "patient", "patient_key")
            attach_inherited_annotations(cur, rows, "patient", scope=scope)

        return {"total": total, "page": page, "per_page": per_page, "items": rows}
    finally:
        conn.close()


@router.get("/api/patients/{patient_key}/studies")
def patient_studies(
    patient_key: str,
    study_import_label: str | None = Query(
        None,
        description="If set, only studies connected to this import_label are returned.",
    ),
    series_type: list[str] | None = Query(
        None,
        description="Only studies containing a series with this machine-derived label.",
    ),
    timepoint: list[str] | None = Query(
        None,
        description=(
            "Machine-derived timepoint (BL / THROMBECTOMY / FU). Substring "
            "match, repeatable (ORed)."
        ),
    ),
    label_filters: str | None = Query(None),
    scope: list[str] | None = Depends(get_dataset_scope),
):
    """Studies for a patient (expandable sub-rows).

    Every study of the enrollment's subject — including imaging owned by a
    linked enrollment in another dataset. Inherited patient labels are this
    enrollment's only (the rows are shown on its behalf).

    Optionally narrowed by the sidebar quick filters (import_label, the Auto
    `series_type` / `timepoint` columns, and select-value annotation labels) so
    an expanded subtable mirrors the top-level filter. `series_type` is a
    "has-one" match (study kept if any of its series matches); `timepoint` and
    the label filters apply to the study directly — the same semantics as the
    flat `list_studies` endpoint.
    """
    conn = get_conn()
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            ensure_patient_access(cur, patient_key, scope)
            cur.execute(
                "SELECT subject_id FROM patient WHERE patient_key = %s", (patient_key,)
            )
            patient = cur.fetchone()
            if patient is None:
                raise HTTPException(status_code=404, detail="Patient not found")
            conditions = ["st.subject_id = %s"]
            params: list = [patient["subject_id"]]
            sil = (study_import_label or "").strip()
            if sil:
                conditions.append(
                    "("
                    "st.import_label = %s OR EXISTS ("
                    "  SELECT 1 FROM image_series s "
                    "  WHERE s.studyinstanceuid = st.studyinstanceuid AND s.import_label = %s"
                    "))"
                )
                params.append(sil)
                params.append(sil)
            tp_sql, tp_params = auto_match_sql(TIMEPOINT_MATCH_EXPR, timepoint)
            if tp_sql:
                conditions.append(tp_sql)
                params.extend(tp_params)
            st_sql, st_params = auto_match_sql(SERIES_TYPE_MATCH_EXPR, series_type)
            if st_sql:
                conditions.append(
                    "EXISTS (SELECT 1 FROM image_series s "
                    f"WHERE s.studyinstanceuid = st.studyinstanceuid AND {st_sql})"
                )
                params.extend(st_params)
            scope_lit = scope_literal(cur, scope)
            apply_label_filters(
                parse_label_filters(label_filters),
                "study", "st.studyinstanceuid", conditions, params, scope_lit,
            )
            where = "WHERE " + " AND ".join(conditions)
            cur.execute(
                "SELECT st.patient_key, st.subject_id, st.import_id, st.import_label, "
                "st.acquisitiondatetime, st.studyinstanceuid, "
                "st.studydescription, st.study_type, "
                f"{STUDY_SERIES_COUNT} AS number_of_series, "
                f"{STUDY_AUTO_COLS}, "
                f"COALESCE({STUDY_MODALITY}, '') AS modality, "
                "st.patient_id "
                "FROM image_study st "
                f"{where} "
                "ORDER BY st.acquisitiondatetime",
                tuple(params),
            )
            rows = cur.fetchall()
            for r in rows:
                dt = r.get("acquisitiondatetime")
                r["acquisitiondatetime"] = dt.isoformat() if dt else None

            attach_enrollment_display(cur, rows, scope)
            attach_annotations(cur, rows, "study", "studyinstanceuid")
            attach_inherited_annotations(
                cur, rows, "study", scope=scope, context_key=patient_key
            )

        return rows
    finally:
        conn.close()


@router.get("/api/study-import-labels")
def list_study_import_labels(scope: list[str] | None = Depends(get_dataset_scope)):
    """Distinct non-empty `import_label` values (study+series) for patient-level filter UI."""
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            scope_st = scope_s = ""
            params: list = []
            if scope is not None:
                scope_st = " AND " + subject_scope_sql("image_study.subject_id")
                scope_s = " AND " + subject_scope_sql("image_series.subject_id")
                params = [scope, scope]
            cur.execute(
                "SELECT import_label FROM ("
                "  SELECT DISTINCT TRIM(import_label) AS import_label FROM image_study "
                f"  WHERE import_label IS NOT NULL AND TRIM(import_label) <> ''{scope_st} "
                "  UNION "
                "  SELECT DISTINCT TRIM(import_label) AS import_label FROM image_series "
                f"  WHERE import_label IS NOT NULL AND TRIM(import_label) <> ''{scope_s} "
                ") u ORDER BY import_label",
                params,
            )
            return [r[0] for r in cur.fetchall()]
    finally:
        conn.close()


@router.get("/api/datasets")
def list_datasets(scope: list[str] | None = Depends(get_dataset_scope)):
    """Registered dataset names (the `dataset` registry).

    Non-admins get only the datasets they are granted (sidebar filter); admins
    get the full list — which is also what the /admin permissions page
    consumes as the set of grantable datasets (a dataset is grantable once
    registered, before anything is ingested into it).
    """
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT name FROM dataset ORDER BY name")
            all_datasets = [r[0] for r in cur.fetchall()]
    finally:
        conn.close()
    if scope is not None:
        return [d for d in all_datasets if d in scope]
    return all_datasets


@router.get("/api/classification-values")
def list_classification_values(scope: list[str] | None = Depends(get_dataset_scope)):
    """Vocabularies + counts for the sidebar's Auto quick filters.

    Read from the data rather than hardcoded, so a reclassify run under new
    rules changes the filter options without a frontend release.
    """
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            params: list = []
            scope_s = scope_t = ""
            if scope is not None:
                scope_s = f" AND {subject_scope_sql('s.subject_id')}"
                scope_t = f" AND {subject_scope_sql('st.subject_id')}"

            cur.execute(
                "SELECT s.series_type, COUNT(*) FROM image_series s "
                f"WHERE s.series_type IS NOT NULL AND s.series_type <> ''{scope_s} "
                "GROUP BY 1 ORDER BY 2 DESC",
                params + ([scope] if scope is not None else []),
            )
            series_types = [{"value": r[0], "count": r[1]} for r in cur.fetchall()]

            # Clinical order (pre / during / post puncture), not alphabetical —
            # BL, FU, THROMBECTOMY would read as nonsense.
            cur.execute(
                "SELECT st.timepoint, COUNT(*) FROM image_study st "
                f"WHERE st.timepoint IS NOT NULL AND st.timepoint <> ''{scope_t} "
                "GROUP BY 1 "
                "ORDER BY CASE st.timepoint "
                "  WHEN 'BL' THEN 1 WHEN 'THROMBECTOMY' THEN 2 WHEN 'FU' THEN 3 "
                "  ELSE 4 END, 1",
                params + ([scope] if scope is not None else []),
            )
            timepoints = [{"value": r[0], "count": r[1]} for r in cur.fetchall()]

        return {"series_types": series_types, "timepoints": timepoints}
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Study browsing
# ---------------------------------------------------------------------------


@router.get("/api/studies")
def list_studies(
    studyinstanceuid: str | None = Query(None),
    patient_id: str | None = Query(None),
    import_id: str | None = Query(None),
    import_label: str | None = Query(None),
    dataset: str | None = Query(
        None,
        description=(
            "Exact match on a dataset name; study included if any in-scope "
            "enrollment of its subject is in that dataset."
        ),
    ),
    modality: str | None = Query(None),
    study_type: str | None = Query(None),
    timepoint: list[str] | None = Query(
        None,
        description=(
            "Machine-derived timepoint (BL / THROMBECTOMY / FU). Substring "
            "match, repeatable (ORed)."
        ),
    ),
    series_type: list[str] | None = Query(
        None,
        description="Only studies containing a series with this machine-derived label.",
    ),
    studydescription: str | None = Query(None),
    acquisitiondatetime: str | None = Query(None),
    label: str | None = Query(None),
    label_level: str | None = Query(None),
    label_filters: str | None = Query(None),
    sort_by: str = Query("patient_id"),
    sort_dir: str = Query("asc"),
    page: int = Query(1, ge=1),
    per_page: int = Query(50, ge=1, le=500),
    scope: list[str] | None = Depends(get_dataset_scope),
):
    conn = get_conn()
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            conditions = []
            params: list = []

            scope_lit = scope_literal(cur, scope)
            if scope is not None:
                conditions.append(subject_scope_sql("st.subject_id"))
                params.append(scope)

            if studyinstanceuid:
                conditions.append("st.studyinstanceuid LIKE %s")
                params.append(f"%{studyinstanceuid}%")
            if patient_id:
                conditions.append(
                    enrollment_match_sql("st.subject_id", scope_lit, "q.patient_id LIKE %s")
                )
                params.append(f"%{patient_id}%")
            if import_id:
                conditions.append("st.import_id::text LIKE %s")
                params.append(f"%{import_id}%")
            if import_label:
                conditions.append("LOWER(COALESCE(st.import_label, '')) LIKE LOWER(%s)")
                params.append(f"%{import_label}%")
            ds = (dataset or "").strip()
            if ds:
                conditions.append(
                    enrollment_match_sql("st.subject_id", scope_lit, "q.dataset = %s")
                )
                params.append(ds)
            if study_type:
                conditions.append("UPPER(st.study_type) = UPPER(%s)")
                params.append(study_type)
            tp_sql, tp_params = auto_match_sql(TIMEPOINT_MATCH_EXPR, timepoint)
            if tp_sql:
                conditions.append(tp_sql)
                params.extend(tp_params)
            st_sql, st_params = auto_match_sql(SERIES_TYPE_MATCH_EXPR, series_type)
            if st_sql:
                conditions.append(
                    "EXISTS (SELECT 1 FROM image_series s "
                    f"WHERE s.studyinstanceuid = st.studyinstanceuid AND {st_sql})"
                )
                params.extend(st_params)
            if studydescription:
                conditions.append("LOWER(st.studydescription) LIKE LOWER(%s)")
                params.append(f"%{studydescription}%")
            if acquisitiondatetime:
                conditions.append("st.acquisitiondatetime::text LIKE %s")
                params.append(f"%{acquisitiondatetime}%")
            if modality:
                conditions.append(
                    "st.studyinstanceuid IN ("
                    "  SELECT s2.studyinstanceuid FROM image_series s2 "
                    "  WHERE UPPER(s2.modality) LIKE UPPER(%s))"
                )
                params.append(f"%{modality}%")
            if label:
                conditions.append(
                    build_label_filter_sql(
                        "study", label_level, "st.studyinstanceuid", scope_lit=scope_lit
                    )
                )
                params.append(label)
            apply_label_filters(
                parse_label_filters(label_filters),
                "study", "st.studyinstanceuid", conditions, params, scope_lit,
            )

            where = "WHERE " + " AND ".join(conditions) if conditions else ""
            offset = (page - 1) * per_page

            cur.execute(
                f"SELECT COUNT(*) FROM image_study st {where}", params
            )
            total = cur.fetchone()["count"]

            col_map = {
                "patient_id": "patient_id",
                "import_id": "import_id",
                "import_label": "import_label",
                "acquisitiondatetime": "acquisitiondatetime",
                "studydescription": "studydescription",
                "study_type": "study_type",
                "timepoint": "timepoint",
            }
            col = col_map.get(sort_by, "patient_id")
            sort_expr = {
                "number_of_series": STUDY_SERIES_COUNT,
                "modality": STUDY_MODALITY,
            }.get(sort_by, f"st.{col}")
            direction = "DESC" if sort_dir.lower() == "desc" else "ASC"

            # patient_id / dataset are replaced by the subject's in-scope
            # enrollments below; sorting by patient_id orders by the owner's id.
            cur.execute(
                f"SELECT st.patient_key, st.subject_id, st.import_id, st.import_label, "
                f"st.acquisitiondatetime, "
                f"st.studyinstanceuid, st.studydescription, st.study_type, "
                f"{STUDY_SERIES_COUNT} AS number_of_series, "
                f"{STUDY_AUTO_COLS}, "
                f"COALESCE({STUDY_MODALITY}, '') AS modality, "
                f"st.patient_id "
                f"FROM image_study st {where} "
                f"ORDER BY {sort_expr} {direction} NULLS LAST, st.studyinstanceuid ASC "
                f"LIMIT %s OFFSET %s",
                params + [per_page, offset],
            )
            rows = cur.fetchall()
            for r in rows:
                dt = r.get("acquisitiondatetime")
                r["acquisitiondatetime"] = dt.isoformat() if dt else None

            attach_enrollment_display(cur, rows, scope)
            attach_annotations(cur, rows, "study", "studyinstanceuid")
            attach_inherited_annotations(cur, rows, "study", scope=scope)

        return {"total": total, "page": page, "per_page": per_page, "items": rows}
    finally:
        conn.close()


@router.get("/api/studies/{studyinstanceuid}/series")
def study_series(
    studyinstanceuid: str,
    series_type: list[str] | None = Query(
        None,
        description=(
            "Machine-derived series label. Substring match, repeatable (ORed)."
        ),
    ),
    timepoint: list[str] | None = Query(
        None,
        description=(
            "The owning study's machine-derived timepoint (BL / THROMBECTOMY / "
            "FU). Substring match, repeatable (ORed)."
        ),
    ),
    label_filters: str | None = Query(None),
    scope: list[str] | None = Depends(get_dataset_scope),
):
    """Series for a given study (expandable sub-rows).

    Optionally narrowed by the sidebar quick filters (the Auto `series_type` /
    `timepoint` columns and select-value annotation labels) so an expanded
    subtable mirrors the top-level filter — the same semantics as the flat
    `list_series` endpoint.
    """
    conn = get_conn()
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            ensure_study_access(cur, studyinstanceuid, scope)
            scope_lit = scope_literal(cur, scope)
            conditions = ["s.studyinstanceuid = %s"]
            params: list = [studyinstanceuid]
            for expr, vals in (
                (SERIES_TYPE_MATCH_EXPR, series_type),
                (TIMEPOINT_MATCH_EXPR, timepoint),
            ):
                sql, ps = auto_match_sql(expr, vals)
                if sql:
                    conditions.append(sql)
                    params.extend(ps)
            apply_label_filters(
                parse_label_filters(label_filters),
                "series", "s.seriesinstanceuid", conditions, params, scope_lit,
            )
            where = "WHERE " + " AND ".join(conditions)
            cur.execute(
                "SELECT s.seriesinstanceuid, s.studyinstanceuid, s.patient_key, s.subject_id, "
                "s.import_id, s.import_label, "
                "s.modality, s.seriesdescription, s.acquisitiondatetime, s.number_of_slices, "
                "s.slicethickness, s.scanaxialcoverage_mm, "
                f"{SERIES_AUTO_COLS}, {STUDY_AUTO_COLS}, "
                "s.patient_id "
                f"FROM {SERIES_FROM_CLAUSE} {where} "
                "ORDER BY s.acquisitiondatetime, s.seriesdescription",
                tuple(params),
            )
            rows = cur.fetchall()
            for r in rows:
                dt = r.get("acquisitiondatetime")
                r["acquisitiondatetime"] = dt.isoformat() if dt else None

            attach_enrollment_display(cur, rows, scope)
            attach_annotations(cur, rows, "series", "seriesinstanceuid")
            attach_inherited_annotations(cur, rows, "series", scope=scope)

        return rows
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Series browsing
# ---------------------------------------------------------------------------


@router.get("/api/series")
def list_series(
    studyinstanceuid: str | None = Query(None),
    seriesinstanceuid: str | None = Query(None),
    label: str | None = Query(None),
    label_level: str | None = Query(None),
    label_filters: str | None = Query(None),
    patient_id: str | None = Query(None),
    import_id: str | None = Query(None),
    import_label: str | None = Query(None),
    dataset: str | None = Query(
        None,
        description=(
            "Exact match on a dataset name; series included if any in-scope "
            "enrollment of its subject is in that dataset."
        ),
    ),
    modality: str | None = Query(None),
    description: str | None = Query(None),
    study_type: str | None = Query(None),
    series_type: list[str] | None = Query(
        None,
        description=(
            "Machine-derived series label. Substring match, repeatable: "
            "?series_type=NCCT&series_type=CTA ORs them. 'NCCT_1' narrows to "
            "each patient's preferred NCCT."
        ),
    ),
    timepoint: list[str] | None = Query(
        None,
        description=(
            "The owning study's machine-derived timepoint (BL / THROMBECTOMY / "
            "FU). Substring match, repeatable (ORed)."
        ),
    ),
    acquisitiondatetime: str | None = Query(None),
    slicethickness: str | None = Query(None),
    scanaxialcoverage: str | None = Query(None),
    sort_by: str = Query("patient_id"),
    sort_dir: str = Query("asc"),
    page: int = Query(1, ge=1),
    per_page: int = Query(50, ge=1, le=500),
    scope: list[str] | None = Depends(get_dataset_scope),
):
    """Paginated series list, optionally filtered."""
    conn = get_conn()
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            conditions = []
            params: list = []

            scope_lit = scope_literal(cur, scope)
            if scope is not None:
                conditions.append(subject_scope_sql("s.subject_id"))
                params.append(scope)

            if label:
                conditions.append(
                    build_label_filter_sql(
                        "series", label_level, "s.seriesinstanceuid", scope_lit=scope_lit
                    )
                )
                params.append(label)
            if studyinstanceuid:
                conditions.append("s.studyinstanceuid LIKE %s")
                params.append(f"%{studyinstanceuid}%")
            if seriesinstanceuid:
                conditions.append("s.seriesinstanceuid LIKE %s")
                params.append(f"%{seriesinstanceuid}%")
            if patient_id:
                conditions.append(
                    enrollment_match_sql("s.subject_id", scope_lit, "q.patient_id LIKE %s")
                )
                params.append(f"%{patient_id}%")
            if import_id:
                conditions.append("s.import_id::text LIKE %s")
                params.append(f"%{import_id}%")
            if import_label:
                conditions.append("LOWER(COALESCE(s.import_label, '')) LIKE LOWER(%s)")
                params.append(f"%{import_label}%")
            ds = (dataset or "").strip()
            if ds:
                conditions.append(
                    enrollment_match_sql("s.subject_id", scope_lit, "q.dataset = %s")
                )
                params.append(ds)
            if modality:
                conditions.append("UPPER(s.modality) LIKE UPPER(%s)")
                params.append(f"%{modality}%")
            if description:
                conditions.append("LOWER(s.seriesdescription) LIKE LOWER(%s)")
                params.append(f"%{description}%")
            if study_type:
                conditions.append("UPPER(st.study_type) = UPPER(%s)")
                params.append(study_type)
            for expr, vals in (
                (SERIES_TYPE_MATCH_EXPR, series_type),
                (TIMEPOINT_MATCH_EXPR, timepoint),
            ):
                sql, ps = auto_match_sql(expr, vals)
                if sql:
                    conditions.append(sql)
                    params.extend(ps)
            if acquisitiondatetime:
                conditions.append("s.acquisitiondatetime::text LIKE %s")
                params.append(f"%{acquisitiondatetime}%")
            if slicethickness:
                conditions.append("ROUND(s.slicethickness::numeric, 2)::text LIKE %s")
                params.append(f"%{slicethickness}%")
            if scanaxialcoverage:
                conditions.append("ROUND(s.scanaxialcoverage_mm::numeric, 2)::text LIKE %s")
                params.append(f"%{scanaxialcoverage}%")
            apply_label_filters(
                parse_label_filters(label_filters),
                "series", "s.seriesinstanceuid", conditions, params, scope_lit,
            )

            where = "WHERE " + " AND ".join(conditions) if conditions else ""
            offset = (page - 1) * per_page

            cur.execute(
                f"SELECT COUNT(DISTINCT s.seriesinstanceuid) "
                f"FROM {SERIES_FROM_CLAUSE} {where}",
                params,
            )
            total = cur.fetchone()["count"]

            col = sort_by if sort_by in SERIES_SORT_WHITELIST else "patient_id"
            col = SERIES_SORT_OVERRIDES.get(col, col)
            direction = "DESC" if sort_dir.lower() == "desc" else "ASC"

            cur.execute(
                f"""
                SELECT * FROM (
                    SELECT DISTINCT ON (s.seriesinstanceuid)
                        s.seriesinstanceuid,
                        s.studyinstanceuid,
                        s.patient_key,
                        s.subject_id,
                        s.import_id,
                        s.import_label,
                        st.study_type,
                        s.modality,
                        s.seriesdescription,
                        s.acquisitiondatetime,
                        s.number_of_slices,
                        s.slicethickness,
                        s.scanaxialcoverage_mm,
                        {SERIES_AUTO_COLS},
                        {STUDY_AUTO_COLS},
                        s.patient_id
                    FROM {SERIES_FROM_CLAUSE}
                    {where}
                    ORDER BY s.seriesinstanceuid
                ) sub
                ORDER BY sub.{col} {direction} NULLS LAST, sub.seriesinstanceuid ASC
                LIMIT %s OFFSET %s
                """,
                params + [per_page, offset],
            )
            rows = cur.fetchall()
            for r in rows:
                dt = r.get("acquisitiondatetime")
                r["acquisitiondatetime"] = dt.isoformat() if dt else None

            attach_enrollment_display(cur, rows, scope)
            attach_annotations(cur, rows, "series", "seriesinstanceuid")
            attach_inherited_annotations(cur, rows, "series", scope=scope)

        return {"total": total, "page": page, "per_page": per_page, "series": rows}
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# OHIF link resolver
# ---------------------------------------------------------------------------


@router.get("/api/ohif-link/{studyinstanceuid}")
def ohif_link(
    studyinstanceuid: str,
    seriesinstanceuid: str | None = Query(None),
    scope: list[str] | None = Depends(get_dataset_scope),
):
    """Resolve a StudyInstanceUID to an OHIF viewer URL via Orthanc lookup.

    Access and series-membership checks run first, on one connection — before
    any series_cache_state mutation or the Orthanc lookup.
    """
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            ensure_study_access(cur, studyinstanceuid, scope)
            if seriesinstanceuid:
                cur.execute(
                    "SELECT 1 FROM image_series "
                    "WHERE studyinstanceuid = %s AND seriesinstanceuid = %s "
                    "LIMIT 1",
                    (studyinstanceuid, seriesinstanceuid),
                )
                if cur.fetchone() is None:
                    raise HTTPException(
                        status_code=404,
                        detail="Series not found in study",
                    )

        if STORAGE_MODE == "cold_path_cache":
            # Per-series preview keys off the series' own warm state (so sifting
            # through independent series is fast); a study open (no series UID)
            # keeps the whole-study aggregate behaviour.
            cs = (
                get_series_cache_status(seriesinstanceuid)
                if seriesinstanceuid
                else get_cache_status(studyinstanceuid)
            )
            st = cs.get("status") or "cold"
            if st in ("warming", "queued"):
                return {"status": st, "url": None}
            if st == "cold":
                detail = (
                    "Series not warmed yet; POST /api/series/{uid}/warm first"
                    if seriesinstanceuid
                    else "Study not warmed yet; POST /api/studies/{uid}/warm first"
                )
                return {"status": "cold", "url": None, "detail": detail}
            if st == "error":
                raise HTTPException(
                    status_code=503,
                    detail=cs.get("error_message") or "Hot cache error for this study",
                )
            # st == "hot": verify files really exist; repair stale rows if not.
            with conn.cursor() as cur:
                if seriesinstanceuid:
                    cur.execute(
                        "SELECT dicom_dir_path FROM image_series "
                        "WHERE seriesinstanceuid = %s AND dicom_dir_path IS NOT NULL "
                        "LIMIT 1",
                        (seriesinstanceuid,),
                    )
                else:
                    cur.execute(
                        "SELECT dicom_dir_path FROM image_series "
                        "WHERE studyinstanceuid = %s AND dicom_dir_path IS NOT NULL "
                        "LIMIT 1",
                        (studyinstanceuid,),
                    )
                row = cur.fetchone()
            files_present = False
            if row and row[0]:
                try:
                    files_present = bool(os.listdir(row[0]))
                except OSError:
                    files_present = False
            if not files_present:
                with conn.cursor() as cur:
                    if seriesinstanceuid:
                        cur.execute(
                            "DELETE FROM series_cache_state WHERE seriesinstanceuid = %s",
                            (seriesinstanceuid,),
                        )
                    else:
                        cur.execute(
                            "DELETE FROM series_cache_state WHERE seriesinstanceuid IN "
                            "(SELECT seriesinstanceuid FROM image_series "
                            " WHERE studyinstanceuid = %s)",
                            (studyinstanceuid,),
                        )
                conn.commit()
                return {
                    "status": "cold",
                    "url": None,
                    "detail": "Cache state was stale; files missing on disk",
                }
            if seriesinstanceuid:
                touch_access_series(seriesinstanceuid)
            else:
                touch_access(studyinstanceuid)
    finally:
        conn.close()

    entries = orthanc_lookup(studyinstanceuid)
    if not entries:
        raise HTTPException(status_code=502, detail="Orthanc lookup failed")
    for entry in entries:
        if entry.get("Type") == "Study":
            query = {"StudyInstanceUIDs": studyinstanceuid}
            if seriesinstanceuid:
                query["SeriesInstanceUIDs"] = seriesinstanceuid
            url = f"/ohif/viewer?{urlencode(query)}"
            if STORAGE_MODE == "cold_path_cache":
                return {"status": "ready", "url": url}
            return {"url": url}
    raise HTTPException(status_code=404, detail="Study not found in Orthanc")


# ---------------------------------------------------------------------------
# DICOM zip download
# ---------------------------------------------------------------------------


@router.get("/api/series/{seriesinstanceuid}/dicom-zip")
def download_dicom_zip(seriesinstanceuid: str, user: str = Depends(require_staff),
                       scope: list[str] | None = Depends(get_dataset_scope)):
    return imaging_download(seriesinstanceuid, scope)


@router.get("/api/series/{seriesinstanceuid}/nifti")
def download_nifti(seriesinstanceuid: str, user: str = Depends(require_staff),
                   scope: list[str] | None = Depends(get_dataset_scope)):
    return imaging_download(seriesinstanceuid, scope, nifti=True)


# ---------------------------------------------------------------------------
# Series filesystem paths
# ---------------------------------------------------------------------------


@router.get("/api/series/{seriesinstanceuid}/paths")
def get_series_paths(
    seriesinstanceuid: str,
    user: str = Depends(require_admin),
):
    """Filesystem paths for a series: loose DICOM directory + compressed
    archive. Admin-only like the zip download — server paths are operational
    detail, exposed for the copy-path quick actions in the table."""
    conn = get_conn()
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(
                "SELECT dicom_dir_path, dicom_archive_path "
                "FROM image_series WHERE seriesinstanceuid = %s LIMIT 1",
                (seriesinstanceuid,),
            )
            row = cur.fetchone()
    finally:
        conn.close()

    if not row:
        raise HTTPException(status_code=404, detail="Series not found")

    # Empty string means "no path" in these columns; normalize to null.
    return {
        "dicom_dir_path": row.get("dicom_dir_path") or None,
        "dicom_archive_path": row.get("dicom_archive_path") or None,
    }
