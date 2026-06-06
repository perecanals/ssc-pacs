"""Two-DB reconciliation: compare image_series (stanford-stroke) vs Orthanc index.

Read-only observer — never mutates either database or the filesystem.

Public API
----------
- ``diff_image_series_vs_orthanc(conn, session)`` → detailed mismatch report
- ``snapshot_summary(report)`` → counts-only dict suitable for metrics / JSON
"""

from __future__ import annotations

import logging
import os
import time
from pathlib import Path
from typing import Any

import psycopg2.extras
import requests
from dotenv import load_dotenv

_REPO_ROOT = Path(__file__).resolve().parent.parent
load_dotenv(_REPO_ROOT / ".env")

logger = logging.getLogger(__name__)

ORTHANC_URL = os.getenv("ORTHANC_URL", "http://localhost:8042")
ORTHANC_USER = os.getenv("ORTHANC_ADMIN_USER", "")
ORTHANC_PASS = os.getenv("ORTHANC_ADMIN_PASSWORD", "")

# Page size for paginating Orthanc's /series listing.
_ORTHANC_PAGE_SIZE = 500


# ---------------------------------------------------------------------------
# Orthanc helpers
# ---------------------------------------------------------------------------

def _orthanc_session() -> requests.Session:
    """Build an authenticated requests.Session for Orthanc."""
    s = requests.Session()
    s.auth = (ORTHANC_USER, ORTHANC_PASS)
    return s


def _get_orthanc_series_uids(session: requests.Session) -> set[str]:
    """Fetch all SeriesInstanceUIDs currently indexed in Orthanc.

    Paginates through ``GET /series`` and resolves each Orthanc internal ID
    to a DICOM SeriesInstanceUID via ``GET /series/{id}``.
    """
    orthanc_ids: list[str] = []
    offset = 0
    while True:
        resp = session.get(
            f"{ORTHANC_URL}/series",
            params={"since": offset, "limit": _ORTHANC_PAGE_SIZE},
            timeout=30,
        )
        resp.raise_for_status()
        batch = resp.json()
        if not batch:
            break
        orthanc_ids.extend(batch)
        if len(batch) < _ORTHANC_PAGE_SIZE:
            break
        offset += _ORTHANC_PAGE_SIZE

    uids: set[str] = set()
    for oid in orthanc_ids:
        try:
            resp = session.get(f"{ORTHANC_URL}/series/{oid}", timeout=10)
            resp.raise_for_status()
            uid = resp.json().get("MainDicomTags", {}).get("SeriesInstanceUID")
            if uid:
                uids.add(uid)
        except Exception:
            logger.warning("Failed to resolve Orthanc series %s", oid)
    return uids


def _get_orthanc_statistics(session: requests.Session) -> dict[str, Any]:
    """GET /statistics from Orthanc."""
    try:
        resp = session.get(f"{ORTHANC_URL}/statistics", timeout=10)
        resp.raise_for_status()
        return resp.json()
    except Exception as exc:
        logger.warning("Could not fetch Orthanc statistics: %s", exc)
        return {}


# ---------------------------------------------------------------------------
# DB helpers
# ---------------------------------------------------------------------------

def _get_db_series(conn) -> list[dict[str, Any]]:
    """Return all image_series rows with a non-null seriesinstanceuid."""
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(
            "SELECT seriesinstanceuid, studyinstanceuid, patient_id, "
            "       modality, seriesdescription, dicom_dir_path, "
            "       dicom_archive_path "
            "FROM image_series "
            "WHERE seriesinstanceuid IS NOT NULL "
            "ORDER BY patient_id, studyinstanceuid, seriesinstanceuid"
        )
        return cur.fetchall()


# ---------------------------------------------------------------------------
# Core reconciliation
# ---------------------------------------------------------------------------

