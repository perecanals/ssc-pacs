#!/usr/bin/env python3
"""
List series in `image_series` that have a `dicom_dir_path` but no
`dicom_archive_path`. These are series whose loose DICOMs are still on disk
but were never compressed (or compression failed during ingestion).

Use to triage compression failures. Retry with:
    python scripts/archive_all_series.py --patient <patient_id>

Usage:
    python scripts/list_unarchived_series.py
    python scripts/list_unarchived_series.py --patient 4-0551
    python scripts/list_unarchived_series.py --import-label "2026-04-batch"
    python scripts/list_unarchived_series.py --count        # just print the total
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path
from typing import Any

import psycopg2
import psycopg2.extras
from dotenv import load_dotenv

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
load_dotenv(REPO_ROOT / ".env")

sys.path.insert(0, str(REPO_ROOT / "web-app"))
from db import DB_CONFIG, get_conn  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--patient", help="Limit to a single patient_id")
    ap.add_argument("--import-label", dest="import_label", help="Limit to a single import_label")
    ap.add_argument("--count", action="store_true", help="Print only the total count")
    args = ap.parse_args()

    if not DB_CONFIG.get("user"):
        print("DB_USER not set in .env", file=sys.stderr)
        return 1

    where = [
        "dicom_dir_path IS NOT NULL",
        "dicom_dir_path <> ''",
        "(dicom_archive_path IS NULL OR dicom_archive_path = '')",
    ]
    params: list[Any] = []
    if args.patient:
        where.append("patient_id = %s")
        params.append(args.patient)
    if args.import_label:
        where.append("import_label = %s")
        params.append(args.import_label)

    if args.count:
        q = f"SELECT COUNT(*) FROM image_series WHERE {' AND '.join(where)}"
    else:
        q = (
            "SELECT patient_id, studyinstanceuid, seriesinstanceuid, dicom_dir_path "
            f"FROM image_series WHERE {' AND '.join(where)} "
            "ORDER BY patient_id, studyinstanceuid, seriesinstanceuid"
        )

    with psycopg2.connect(**DB_CONFIG) as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(q, params)
            rows = cur.fetchall()

    if args.count:
        print(rows[0]["count"])
        return 0

    if not rows:
        print("No unarchived series.")
        return 0

    for r in rows:
        print(f"{r['patient_id']}\t{r['studyinstanceuid']}\t{r['seriesinstanceuid']}\t{r['dicom_dir_path']}")
    print(f"\n{len(rows)} series with no archive.", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
