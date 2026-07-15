#!/usr/bin/env python3
"""
REPORT-ONLY detector for on-disk DICOM series vs the `image_series` table.

Scans each patient's tree under `dicom_data_root` and, per series, compares it to
`image_series`:
  * MISSING — files on disk but no `image_series` row (candidate for backfill)
  * DRIFT   — row exists but `number_of_slices` != on-disk file count
  * ok      — row exists and counts match

This is **read-only**. It does NOT import `ImageIngestionProtocol` and does NOT
write anything.

WHY (incident 2026-06-24): an earlier version reused
`ImageIngestionProtocol.filter_existing_studies()` with `case_dir` pointed at the
live `dicom_data_root`. That method DELETES the canonical DICOM dir + cold archive
on a slice-count mismatch (to force a clean re-ingest) — and it runs during the
scan/filter phase, before any `--execute`. Pointed at the real store, it wiped 6
series. Lesson: never run the ingestion protocol against `dicom_data_root`; a
backfill of already-in-place files must be done with a dedicated, write-scoped
upsert (NOT this protocol), and only after the DRIFT rows below are understood.

Usage:
  python scripts/data_integrity/disk_vs_db_series_audit.py --patients <id1>,<id2>
  python scripts/data_integrity/disk_vs_db_series_audit.py --all           # every patient with on-disk files
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

import psycopg2
import pydicom
from dotenv import load_dotenv

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
load_dotenv(REPO_ROOT / ".env")
sys.path.insert(0, str(REPO_ROOT / "web-app"))

from db import DB_CONFIG  # noqa: E402

from config import DICOM_DATA_ROOT  # noqa: E402


def scan_patient_series(patient_root: str) -> dict[str, dict]:
    """Map SeriesInstanceUID -> {files, dir, dirname_uid} for on-disk DICOM dirs.

    Canonical layout: <patient>/<studyUID>/<desc>/<seriesUID>/DICOM/*.dcm
    Reads one file per series dir to get the true SeriesInstanceUID (and flags if
    it differs from the directory name).
    """
    out: dict[str, dict] = {}
    for study in sorted(os.scandir(patient_root), key=lambda e: e.name):
        if not study.is_dir():
            continue
        for desc in os.scandir(study.path):
            if not desc.is_dir():
                continue
            for series in os.scandir(desc.path):
                if not series.is_dir():
                    continue
                dicom_dir = os.path.join(series.path, "DICOM")
                if not os.path.isdir(dicom_dir):
                    continue
                files = [f for f in os.listdir(dicom_dir)
                         if not f.startswith(".") and os.path.isfile(os.path.join(dicom_dir, f))]
                if not files:
                    continue
                try:
                    suid = str(pydicom.dcmread(
                        os.path.join(dicom_dir, files[0]), stop_before_pixels=True
                    ).SeriesInstanceUID)
                except Exception:  # noqa: BLE001
                    suid = series.name  # fall back to the directory name
                out[suid] = {"files": len(files), "dir": dicom_dir,
                             "dirname_uid": series.name, "desc": desc.name}
    return out


def db_series(conn, patient: str) -> dict[str, int | None]:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT seriesinstanceuid, number_of_slices FROM image_series "
            "WHERE patient_id = %s", (patient,))
        return {r[0]: r[1] for r in cur.fetchall()}


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    g = ap.add_mutually_exclusive_group()
    g.add_argument("--patients",
                   help="Comma-separated patient_ids to scan (required unless --all)")
    g.add_argument("--all", action="store_true",
                   help="Scan every patient dir present under dicom_data_root")
    args = ap.parse_args()

    root = str(DICOM_DATA_ROOT)
    if args.all:
        patients = sorted(e.name for e in os.scandir(root) if e.is_dir())
    elif args.patients:
        patients = [p.strip() for p in args.patients.split(",") if p.strip()]
    else:
        ap.error("provide --patients <id,...> or --all")

    conn = psycopg2.connect(**DB_CONFIG)
    print(f"READ-ONLY scan  dicom_data_root={root}  patients={len(patients)}")
    n_missing = n_drift = 0
    try:
        for patient in patients:
            proot = os.path.join(root, patient)
            if not os.path.isdir(proot):
                continue
            on_disk = scan_patient_series(proot)
            if not on_disk:
                continue
            dbrows = db_series(conn, patient)
            lines = []
            for suid, info in sorted(on_disk.items(), key=lambda kv: kv[1]["desc"]):
                if suid not in dbrows:
                    n_missing += 1
                    lines.append(f"  MISSING  {info['desc']:24s} files={info['files']:4d}  "
                                 f"series=...{suid[-12:]}")
                else:
                    db_n = dbrows[suid]
                    if db_n is not None and int(db_n) != info["files"]:
                        n_drift += 1
                        lines.append(f"  DRIFT    {info['desc']:24s} disk={info['files']:4d} "
                                     f"db={db_n}  series=...{suid[-12:]}")
            if lines:
                print(f"\n=== {patient} ===")
                print("\n".join(lines))
        print(f"\n{'=' * 60}")
        print(f"MISSING (on disk, not in image_series): {n_missing}")
        print(f"DRIFT   (in image_series, count mismatch): {n_drift}")
        print("Read-only report. No files or DB rows were touched.")
        if n_drift:
            print("NOTE: investigate DRIFT rows before any backfill — a count mismatch is\n"
                  "      exactly what the (now-removed) protocol path wiped.")
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
