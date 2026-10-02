"""Per-user dataset (cohort) access scopes.

Each ``patient`` row is an enrollment in one dataset (``patient.dataset``);
enrollments of the same person share a ``subject_id``, and a study belongs to
the whole subject. A user's scope is the set of dataset names they may see,
from ``users.allowed_datasets``:

 - admins are unrestricted — their scope is the ``None`` sentinel;
 - non-admins see a study when any enrollment of its subject is in a granted
   dataset;
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
# study/PatientID → datasets changes only at ingest and when enrollments are
# linked or unlinked (scripts/admin/link_patients.py, out of process). An
# unlink can therefore take up to this long to revoke cross-dataset access in a
# running app; link_patients.py tells the operator to restart it.
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
    """Datasets of every enrollment of the study's subject; None if unknown."""
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT st.studyinstanceuid, q.dataset FROM image_study st "
                "LEFT JOIN patient q ON q.subject_id = st.subject_id "
                "WHERE st.studyinstanceuid = %s",
                (studyinstanceuid,),
            )
            rows = cur.fetchall()
    finally:
        conn.close()
    if not rows:
        return None
    return frozenset(r[1] for r in rows if r[1] is not None)


def fetch_patient_id_subjects(patient_id: str) -> tuple | None:
    """Per subject with imaging under a DICOM PatientID: its datasets and studies.

    OHIF's study browser searches QIDO by PatientID (0010,0020). That is the
    bare id in the files, and Orthanc groups patients by it alone — so one
    PatientID can cover *different people* from different datasets (same id,
    separate enrollments and subjects). Returns a tuple of
    ``(datasets, study_uids)`` frozenset pairs, one per subject, or None when
    no study carries that PatientID (a QIDO wildcard pattern matches nothing
    and is therefore denied for non-admins).
    """
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT st.subject_id, array_agg(DISTINCT st.studyinstanceuid), "
                "       (SELECT array_agg(DISTINCT q.dataset) FROM patient q "
                "        WHERE q.subject_id = st.subject_id) "
                "FROM image_study st WHERE st.patient_id = %s "
                "GROUP BY st.subject_id",
                (patient_id,),
            )
            rows = cur.fetchall()
    finally:
        conn.close()
    if not rows:
        return None
    return tuple(
        (frozenset(datasets or []), frozenset(study_uids or []))
        for _subject, study_uids, datasets in rows
    )


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


def get_patient_id_subjects_cached(patient_id: str) -> tuple | None:
    cached = _patient_cache.get(patient_id)
    if cached is not None:
        return None if cached == _UNKNOWN else cached
    subjects = fetch_patient_id_subjects(patient_id)
    _patient_cache.set(patient_id, _UNKNOWN if subjects is None else subjects)
    return subjects


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


def visible_patient_id_studies(scope: Scope, subjects: tuple | None) -> tuple[frozenset, bool]:
    """Studies under a PatientID the scope may see, and whether that is all of them.

    ``subjects`` comes from :func:`get_patient_id_subjects_cached`. A subject is
    visible when any of its enrollments is in scope.
    """
    if not subjects:
        return frozenset(), False
    visible = [uids for datasets, uids in subjects if scope_allows(scope, datasets)]
    return frozenset().union(*visible), len(visible) == len(subjects)
