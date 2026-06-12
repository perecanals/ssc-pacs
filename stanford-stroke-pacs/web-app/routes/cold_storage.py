"""Cold storage endpoints: warm, evict, cache-status, storage-mode."""

from __future__ import annotations

import asyncio

from fastapi import APIRouter, Body, Depends, HTTPException, Request

from auth import get_current_user, get_dataset_scope
from cache_manager import (
    estimate_warm_disk_space,
    evict_study,
    get_batch_cache_status,
    get_cache_status,
    get_patient_cache_status,
    get_patients_cache_status,
    list_patient_study_uids,
    mark_queued,
    warm_study,
)
from common import ensure_patient_access, ensure_study_access
from config import STORAGE_MODE
from db import get_conn
from metrics import cold_storage_evict_total, cold_storage_warm_total

router = APIRouter()


def _check_study_access(studyinstanceuid: str, scope: list[str] | None) -> None:
    """404 if the study's patient is outside the caller's dataset scope."""
    if scope is None:
        return
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            ensure_study_access(cur, studyinstanceuid, scope)
    finally:
        conn.close()


def _check_patient_access(patient_id: str, scope: list[str] | None) -> None:
    if scope is None:
        return
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            ensure_patient_access(cur, patient_id, scope)
    finally:
        conn.close()


def _filter_in_scope(uids: list[str], patient_ids: list[str], scope: list[str]):
    """Narrow batch ids to those within scope (silently drops the rest)."""
    if not uids and not patient_ids:
        return uids, patient_ids
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            if uids:
                cur.execute(
                    "SELECT st.studyinstanceuid FROM image_study st "
                    "JOIN patient p ON p.patient_id = st.patient_id "
                    "WHERE st.studyinstanceuid = ANY(%s) AND p.dataset && %s::text[]",
                    (uids, scope),
                )
                uids = [r[0] for r in cur.fetchall()]
            if patient_ids:
                cur.execute(
                    "SELECT patient_id FROM patient "
                    "WHERE patient_id = ANY(%s) AND dataset && %s::text[]",
                    (patient_ids, scope),
                )
                patient_ids = [r[0] for r in cur.fetchall()]
    finally:
        conn.close()
    return uids, patient_ids


@router.get("/api/storage-mode")
def api_storage_mode(user: str = Depends(get_current_user)):
    return {"storage_mode": STORAGE_MODE}


def _run_warm_with_metrics(studyinstanceuid: str) -> None:
    """Run warm_study in a worker thread and emit the result-labelled counter.

    Runs in the bounded ``app.state.warm_executor`` pool. Failure here does
    not surface to the HTTP client (which already returned 202); the worker's
    own except block in :func:`cache_manager.warm_study` sets
    ``cache_state.status='error'`` so the frontend learns via the
    ``/cache-status`` poll loop.
    """
    try:
        result = warm_study(studyinstanceuid)
    except Exception:
        cold_storage_warm_total.labels(result="failure").inc()
        raise
    cold_storage_warm_total.labels(
        result="success" if result.get("ok") else "failure"
    ).inc()


@router.post("/api/studies/{studyinstanceuid}/warm", status_code=202)
async def api_warm_study(
    studyinstanceuid: str,
    request: Request,
    scope: list[str] | None = Depends(get_dataset_scope),
):
    """Queue a study for warming. Returns 202 immediately.

    The actual tar+zstd extraction runs in a bounded worker pool
    (``app.state.warm_executor``). Clients observe progress by polling
    ``GET /api/studies/{uid}/cache-status`` until ``status='hot'``.

    A synchronous disk-space precheck still surfaces 507 directly so
    callers get a structured error before any background work is queued.
    """
    _check_study_access(studyinstanceuid, scope)

    est = estimate_warm_disk_space(studyinstanceuid)
    if est and est["available_bytes"] < est["required_bytes"]:
        cold_storage_warm_total.labels(result="insufficient_disk_space").inc()
        raise HTTPException(
            status_code=507,
            detail={
                "error": "insufficient_disk_space",
                "required_bytes": est["required_bytes"],
                "available_bytes": est["available_bytes"],
                "target": str(est["target"]),
            },
        )

    # Persist the 'queued' marker before submitting so the badge survives a
    # reload and is visible to other users (the executor queue is in-process).
    mark_queued([studyinstanceuid])
    loop = asyncio.get_running_loop()
    loop.run_in_executor(
        request.app.state.warm_executor,
        _run_warm_with_metrics,
        studyinstanceuid,
    )
    return {"ok": True, "queued": True, "studyinstanceuid": studyinstanceuid}


