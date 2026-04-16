"""Cold storage endpoints: warm, evict, cache-status, storage-mode."""

from __future__ import annotations

from fastapi import APIRouter, Cookie, HTTPException

from auth import get_current_user
from cache_manager import (
    InsufficientDiskSpaceError,
    evict_study,
    get_cache_status,
    warm_study,
)
from config import STORAGE_MODE
from metrics import cold_storage_evict_total, cold_storage_warm_total

router = APIRouter()


@router.get("/api/storage-mode")
def api_storage_mode():
    return {"storage_mode": STORAGE_MODE}


@router.post("/api/studies/{studyinstanceuid}/warm")
def api_warm_study(studyinstanceuid: str, auth_token: str | None = Cookie(None)):
    get_current_user(auth_token)
    try:
        result = warm_study(studyinstanceuid)
    except InsufficientDiskSpaceError as e:
        cold_storage_warm_total.labels(result="insufficient_disk_space").inc()
        raise HTTPException(
            status_code=507,
            detail={
                "error": "insufficient_disk_space",
                "required_bytes": e.required_bytes,
                "available_bytes": e.available_bytes,
                "target": str(e.target),
            },
        )
    cold_storage_warm_total.labels(
        result="success" if result.get("ok") else "failure"
    ).inc()
    return result


@router.post("/api/studies/{studyinstanceuid}/evict")
def api_evict_study(studyinstanceuid: str, auth_token: str | None = Cookie(None)):
    get_current_user(auth_token)
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
def api_cache_status(studyinstanceuid: str):
    return get_cache_status(studyinstanceuid)
