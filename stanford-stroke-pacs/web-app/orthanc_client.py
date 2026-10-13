"""Thin wrapper around Orthanc REST API calls used by the web app.

Single source of truth for the Orthanc service-account credentials
(ORTHANC_URL / ORTHANC_USER / ORTHANC_PASS) — reconciliation.py and
routes/proxy.py import them from here.
"""

from __future__ import annotations

import gzip
import json
import logging
import os
import time
from typing import Any

import requests

from db import require_env  # importing db loads .env

logger = logging.getLogger(__name__)

ORTHANC_URL = os.getenv("ORTHANC_URL", "http://localhost:8042")
ORTHANC_USER = require_env("ORTHANC_ADMIN_USER")
ORTHANC_PASS = require_env("ORTHANC_ADMIN_PASSWORD")

# --- DICOMweb series-metadata cache ----------------------------------------
#
# The DICOMweb plugin caches each series' WADO-RS metadata as an Orthanc
# attachment, and it builds that cache by READING THE DICOM FILES. In
# cold_path_cache mode the files are absent whenever a series is evicted, and
# the Orthanc index deliberately keeps pointing at them
# ("RemoveMissingFiles": false), so nothing ever invalidates the cache.
#
# A cache computed while the files are absent is stored as an empty JSON array
# and is then PERMANENT: every later metadata request 400s with
# "The series metadata json does not contain an array", which hangs OHIF on
# the loading spinner. A healthy cache, by contrast, keeps serving fine after
# eviction — metadata comes from the cache, only pixels need the files.
#
# Hence the invariant these helpers exist to protect:
#
#   *** Only ever build this cache while the series' files are on disk. ***
#
# Corollary: deleting a poisoned cache is not a fix on its own — if anything
# requests the metadata again while the series is cold, it is re-poisoned on
# the spot. Always DELETE + rebuild in one go, while warm.
DICOMWEB_SERIES_METADATA_ATTACHMENT = 4301

# The poisoned payload is "<revision>;<signature>;" + gzipped "[]" == 57 bytes.
# A real cache is >1 KB (one entry per instance); 64 is a safe ceiling for
# "this cache is the empty-array form".
EMPTY_METADATA_CACHE_MAX_BYTES = 64


def _http(session: requests.Session | None):
    """Return an object exposing .get/.post/.delete with Orthanc auth applied."""
    if session is not None:
        return session, {}
    return requests, {"auth": (ORTHANC_USER, ORTHANC_PASS)}


def series_metadata_cache_bytes(
    orthanc_series_id: str,
    *,
    session: requests.Session | None = None,
    timeout: int = 10,
) -> int | None:
    """Uncompressed size of the series' cached DICOMweb metadata, or None if absent."""
    http, kw = _http(session)
    try:
        resp = http.get(
            f"{ORTHANC_URL}/series/{orthanc_series_id}/attachments/"
            f"{DICOMWEB_SERIES_METADATA_ATTACHMENT}/info",
            timeout=timeout,
            **kw,
        )
    except requests.RequestException:
        return None
    if resp.status_code != 200:
        return None
    try:
        return int(resp.json()["UncompressedSize"])
    except (ValueError, KeyError, TypeError):
        return None


def metadata_instance_count(body: Any) -> int | None:
    """Count distinct SOP instances; reject malformed or duplicate entries.

    Count instances, not NumberOfFrames: one multiframe DICOM is one entry.
    """
    if not isinstance(body, list) or not body:
        return None
    try:
        values = [entry["00080018"]["Value"] for entry in body]
        if any(not isinstance(value, list) or len(value) != 1 for value in values):
            return None
        uids = [value[0] for value in values]
        if any(not isinstance(uid, str) or not uid for uid in uids):
            return None
        return len(uids) if len(set(uids)) == len(uids) else None
    except (KeyError, IndexError, TypeError):
        return None


