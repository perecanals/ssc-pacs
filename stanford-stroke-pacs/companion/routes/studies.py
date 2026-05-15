"""Patient, study, and series browsing endpoints, OHIF link, DICOM zip."""

from __future__ import annotations

import os
import re
import shutil
import tempfile
from pathlib import Path
from urllib.parse import urlencode

import psycopg2.extras
from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import StreamingResponse
from zipstream import ZipStream

from auth import require_admin
from cache_manager import (
    get_cache_status,
    resolve_series_archive,
    touch_access,
    untar_zst,
)
from common import (
    PATIENT_ID_COL,
    SERIES_FROM_CLAUSE,
    SERIES_SORT_WHITELIST,
    apply_label_filters,
    attach_annotations,
    attach_inherited_annotations,
    build_label_filter_sql,
    parse_label_filters,
)
from config import STORAGE_MODE
from db import get_conn
from orthanc_client import orthanc_lookup

router = APIRouter()


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
    label: str | None = Query(None),
    label_level: str | None = Query(None),
    label_filters: str | None = Query(None),
    sort_by: str = Query("patient_id"),
    sort_dir: str = Query("asc"),
    page: int = Query(1, ge=1),
    per_page: int = Query(50, ge=1, le=500),
):
    conn = get_conn()
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            conditions = []
            params: list = []

            conditions.append(
                f"p.{PATIENT_ID_COL} IN (SELECT DISTINCT patient_id FROM image_study)"
            )

            if patient_id:
                conditions.append(f"p.{PATIENT_ID_COL}::text LIKE %s")
                params.append(f"%{patient_id}%")
            if stroke_date:
                conditions.append("p.stroke_date::text LIKE %s")
                params.append(f"%{stroke_date}%")
            sil = (study_import_label or "").strip()
            if sil:
                conditions.append(
                    f"p.{PATIENT_ID_COL} IN ("
                    "SELECT DISTINCT patient_id FROM image_study st WHERE st.import_label = %s "
                    "UNION "
                    "SELECT DISTINCT patient_id FROM image_series s WHERE s.import_label = %s)"
                )
                params.append(sil)
                params.append(sil)
            if label:
                conditions.append(
                    build_label_filter_sql("patient", label_level, f"p.{PATIENT_ID_COL}")
                )
                params.append(label)
            apply_label_filters(
                parse_label_filters(label_filters),
                "patient", f"p.{PATIENT_ID_COL}", conditions, params,
            )

            where = "WHERE " + " AND ".join(conditions)
            offset = (page - 1) * per_page

            cur.execute(
                f"SELECT COUNT(*) FROM lvo_clinical_data p {where}", params
            )
            total = cur.fetchone()["count"]

            col_map = {"patient_id": PATIENT_ID_COL, "stroke_date": "stroke_date"}
            col = col_map.get(sort_by, PATIENT_ID_COL)
            direction = "DESC" if sort_dir.lower() == "desc" else "ASC"

            study_labels_agg = (
                "COALESCE(("
                "  SELECT string_agg(lbl, ', ' ORDER BY lbl) FROM ("
                "    SELECT DISTINCT TRIM(sti.import_label) AS lbl "
                "    FROM image_study sti "
                f"    WHERE sti.patient_id = p.{PATIENT_ID_COL} "
                "      AND sti.import_label IS NOT NULL AND TRIM(sti.import_label) <> '' "
                "    UNION "
                "    SELECT DISTINCT TRIM(s.import_label) AS lbl "
                "    FROM image_series s "
                f"    WHERE s.patient_id = p.{PATIENT_ID_COL} "
                "      AND s.import_label IS NOT NULL AND TRIM(s.import_label) <> '' "
                "  ) u"
                "), '') AS study_import_labels"
            )
            cur.execute(
                f"SELECT p.{PATIENT_ID_COL} AS patient_id, p.stroke_date, {study_labels_agg} "
                f"FROM lvo_clinical_data p {where} "
                f"ORDER BY p.{col} {direction} NULLS LAST, p.{PATIENT_ID_COL} ASC "
                f"LIMIT %s OFFSET %s",
                params + [per_page, offset],
            )
            rows = cur.fetchall()

            attach_annotations(cur, rows, "patient", "patient_id")
            attach_inherited_annotations(cur, rows, "patient")

        return {"total": total, "page": page, "per_page": per_page, "items": rows}
    finally:
        conn.close()


