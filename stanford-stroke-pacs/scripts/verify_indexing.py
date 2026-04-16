#!/usr/bin/env python3
"""Cross-reference the image_series SQL table against Orthanc's indexed data.

.. deprecated::
    This script is superseded by ``scripts/reconcile.py`` which performs the
    same check plus additional validations (archive path, dicom_dir_path on
    disk). Use ``python scripts/reconcile.py`` instead.

Reads the existing PostgreSQL table (read-only) and queries the Orthanc REST API
to report which series have been indexed and which are missing.

Usage:
    pip install -r requirements.txt
    python verify_indexing.py
"""

import os
import sys
import warnings
from pathlib import Path

import psycopg2
import requests
from dotenv import load_dotenv

warnings.warn(
    "verify_indexing.py is deprecated — use scripts/reconcile.py instead.",
    DeprecationWarning,
    stacklevel=1,
)

REPO_ROOT = Path(__file__).resolve().parent.parent
load_dotenv(REPO_ROOT / ".env")

DB_HOST = os.getenv("DB_HOST", "localhost")
DB_PORT = os.getenv("DB_PORT", "5432")
DB_NAME = os.getenv("DB_NAME", "stanford-stroke")
DB_USER = os.getenv("DB_USER")
DB_PASSWORD = os.getenv("DB_PASSWORD")

ORTHANC_URL = os.getenv("ORTHANC_URL", "http://localhost:8042")
ORTHANC_USER = os.getenv("ORTHANC_ADMIN_USER")
ORTHANC_PASSWORD = os.getenv("ORTHANC_ADMIN_PASSWORD")


def get_sql_series():
    """Fetch all series from the SQL table, keyed by SeriesInstanceUID."""
    conn = psycopg2.connect(
        host=DB_HOST, port=DB_PORT, dbname=DB_NAME,
        user=DB_USER, password=DB_PASSWORD,
    )
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT seriesinstanceuid, patient_id, "
                "       modality, seriesdescription, dicom_dir_path "
                "FROM image_series "
                "WHERE seriesinstanceuid IS NOT NULL"
            )
            rows = cur.fetchall()
    finally:
        conn.close()

    series = {}
    for uid, patient_id, modality, desc, path in rows:
        series[uid] = {
            "patient_id": patient_id,
            "modality": modality,
            "description": desc,
            "path": path,
        }
    return series


def get_orthanc_stats(session):
    """Get high-level statistics from Orthanc."""
    resp = session.get(f"{ORTHANC_URL}/statistics")
    resp.raise_for_status()
    return resp.json()


def get_orthanc_series_uids(session):
    """Fetch all SeriesInstanceUIDs currently indexed in Orthanc."""
    orthanc_ids = []
    offset = 0
    limit = 500
    while True:
        resp = session.get(
            f"{ORTHANC_URL}/series",
            params={"since": offset, "limit": limit},
        )
        resp.raise_for_status()
        batch = resp.json()
        if not batch:
            break
        orthanc_ids.extend(batch)
        if len(batch) < limit:
            break
        offset += limit

    uids = set()
    for oid in orthanc_ids:
        resp = session.get(f"{ORTHANC_URL}/series/{oid}")
        resp.raise_for_status()
        info = resp.json()
        uid = info.get("MainDicomTags", {}).get("SeriesInstanceUID")
        if uid:
            uids.add(uid)
    return uids


def main():
    if not DB_USER or not DB_PASSWORD:
        print("Error: DB_USER / DB_PASSWORD not set. Check your .env file.", file=sys.stderr)
        sys.exit(1)

    print(f"Loading .env from: {REPO_ROOT / '.env'}")
    print(f"Connecting to PostgreSQL: {DB_HOST}:{DB_PORT}/{DB_NAME}")
    sql_series = get_sql_series()
    print(f"  SQL table: {len(sql_series)} series with a SeriesInstanceUID\n")

    session = requests.Session()
    session.auth = (ORTHANC_USER, ORTHANC_PASSWORD)

    print(f"Querying Orthanc at {ORTHANC_URL} ...")
    try:
        stats = get_orthanc_stats(session)
    except requests.ConnectionError:
        print("Error: Cannot connect to Orthanc. Is the container running?", file=sys.stderr)
        sys.exit(1)

    print("  Orthanc statistics:")
    for key in ("CountPatients", "CountStudies", "CountSeries", "CountInstances"):
        print(f"    {key}: {stats.get(key, '?')}")
    print()

    print("Fetching indexed SeriesInstanceUIDs from Orthanc (this may take a while) ...")
    orthanc_uids = get_orthanc_series_uids(session)
    print(f"  Orthanc: {len(orthanc_uids)} series indexed\n")

    sql_uids = set(sql_series.keys())
    indexed = sql_uids & orthanc_uids
    missing = sql_uids - orthanc_uids
    extra = orthanc_uids - sql_uids

    coverage = len(indexed) / len(sql_uids) * 100 if sql_uids else 0

    print("=== Results ===")
    print(f"  Series in SQL table:       {len(sql_uids)}")
    print(f"  Series indexed in Orthanc: {len(orthanc_uids)}")
    print(f"  Matched (in both):         {len(indexed)}")
    print(f"  Missing from Orthanc:      {len(missing)}")
    print(f"  Extra in Orthanc:          {len(extra)}")
    print(f"  Coverage:                  {coverage:.1f}%")

    if missing:
        print("\n--- First 20 missing series ---")
        for uid in sorted(missing)[:20]:
            info = sql_series[uid]
            print(f"  Patient: {info['patient_id']}  |  {info['modality']}  |  {info['description']}")
            print(f"    UID:  {uid}")
            print(f"    Path: {info['path']}")

    if extra:
        print(f"\n  ({len(extra)} series in Orthanc not present in the SQL table)")


if __name__ == "__main__":
    main()