def series_metadata_cache_is_healthy(
    orthanc_series_id: str,
    *,
    session: requests.Session | None = None,
    timeout: int = 10,
    expected_instances: int | None = None,
) -> bool:
    """Verify cached distinct-instance count matches the Orthanc index.

    Read attachment 4301 directly: never request WADO metadata here, since
    that can create a poisoned cache while cold. No DICOM files are read.
    Unknown attachment formats fail closed. Optional expected_instances also
    fences cleanup against an incomplete Orthanc index.
    """
    size = series_metadata_cache_bytes(
        orthanc_series_id, session=session, timeout=timeout
    )
    if size is None or size <= EMPTY_METADATA_CACHE_MAX_BYTES:
        return False
    http, kw = _http(session)
    try:
        attachment = http.get(
            f"{ORTHANC_URL}/series/{orthanc_series_id}/attachments/"
            f"{DICOMWEB_SERIES_METADATA_ATTACHMENT}/data", timeout=timeout, **kw,
        )
        if attachment.status_code != 200:
            return False
        # DICOMweb cache revision 2: ASCII revision;signature; + gzip JSON.
        revision, signature, payload = attachment.content.split(b";", 2)
        if revision != b"2" or not signature:
            return False
        count = metadata_instance_count(json.loads(gzip.decompress(payload)))
        if count is None or (expected_instances is not None and count != expected_instances):
            return False
        series = http.get(
            f"{ORTHANC_URL}/series/{orthanc_series_id}", timeout=timeout, **kw,
        )
        return series.status_code == 200 and count == len(series.json()["Instances"])
    except (requests.RequestException, ValueError, OSError, EOFError, KeyError, TypeError):
        return False


def rebuild_series_metadata_cache(
    studyinstanceuid: str,
    seriesinstanceuid: str,
    orthanc_series_id: str,
    *,
    session: requests.Session | None = None,
    timeout: int = 120,
) -> bool:
    """Drop the cached DICOMweb metadata and rebuild it from the files on disk.

    **The caller must guarantee the series is warm.** Rebuilding while the files
    are absent re-poisons the cache with an empty array (permanent HTTP 400) —
    that is precisely the bug this exists to repair.

    Returns True iff the rebuilt metadata covers all indexed instances.
    """
    http, kw = _http(session)
    try:
        # 404 here is fine: the cache may simply not exist yet.
        deleted = http.delete(
            f"{ORTHANC_URL}/series/{orthanc_series_id}/attachments/"
            f"{DICOMWEB_SERIES_METADATA_ATTACHMENT}",
            timeout=timeout,
            **kw,
        )
        if deleted.status_code not in (200, 204, 404):
            return False
        # This GET is what rebuilds the cache, reading the on-disk instances.
        resp = http.get(
            f"{ORTHANC_URL}/dicom-web/studies/{studyinstanceuid}"
            f"/series/{seriesinstanceuid}/metadata",
            timeout=timeout,
            **kw,
        )
    except requests.RequestException as exc:
        logger.warning(
            "dicomweb metadata cache rebuild failed for series %s: %s",
            seriesinstanceuid, exc,
        )
        return False
    if resp.status_code != 200:
        return False
    try:
        body = resp.json()
    except ValueError:
        return False
    if metadata_instance_count(body) is None:
        return False
    return series_metadata_cache_is_healthy(
        orthanc_series_id, session=session, timeout=timeout,
    )


def wait_for_series_metadata_cache(
    orthanc_series_id: str,
    *,
    timeout_s: float = 90.0,
    poll_s: float = 2.0,
    session: requests.Session | None = None,
    expected_instances: int | None = None,
) -> bool:
    """Block until cached metadata covers all indexed and expected instances.

    Orthanc's DICOMweb plugin builds the cache in a background worker when the
    series goes stable (``StableAge``). Ingestion must not delete a series' loose
    DICOMs before that has happened — hence this wait. Waiting for Orthanc's own
    write (rather than forcing one ourselves) also guarantees the stable-series
    worker has already run against files that were present, so no later
    recomputation can overwrite the cache with an empty array.

    Returns False on timeout (caller should then keep the loose files).
    """
    deadline = time.monotonic() + timeout_s
    while True:
        if series_metadata_cache_is_healthy(
            orthanc_series_id, session=session, expected_instances=expected_instances,
        ):
            return True
        if time.monotonic() >= deadline:
            return False
        time.sleep(poll_s)