@router.get("/api/patients/{patient_id}/studies")
def patient_studies(
    patient_id: str,
    study_import_label: str | None = Query(
        None,
        description="If set, only studies connected to this import_label are returned.",
    ),
):
    """Studies for a patient (expandable sub-rows); optionally filtered by import_label."""
    conn = get_conn()
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            sil = (study_import_label or "").strip()
            where_st = "st.patient_id = %s"
            qparams: list = [patient_id]
            if sil:
                where_st += (
                    " AND ("
                    "st.import_label = %s OR EXISTS ("
                    "  SELECT 1 FROM image_series s "
                    "  WHERE s.studyinstanceuid = st.studyinstanceuid AND s.import_label = %s"
                    "))"
                )
                qparams.append(sil)
                qparams.append(sil)
            cur.execute(
                "SELECT st.patient_id, st.import_id, st.import_label, st.acquisitiondatetime, st.studyinstanceuid, "
                "st.studydescription, st.study_type, "
                "COALESCE(("
                "  SELECT string_agg(DISTINCT s.modality, ', ' ORDER BY s.modality) "
                "  FROM image_series s WHERE s.studyinstanceuid = st.studyinstanceuid"
                "), '') AS modality "
                "FROM image_study st "
                f"WHERE {where_st} "
                "ORDER BY st.acquisitiondatetime",
                tuple(qparams),
            )
            rows = cur.fetchall()
            for r in rows:
                dt = r.get("acquisitiondatetime")
                r["acquisitiondatetime"] = dt.isoformat() if dt else None

            attach_annotations(cur, rows, "study", "studyinstanceuid")
            attach_inherited_annotations(cur, rows, "study")

        return rows
    finally:
        conn.close()


@router.get("/api/study-import-labels")
def list_study_import_labels():
    """Distinct non-empty `import_label` values (study+series) for patient-level filter UI."""
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT import_label FROM ("
                "  SELECT DISTINCT TRIM(import_label) AS import_label FROM image_study "
                "  WHERE import_label IS NOT NULL AND TRIM(import_label) <> '' "
                "  UNION "
                "  SELECT DISTINCT TRIM(import_label) AS import_label FROM image_series "
                "  WHERE import_label IS NOT NULL AND TRIM(import_label) <> '' "
                ") u ORDER BY import_label",
            )
            return [r[0] for r in cur.fetchall()]
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Study browsing
# ---------------------------------------------------------------------------


@router.get("/api/studies")
def list_studies(
    patient_id: str | None = Query(None),
    import_id: str | None = Query(None),
    import_label: str | None = Query(None),
    modality: str | None = Query(None),
    study_type: str | None = Query(None),
    studydescription: str | None = Query(None),
    acquisitiondatetime: str | None = Query(None),
    label: str | None = Query(None),
    label_level: str | None = Query(None),
    label_filters: str | None = Query(None),
    sort_by: str = Query("patient_id"),
    sort_dir: str = Query("asc"),
    page: int = Query(1, ge=1),
    per_page: int = Query(50, ge=1, le=500),
):
    conn = get_conn()
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            conditions = []
            params: list = []

            if patient_id:
                conditions.append("st.patient_id LIKE %s")
                params.append(f"%{patient_id}%")
            if import_id:
                conditions.append("st.import_id::text LIKE %s")
                params.append(f"%{import_id}%")
            if import_label:
                conditions.append("LOWER(COALESCE(st.import_label, '')) LIKE LOWER(%s)")
                params.append(f"%{import_label}%")
            if study_type:
                conditions.append("UPPER(st.study_type) = UPPER(%s)")
                params.append(study_type)
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
                    build_label_filter_sql("study", label_level, "st.studyinstanceuid")
                )
                params.append(label)
            apply_label_filters(
                parse_label_filters(label_filters),
                "study", "st.studyinstanceuid", conditions, params,
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
            }
            col = col_map.get(sort_by, "patient_id")
            direction = "DESC" if sort_dir.lower() == "desc" else "ASC"

            cur.execute(
                f"SELECT st.patient_id, st.import_id, st.import_label, st.acquisitiondatetime, "
                f"st.studyinstanceuid, st.studydescription, st.study_type, "
                f"COALESCE(("
                f"  SELECT string_agg(DISTINCT s.modality, ', ' ORDER BY s.modality) "
                f"  FROM image_series s WHERE s.studyinstanceuid = st.studyinstanceuid"
                f"), '') AS modality "
                f"FROM image_study st {where} "
                f"ORDER BY st.{col} {direction} NULLS LAST, st.studyinstanceuid ASC "
                f"LIMIT %s OFFSET %s",
                params + [per_page, offset],
            )
            rows = cur.fetchall()
            for r in rows:
                dt = r.get("acquisitiondatetime")
                r["acquisitiondatetime"] = dt.isoformat() if dt else None

            attach_annotations(cur, rows, "study", "studyinstanceuid")
            attach_inherited_annotations(cur, rows, "study")

        return {"total": total, "page": page, "per_page": per_page, "items": rows}
    finally:
        conn.close()


