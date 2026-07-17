"""Per-user dataset (cohort) access scopes.

Patients carry cohort tags in ``patient.dataset`` (text[]). A user's scope is
the set of tags they may see, from ``users.allowed_datasets``:

 - admins are unrestricted — their scope is the ``None`` sentinel;
 - non-admins see only patients whose ``dataset`` overlaps their grants;
 - an empty grant set (the default) means deny-by-default: no patient data.

This module is shared by the sync API routes (via the ``get_dataset_scope``
dependency in auth.py), the async DICOMweb proxy (via the cached lookups, so
per-frame requests cost no DB round-trips), and the admin permissions API
(which invalidates the user cache on grant changes).
"""

from __future__ import annotations

import threading
import time

from db import get_conn

# None = unrestricted (admin); a frozenset = allowed dataset tags (may be empty).
Scope = frozenset | None

# Bound staleness after an admin edits grants mid-session.
_USER_TTL_SECONDS = 30.0
# study/patient → datasets is effectively immutable (cohort tags only grow at
# ingest).
_STUDY_TTL_SECONDS = 300.0


class _TTLCache:
    """Tiny thread-safe TTL cache. Eviction is naive (full clear on overflow):
    correctness over cleverness — entries are cheap to refetch."""

    def __init__(self, ttl: float, maxsize: int = 4096):
        self._ttl = ttl
        self._maxsize = maxsize
        self._lock = threading.Lock()
        self._data: dict = {}

    def get(self, key):
        with self._lock:
            entry = self._data.get(key)
            if entry is None:
                return None
            expires, value = entry
            if time.monotonic() > expires:
                del self._data[key]
                return None
            return value

    def set(self, key, value) -> None:
        with self._lock:
            if len(self._data) >= self._maxsize:
                self._data.clear()
            self._data[key] = (time.monotonic() + self._ttl, value)

    def invalidate(self, key) -> None:
        with self._lock:
            self._data.pop(key, None)


_user_cache = _TTLCache(ttl=_USER_TTL_SECONDS)
_study_cache = _TTLCache(ttl=_STUDY_TTL_SECONDS)
_patient_cache = _TTLCache(ttl=_STUDY_TTL_SECONDS)

# Cached sentinels: _TTLCache.get returns None for "miss", so cached values
# must never be None. Admin scope and unknown-study both need distinct markers.
_ADMIN = "__admin__"
_UNKNOWN = "__unknown__"


def fetch_user_scope(username: str) -> Scope:
    """Return the user's dataset scope straight from the DB.

    None = admin (unrestricted). A missing users row resolves to an empty
    scope (deny) — a valid JWT for a deleted user grants nothing.
    """
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT is_admin, allowed_datasets FROM users WHERE username = %s",
                (username,),
            )
            row = cur.fetchone()
    finally:
        conn.close()
    if not row:
        return frozenset()
    if row[0]:
        return None
    return frozenset(row[1] or [])


def fetch_study_datasets(studyinstanceuid: str) -> frozenset | None:
    """Dataset tags of the patient owning a study; None if the study is unknown."""
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT p.dataset FROM image_study st "
                "JOIN patient p ON p.patient_id = st.patient_id "
                "WHERE st.studyinstanceuid = %s LIMIT 1",
                (studyinstanceuid,),
            )
            row = cur.fetchone()
    finally:
        conn.close()
    if not row:
        return None
    return frozenset(row[0] or [])


def fetch_patient_datasets(patient_id: str) -> frozenset | None:
    """Dataset tags of a patient; None if the patient is unknown.

    Keyed by ``patient.patient_id`` — the value OHIF sends as the DICOM
    PatientID (0010,0020) in QIDO patient-scoped searches. A QIDO wildcard
    pattern simply matches no row and resolves to None (deny for non-admins).
    """
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT dataset FROM patient WHERE patient_id = %s LIMIT 1",
                (patient_id,),
            )
            row = cur.fetchone()
    finally:
        conn.close()
    if not row:
        return None
    return frozenset(row[0] or [])


def get_user_scope_cached(username: str) -> Scope:
    cached = _user_cache.get(username)
    if cached is not None:
        return None if cached == _ADMIN else cached
    scope = fetch_user_scope(username)
    _user_cache.set(username, _ADMIN if scope is None else scope)
    return scope


def get_study_datasets_cached(studyinstanceuid: str) -> frozenset | None:
    cached = _study_cache.get(studyinstanceuid)
    if cached is not None:
        return None if cached == _UNKNOWN else cached
    datasets = fetch_study_datasets(studyinstanceuid)
    _study_cache.set(studyinstanceuid, _UNKNOWN if datasets is None else datasets)
    return datasets


def get_patient_datasets_cached(patient_id: str) -> frozenset | None:
    cached = _patient_cache.get(patient_id)
    if cached is not None:
        return None if cached == _UNKNOWN else cached
    datasets = fetch_patient_datasets(patient_id)
    _patient_cache.set(patient_id, _UNKNOWN if datasets is None else datasets)
    return datasets


def invalidate_user_scope(username: str) -> None:
    """Drop a user's cached scope so grant changes apply immediately."""
    _user_cache.invalidate(username)


def clear_caches() -> None:
    """Drop all cached scopes/datasets (test isolation, ops escape hatch)."""
    for cache in (_user_cache, _study_cache, _patient_cache):
        with cache._lock:
            cache._data.clear()


def scope_allows(scope: Scope, datasets: frozenset | None) -> bool:
    """True if a scope may access an entity with the given dataset tags."""
    if scope is None:
        return True
    if not datasets:
        return False
    return bool(scope & datasets)
