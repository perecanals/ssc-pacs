"""Two-DB reconciliation: compare image_series (stanford-stroke) vs Orthanc index.

Also runs intra-DB integrity checks (annotations vs their spine tables).
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

from orthanc_client import ORTHANC_PASS, ORTHANC_URL, ORTHANC_USER

logger = logging.getLogger(__name__)

# Read-only connection to Orthanc's PostgreSQL index (same credentials the
# Orthanc container uses, from .env). Used only to bulk-read series UIDs.
_PG_ORTHANC = {
    "host": os.getenv("DB_HOST", "localhost"),
    "port": os.getenv("DB_PORT", "5432"),
    "dbname": os.getenv("PG_ORTHANC_DB", "orthanc_db"),
    "user": os.getenv("PG_ORTHANC_USER", ""),
    "password": os.getenv("PG_ORTHANC_PASSWORD", ""),
}

# Page size for paginating Orthanc's /series listing (REST fallback).
_ORTHANC_PAGE_SIZE = 500


# ---------------------------------------------------------------------------
# Orthanc helpers
# ---------------------------------------------------------------------------

def _orthanc_session() -> requests.Session:
    """Build an authenticated requests.Session for Orthanc."""
    s = requests.Session()
    s.auth = (ORTHANC_USER, ORTHANC_PASS)
    return s


def _get_orthanc_series_uids_sql() -> set[str]:
    """Fetch all SeriesInstanceUIDs straight from Orthanc's PostgreSQL index.

    Reads tag (0020,000E) from ``maindicomtags`` for series-level resources
    (``resourcetype = 2``) — the same data the REST path resolves, but in one
    query (~seconds) instead of one ``GET /series/{id}`` per series (~tens of
    minutes on a 100k-series index).
    """
    conn = psycopg2.connect(connect_timeout=10, **_PG_ORTHANC)
    try:
        conn.set_session(readonly=True)
        with conn.cursor() as cur:
            cur.execute(
                "SELECT t.value FROM resources r "
                "JOIN maindicomtags t ON t.id = r.internalid "
                "  AND t.taggroup = 32 AND t.tagelement = 14 "
                "WHERE r.resourcetype = 2"
            )
            return {row[0] for row in cur.fetchall()}
    finally:
        conn.close()


def _get_orthanc_series_uids(session: requests.Session) -> set[str]:
    """Fetch all SeriesInstanceUIDs currently indexed in Orthanc (REST fallback).

    Paginates through ``GET /series`` and resolves each Orthanc internal ID
    to a DICOM SeriesInstanceUID via ``GET /series/{id}``.  Slow on large
    indexes — used only when the direct ``orthanc_db`` read fails.
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

def _get_orphaned_annotations(conn) -> list[dict[str, Any]]:
    """Annotations whose entity no longer exists in its spine table.

    Checks each level against its own key: patient → ``patient``,
    study → ``image_study``, series → ``image_series``. Orphans appear when
    entity rows are deleted/renamed out from under existing annotations
    (annotations has no FK to the upstream tables).
    """
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(
            "SELECT 'patient' AS level, a.patient_id AS entity_id, "
            "       a.label, a.created_by "
            "FROM annotations a "
            "WHERE a.level = 'patient' AND a.patient_id IS NOT NULL "
            "  AND NOT EXISTS (SELECT 1 FROM patient p "
            "                  WHERE p.patient_id = a.patient_id) "
            "UNION ALL "
            "SELECT 'study', a.studyinstanceuid, a.label, a.created_by "
            "FROM annotations a "
            "WHERE a.level = 'study' AND a.studyinstanceuid IS NOT NULL "
            "  AND NOT EXISTS (SELECT 1 FROM image_study st "
            "                  WHERE st.studyinstanceuid = a.studyinstanceuid) "
            "UNION ALL "
            "SELECT 'series', a.seriesinstanceuid, a.label, a.created_by "
            "FROM annotations a "
            "WHERE a.level = 'series' AND a.seriesinstanceuid IS NOT NULL "
            "  AND NOT EXISTS (SELECT 1 FROM image_series se "
            "                  WHERE se.seriesinstanceuid = a.seriesinstanceuid) "
            "ORDER BY 1, 2, 3"
        )
        return [dict(r) for r in cur.fetchall()]


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

    # 1. DB rows + intra-DB orphan check (annotations vs their spine tables)
    db_rows = _get_db_series(conn)
    db_by_uid = {r["seriesinstanceuid"]: r for r in db_rows}
    db_uids = set(db_by_uid.keys())
    orphaned_annotations = _get_orphaned_annotations(conn)

    # These SELECTs are the only DB work in this function — the Orthanc calls below
    # are HTTP and the classify/disk-stat loops touch no DB. End the transaction
    # now so we do NOT keep an ACCESS SHARE lock on image_series open across the
    # (potentially many-minute) per-row is_file() disk scan. Holding it
    # would block writers: a concurrent ingestion's
    # `ALTER TABLE image_series ADD COLUMN IF NOT EXISTS` (ACCESS EXCLUSIVE) would
    # queue behind us for the whole scan, stalling the run by tens of minutes.
    conn.rollback()

    # 2. Orthanc UIDs — prefer the direct index read; fall back to REST
    try:
        orthanc_uids = _get_orthanc_series_uids_sql()
    except Exception as exc:
        logger.warning(
            "orthanc_db read failed (%s); falling back to slow REST enumeration",
            exc,
        )
        orthanc_uids = _get_orthanc_series_uids(session)

    # 3. Orthanc statistics
    orthanc_stats = _get_orthanc_statistics(session)

    # 4. Classify mismatches
    in_db_not_in_orthanc: list[dict[str, Any]] = []
    in_orthanc_not_in_db: list[dict[str, Any]] = []
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

    # NOTE: we deliberately do NOT check dicom_dir_path existence on disk.
    # In cold_path_cache mode evicted series legitimately have no loose files
    # (the archive is canonical), so the check flags tens of thousands of
    # false positives while costing one stat() per series on the data disk.

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
        "dicom_archive_missing": dicom_archive_missing,
        "orphaned_annotations": orphaned_annotations,
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
        "dicom_archive_missing": len(mismatches.get("dicom_archive_missing", [])),
        "orphaned_annotations": len(mismatches.get("orphaned_annotations", [])),
        "db_series_count": db_count,
        "orthanc_series_count": orthanc_count,
        "matched": matched,
        "coverage_percent": coverage,
    }


def snapshot_summary(report: dict[str, Any]) -> dict[str, Any]:
    """Extract the counts-only summary from a full report dict."""
    return report.get("summary", {})