@router.get("/api/studies/{studyinstanceuid}/series")
def study_series(studyinstanceuid: str):
    """All series for a given study (for expandable sub-rows)."""
    conn = get_conn()
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(
                "SELECT s.seriesinstanceuid, s.studyinstanceuid, s.patient_id, s.import_id, s.import_label, "
                "s.modality, s.seriesdescription, s.acquisitiondatetime, s.number_of_slices "
                "FROM image_series s WHERE s.studyinstanceuid = %s "
                "ORDER BY s.acquisitiondatetime, s.seriesdescription",
                (studyinstanceuid,),
            )
            rows = cur.fetchall()
            for r in rows:
                dt = r.get("acquisitiondatetime")
                r["acquisitiondatetime"] = dt.isoformat() if dt else None

            attach_annotations(cur, rows, "series", "seriesinstanceuid")
            attach_inherited_annotations(cur, rows, "series")

        return rows
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Series browsing
# ---------------------------------------------------------------------------


@router.get("/api/series")
def list_series(
    label: str | None = Query(None),
    label_level: str | None = Query(None),
    label_filters: str | None = Query(None),
    patient_id: str | None = Query(None),
    import_id: str | None = Query(None),
    import_label: str | None = Query(None),
    modality: str | None = Query(None),
    description: str | None = Query(None),
    study_type: str | None = Query(None),
    acquisitiondatetime: str | None = Query(None),
    sort_by: str = Query("patient_id"),
    sort_dir: str = Query("asc"),
    page: int = Query(1, ge=1),
    per_page: int = Query(50, ge=1, le=500),
):
    """Paginated series list, optionally filtered."""
    conn = get_conn()
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            conditions = []
            params: list = []

            if label:
                conditions.append(
                    build_label_filter_sql("series", label_level, "s.seriesinstanceuid")
                )
                params.append(label)
            if patient_id:
                conditions.append("s.patient_id LIKE %s")
                params.append(f"%{patient_id}%")
            if import_id:
                conditions.append("s.import_id::text LIKE %s")
                params.append(f"%{import_id}%")
            if import_label:
                conditions.append("LOWER(COALESCE(s.import_label, '')) LIKE LOWER(%s)")
                params.append(f"%{import_label}%")
            if modality:
                conditions.append("UPPER(s.modality) LIKE UPPER(%s)")
                params.append(f"%{modality}%")
            if description:
                conditions.append("LOWER(s.seriesdescription) LIKE LOWER(%s)")
                params.append(f"%{description}%")
            if study_type:
                conditions.append("UPPER(st.study_type) = UPPER(%s)")
                params.append(study_type)
            if acquisitiondatetime:
                conditions.append("s.acquisitiondatetime::text LIKE %s")
                params.append(f"%{acquisitiondatetime}%")
            apply_label_filters(
                parse_label_filters(label_filters),
                "series", "s.seriesinstanceuid", conditions, params,
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
            direction = "DESC" if sort_dir.lower() == "desc" else "ASC"

            cur.execute(
                f"""
                SELECT * FROM (
                    SELECT DISTINCT ON (s.seriesinstanceuid)
                        s.seriesinstanceuid,
                        s.studyinstanceuid,
                        s.patient_id,
                        s.import_id,
                        s.import_label,
                        st.study_type,
                        s.modality,
                        s.seriesdescription,
                        s.acquisitiondatetime,
                        s.number_of_slices
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

            attach_annotations(cur, rows, "series", "seriesinstanceuid")
            attach_inherited_annotations(cur, rows, "series")

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
):
    """Resolve a StudyInstanceUID to an OHIF viewer URL via Orthanc lookup."""
    if STORAGE_MODE == "cold_path_cache":
        cs = get_cache_status(studyinstanceuid)
        st = cs.get("status") or "cold"
        if st == "warming":
            return {"status": "warming", "url": None}
        if st == "cold":
            return {
                "status": "cold",
                "url": None,
                "detail": "Study not warmed yet; POST /api/studies/{uid}/warm first",
            }
        if st == "error":
            raise HTTPException(
                status_code=503,
                detail=cs.get("error_message") or "Hot cache error for this study",
            )
        if st == "hot":
            conn = get_conn()
            try:
                with conn.cursor() as cur:
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
                        cur.execute(
                            "DELETE FROM cache_state WHERE studyinstanceuid = %s",
                            (studyinstanceuid,),
                        )
                    conn.commit()
                    return {
                        "status": "cold",
                        "url": None,
                        "detail": "Cache state was stale; files missing on disk",
                    }
            finally:
                conn.close()
            touch_access(studyinstanceuid)

    entries = orthanc_lookup(studyinstanceuid)
    if not entries:
        raise HTTPException(status_code=502, detail="Orthanc lookup failed")
    for entry in entries:
        if entry.get("Type") == "Study":
            if seriesinstanceuid:
                conn = get_conn()
                try:
                    with conn.cursor() as cur:
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
                finally:
                    conn.close()

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
def download_dicom_zip(
    seriesinstanceuid: str,
    user: str = Depends(require_admin),
):
    """Stream a `.zip` of the series' DICOMs. Admin-only (bulk DICOM export
    is a privilege, not a public read like the browsing endpoints)."""
    conn = get_conn()
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(
                "SELECT patient_id, acquisitiondatetime, seriesdescription, dicom_dir_path, dicom_archive_path "
                "FROM image_series WHERE seriesinstanceuid = %s LIMIT 1",
                (seriesinstanceuid,),
            )
            row = cur.fetchone()
    finally:
        conn.close()

    if not row:
        raise HTTPException(status_code=404, detail="Series not found")

    pid = row.get("patient_id") or "unknown"
    dt = row.get("acquisitiondatetime")
    date_str = dt.strftime("%Y%m%d") if dt else "nodate"
    desc = row.get("seriesdescription") or "series"
    safe = re.sub(r"[^\w\-.]", "_", f"{pid}-{date_str}-{desc}")
    folder_name = re.sub(r"[^\w\-.]", "_", f"{pid}_{desc}")
    filename = f"{safe}.zip"

    if STORAGE_MODE == "cold_path_cache":
        arch = resolve_series_archive(row.get("dicom_archive_path"), row.get("dicom_dir_path"))
        if arch and arch.is_file():
            tmpdir = tempfile.mkdtemp(prefix="dicom-zip-")
            try:
                untar_zst(arch, Path(tmpdir))
                zs = ZipStream.from_path(tmpdir, arcname=folder_name)
                content_length = len(zs)

                def gen():
                    try:
                        yield from zs
                    finally:
                        shutil.rmtree(tmpdir, ignore_errors=True)

                return StreamingResponse(
                    gen(),
                    media_type="application/zip",
                    headers={
                        "Content-Disposition": f'attachment; filename="{filename}"',
                        "Content-Length": str(content_length),
                    },
                )
            except Exception:
                shutil.rmtree(tmpdir, ignore_errors=True)
                raise

    if not row.get("dicom_dir_path"):
        raise HTTPException(status_code=404, detail="DICOM path not found for this series")

    dicom_dir = Path(row["dicom_dir_path"])
    if not dicom_dir.is_dir():
        raise HTTPException(status_code=404, detail=f"DICOM directory does not exist: {dicom_dir}")

    zs = ZipStream.from_path(str(dicom_dir), arcname=folder_name)

    return StreamingResponse(
        zs,
        media_type="application/zip",
        headers={
            "Content-Disposition": f'attachment; filename="{filename}"',
            "Content-Length": str(len(zs)),
        },
    )