def orthanc_series_id(
    seriesinstanceuid: str,
    *,
    session: requests.Session | None = None,
    timeout: int = 10,
) -> str | None:
    """Resolve a SeriesInstanceUID to Orthanc's internal series ID."""
    http, kw = _http(session)
    try:
        resp = http.post(
            f"{ORTHANC_URL}/tools/lookup", data=seriesinstanceuid, timeout=timeout, **kw
        )
    except requests.RequestException:
        return None
    if resp.status_code != 200:
        return None
    for entry in resp.json():
        if entry.get("Type") == "Series":
            return entry.get("ID")
    return None


def orthanc_study_id(
    studyinstanceuid: str,
    *,
    session: requests.Session | None = None,
    timeout: int = 10,
) -> str | None:
    """Resolve a StudyInstanceUID to Orthanc's internal study ID (or None)."""
    http, kw = _http(session)
    try:
        resp = http.post(
            f"{ORTHANC_URL}/tools/lookup", data=studyinstanceuid, timeout=timeout, **kw
        )
    except requests.RequestException:
        return None
    if resp.status_code != 200:
        return None
    for entry in resp.json():
        if entry.get("Type") == "Study":
            return entry.get("ID")
    return None


def _orthanc_delete_resource(
    kind: str,
    orthanc_id: str,
    *,
    session: requests.Session | None = None,
    timeout: int = 60,
) -> bool:
    """DELETE a Study/Series resource from Orthanc by internal ID.

    Orthanc's REST delete removes the core index resource and DICOMweb
    caches online. Indexer Files rows can survive; callers must separately
    purge them with a scoped Force scan when re-registering or deleting files.
    Idempotent: a 404
    (already gone) counts as success. Returns False only on a real error.
    """
    http, kw = _http(session)
    try:
        resp = http.delete(f"{ORTHANC_URL}/{kind}/{orthanc_id}", timeout=timeout, **kw)
    except requests.RequestException as exc:
        logger.warning("orthanc DELETE /%s/%s failed: %s", kind, orthanc_id, exc)
        return False
    if resp.status_code in (200, 404):
        return True
    logger.warning(
        "orthanc DELETE /%s/%s -> HTTP %s", kind, orthanc_id, resp.status_code
    )
    return False


def delete_orthanc_study(
    orthanc_id: str,
    *,
    session: requests.Session | None = None,
    timeout: int = 60,
) -> bool:
    """DELETE a study from Orthanc by internal ID (idempotent — 404 ⇒ True)."""
    return _orthanc_delete_resource(
        "studies", orthanc_id, session=session, timeout=timeout
    )


def delete_orthanc_series(
    orthanc_id: str,
    *,
    session: requests.Session | None = None,
    timeout: int = 60,
) -> bool:
    """DELETE a series from Orthanc by internal ID (idempotent — 404 ⇒ True)."""
    return _orthanc_delete_resource(
        "series", orthanc_id, session=session, timeout=timeout
    )


def orthanc_lookup(studyinstanceuid: str, *, timeout: int = 5) -> list[dict[str, Any]]:
    """POST /tools/lookup — resolve a DICOM UID to Orthanc internal IDs."""
    resp = requests.post(
        f"{ORTHANC_URL}/tools/lookup",
        data=studyinstanceuid,
        auth=(ORTHANC_USER, ORTHANC_PASS),
        timeout=timeout,
    )
    if resp.status_code != 200:
        return []
    return resp.json()


def orthanc_system_check(*, timeout: int = 3) -> tuple[str, str | None]:
    """GET /system — returns ``("ok", None)`` or ``("error", detail)``."""
    try:
        resp = requests.get(
            f"{ORTHANC_URL}/system",
            auth=(ORTHANC_USER, ORTHANC_PASS),
            timeout=timeout,
        )
        if resp.status_code == 200:
            return "ok", None
        return f"http_{resp.status_code}", None
    except Exception as e:
        return "error", str(e)[:200]