@router.post("/api/studies/{studyinstanceuid}/evict")
def api_evict_study(
    studyinstanceuid: str,
    scope: list[str] | None = Depends(get_dataset_scope),
):
    _check_study_access(studyinstanceuid, scope)
    try:
        result = evict_study(studyinstanceuid)
    except Exception as e:
        cold_storage_evict_total.labels(result="failure").inc()
        raise HTTPException(
            status_code=500,
            detail={"error": "evict_failed", "reason": str(e)[:500]},
        )
    cold_storage_evict_total.labels(result="success").inc()
    return result


@router.get("/api/studies/{studyinstanceuid}/cache-status")
def api_cache_status(
    studyinstanceuid: str,
    scope: list[str] | None = Depends(get_dataset_scope),
):
    _check_study_access(studyinstanceuid, scope)
    return get_cache_status(studyinstanceuid)


@router.post("/api/patients/{patient_id}/warm", status_code=202)
async def api_warm_patient(
    patient_id: str,
    request: Request,
    scope: list[str] | None = Depends(get_dataset_scope),
):
    """Queue every study of a patient for warming. Returns 202 immediately.

    Fans each study into the same bounded ``app.state.warm_executor`` pool as
    the per-study endpoint. Per-study disk-space prechecks run inside the
    worker (``warm_study``), so a single study failing for space does not block
    the rest; clients observe progress via
    ``GET /api/patients/{id}/cache-status``.
    """
    _check_patient_access(patient_id, scope)

    uids = list_patient_study_uids(patient_id)
    # Persist 'queued' markers up front so the whole patient shows Queued
    # immediately and durably, even before the workers start draining them.
    mark_queued(uids)
    loop = asyncio.get_running_loop()
    for uid in uids:
        loop.run_in_executor(
            request.app.state.warm_executor,
            _run_warm_with_metrics,
            uid,
        )
    return {"ok": True, "queued": len(uids), "patient_id": patient_id}


@router.get("/api/patients/{patient_id}/cache-status")
def api_patient_cache_status(
    patient_id: str,
    scope: list[str] | None = Depends(get_dataset_scope),
):
    _check_patient_access(patient_id, scope)
    return get_patient_cache_status(patient_id)


@router.post("/api/cache-status/batch")
def api_batch_cache_status(
    uids: list[str] = Body(default=[]),
    patient_ids: list[str] = Body(default=[]),
    scope: list[str] | None = Depends(get_dataset_scope),
):
    """Cache status for many study UIDs and/or patients in one round-trip.

    Lets the table poll every visible row at once instead of one request per
    row. Returns ``{"studies": {uid: status}, "patients": {id: counts}}``.
    Bounded to keep the ``ANY(%s)`` queries and response small.

    Out-of-scope ids are silently dropped (not rejected): the table polls
    whatever rows are visible, and a wholesale 404 would break the poll loop.
    """
    if len(uids) > 500 or len(patient_ids) > 500:
        raise HTTPException(status_code=413, detail="too_many_ids")
    if scope is not None:
        uids, patient_ids = _filter_in_scope(uids, patient_ids, scope)
    return {
        "studies": get_batch_cache_status(uids),
        "patients": get_patients_cache_status(patient_ids),
    }
