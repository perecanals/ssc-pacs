"""Admin-only destructive data endpoints: delete a study or series.

Performs a **complete** removal across all three layers, as the web-app service
user (which owns the storage roots — no privilege escalation needed):

- Orthanc: ``orthanc_db`` index + DICOMweb caches (REST delete), plus the
  Folder-Indexer ``indexer-plugin.db`` Files rows (Force ``POST /indexer/scan``
  after the files are gone).
- ``stanford-stroke`` DB: rows + soft-linked side tables + annotations +
  ``*_labelled`` mirrors. Annotations are discarded — captured in
  ``annotations_history`` (append-only), never migrated.
- On disk: the loose ``dicom_dir_path`` tree and the cold ``dicom_archive_path``
  archives.

Ordering is Orthanc → DB → files → indexer purge (the purge must follow file
removal — a Force scan would re-register files still on disk). Each step is
idempotent; a partial failure is reported in the response and re-runnable via
``scripts/admin/delete_study.py``.

Shared deletion core: ``web-app/deletion.py`` (also used by the CLI).
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException

from auth import require_admin
from db import get_conn
from deletion import (
    build_series_deletion_plan,
    build_study_deletion_plan,
    delete_index_and_db,
    purge_indexer_rows,
    remove_files,
)

logger = logging.getLogger(__name__)

router = APIRouter()


def _plan_summary(plan: dict) -> dict:
    """Client-facing projection of a deletion plan (drives the confirm modal)."""
    return {
        "level": plan["level"],
        "studyinstanceuid": plan.get("studyinstanceuid"),
        "seriesinstanceuid": plan.get("seriesinstanceuid"),
        "patient_id": plan["patient_id"],
        "n_series": plan["n_series"],
        "n_annotations_discarded": plan["n_annotations"],
        "orthanc_id": plan["orthanc"]["id"],
        "files": plan["remove_dirs"],
    }


def _do_full_delete(conn, plan: dict, admin: str) -> dict:
    """Run the complete Orthanc + DB + files + indexer-purge removal for one plan."""
    idb = delete_index_and_db(conn, plan, execute=True)
    files = remove_files(plan, execute=True)          # service user owns the roots
    idx = purge_indexer_rows(plan.get("indexer_dirs", []), execute=True)
    label = plan.get("studyinstanceuid") or plan.get("seriesinstanceuid")
    logger.info(
        "admin deleted %s", plan["level"], extra={
            "user": admin, "entity": label,
            "annotations_discarded": idb["annotations"],
            "orthanc_deleted": idb["orthanc_deleted"],
            "dirs_removed": len(files["removed"]),
            "indexer_purged": len(idx["purged"]),
            "indexer_error": idx.get("error"),
        },
    )
    return {
        "deleted": True,
        **idb,
        "files_removed": files["removed"],
        "files_missing": files["missing"],
        "indexer_purged": idx["purged"],
        "indexer_error": idx.get("error"),
    }


@router.get("/api/admin/studies/{studyinstanceuid}/deletion-plan")
def study_deletion_plan(studyinstanceuid: str, admin: str = Depends(require_admin)):
    conn = get_conn()
    try:
        plan = build_study_deletion_plan(conn, studyinstanceuid)
    finally:
        conn.close()
    if plan is None:
        raise HTTPException(status_code=404, detail="Study not found")
    return _plan_summary(plan)


@router.get("/api/admin/series/{seriesinstanceuid}/deletion-plan")
def series_deletion_plan(seriesinstanceuid: str, admin: str = Depends(require_admin)):
    conn = get_conn()
    try:
        plan = build_series_deletion_plan(conn, seriesinstanceuid)
    finally:
        conn.close()
    if plan is None:
        raise HTTPException(status_code=404, detail="Series not found")
    return _plan_summary(plan)


@router.delete("/api/admin/studies/{studyinstanceuid}")
def delete_study_endpoint(studyinstanceuid: str, admin: str = Depends(require_admin)):
    """Completely remove a study (Orthanc + DB + files + indexer purge)."""
    conn = get_conn()
    try:
        plan = build_study_deletion_plan(conn, studyinstanceuid)
        if plan is None:
            raise HTTPException(status_code=404, detail="Study not found")
        return _do_full_delete(conn, plan, admin)
    finally:
        conn.close()


@router.delete("/api/admin/series/{seriesinstanceuid}")
def delete_series_endpoint(seriesinstanceuid: str, admin: str = Depends(require_admin)):
    """Completely remove a single series (parent study row is preserved)."""
    conn = get_conn()
    try:
        plan = build_series_deletion_plan(conn, seriesinstanceuid)
        if plan is None:
            raise HTTPException(status_code=404, detail="Series not found")
        return _do_full_delete(conn, plan, admin)
    finally:
        conn.close()