def diff_image_series_vs_orthanc(
    conn,
    session: requests.Session | None = None,
) -> dict[str, Any]:
    """Run a full reconciliation between ``image_series`` and Orthanc.

    Parameters
    ----------
    conn : psycopg2 connection
        Connection to the ``stanford-stroke`` database.
    session : requests.Session, optional
        Authenticated session for the Orthanc REST API.  A default one is
        created if not supplied.

    Returns
    -------
    dict
        A report dict with keys: ``timestamp``, ``duration_seconds``,
        ``orthanc_statistics``, ``db_series_count``, ``orthanc_series_count``,
        ``mismatches`` (dict of category → list of detail dicts), ``summary``.
    """
    if session is None:
        session = _orthanc_session()

    t0 = time.time()

    # 1. DB rows
    db_rows = _get_db_series(conn)
    db_by_uid = {r["seriesinstanceuid"]: r for r in db_rows}
    db_uids = set(db_by_uid.keys())

    # 2. Orthanc UIDs
    orthanc_uids = _get_orthanc_series_uids(session)

    # 3. Orthanc statistics
    orthanc_stats = _get_orthanc_statistics(session)

    # 4. Classify mismatches
    in_db_not_in_orthanc: list[dict[str, Any]] = []
    in_orthanc_not_in_db: list[dict[str, Any]] = []
    dicom_dir_missing: list[dict[str, Any]] = []
    dicom_archive_missing: list[dict[str, Any]] = []

    # Series in DB but not indexed by Orthanc
    for uid in sorted(db_uids - orthanc_uids):
        row = db_by_uid[uid]
        in_db_not_in_orthanc.append({
            "seriesinstanceuid": uid,
            "patient_id": row.get("patient_id"),
            "modality": row.get("modality"),
            "seriesdescription": row.get("seriesdescription"),
        })

    # Series in Orthanc but not in DB
    for uid in sorted(orthanc_uids - db_uids):
        in_orthanc_not_in_db.append({"seriesinstanceuid": uid})

    # dicom_dir_path set but directory does not exist on disk
    for row in db_rows:
        dpath = row.get("dicom_dir_path")
        if dpath and dpath.strip():
            if not Path(dpath).is_dir():
                dicom_dir_missing.append({
                    "seriesinstanceuid": row["seriesinstanceuid"],
                    "patient_id": row.get("patient_id"),
                    "dicom_dir_path": dpath,
                })

    # dicom_archive_path set but archive file does not exist on disk
    for row in db_rows:
        apath = row.get("dicom_archive_path")
        if apath and apath.strip():
            if not Path(apath).is_file():
                dicom_archive_missing.append({
                    "seriesinstanceuid": row["seriesinstanceuid"],
                    "patient_id": row.get("patient_id"),
                    "dicom_archive_path": apath,
                })

    duration = round(time.time() - t0, 2)

    mismatches = {
        "in_db_not_in_orthanc": in_db_not_in_orthanc,
        "in_orthanc_not_in_db": in_orthanc_not_in_db,
        "dicom_dir_missing": dicom_dir_missing,
        "dicom_archive_missing": dicom_archive_missing,
    }

    report = {
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "duration_seconds": duration,
        "orthanc_statistics": orthanc_stats,
        "db_series_count": len(db_uids),
        "orthanc_series_count": len(orthanc_uids),
        "mismatches": mismatches,
        "summary": snapshot_summary_from_mismatches(mismatches, len(db_uids), len(orthanc_uids)),
    }
    return report


def snapshot_summary_from_mismatches(
    mismatches: dict[str, list],
    db_count: int,
    orthanc_count: int,
) -> dict[str, Any]:
    """Build a counts-only summary from a mismatches dict."""
    total = sum(len(v) for v in mismatches.values())
    matched = db_count - len(mismatches.get("in_db_not_in_orthanc", []))
    coverage = round(matched / db_count * 100, 1) if db_count else 0.0
    return {
        "total_mismatches": total,
        "in_db_not_in_orthanc": len(mismatches.get("in_db_not_in_orthanc", [])),
        "in_orthanc_not_in_db": len(mismatches.get("in_orthanc_not_in_db", [])),
        "dicom_dir_missing": len(mismatches.get("dicom_dir_missing", [])),
        "dicom_archive_missing": len(mismatches.get("dicom_archive_missing", [])),
        "db_series_count": db_count,
        "orthanc_series_count": orthanc_count,
        "matched": matched,
        "coverage_percent": coverage,
    }


def snapshot_summary(report: dict[str, Any]) -> dict[str, Any]:
    """Extract the counts-only summary from a full report dict."""
    return report.get("summary", {})
