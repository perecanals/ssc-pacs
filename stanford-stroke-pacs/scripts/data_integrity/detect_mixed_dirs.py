#!/usr/bin/env python3
"""
REPORT-ONLY detector for "mixed" DICOM directories: a single physical
``.../<seriesUID>/DICOM/`` dir that holds files from more than one
``SeriesInstanceUID``.

Unlike ``data_integrity/disk_vs_db_series_audit.py`` (which reads only the *first* file in each
dir, so it keys by dir name and is blind to a mixed dir's secondary series), this
scans **every** file's header and groups by ``SeriesInstanceUID``. For each
distinct UID found in a dir it reports:

  * file count on disk (the true per-series count),
  * SeriesDescription / Modality / SeriesNumber (from headers),
  * whether an ``image_series`` row exists, and that row's ``number_of_slices``,
  * Orthanc's served + readable instance counts for that UID.

The "host" UID is the one whose directory name it lives under (the named series);
any other UID in the same dir is an untracked **secondary** — the remediation
target. A dir with exactly one UID is reported as ``clean``.

This is strictly **read-only**: it opens the DB and Orthanc for SELECT/lookup
only and never writes disk, Postgres, or the Orthanc index.

Usage:
  python scripts/data_integrity/detect_mixed_dirs.py --patients <id1>,<id2>
  python scripts/data_integrity/detect_mixed_dirs.py --all           # every patient on disk (slow)
  python scripts/data_integrity/detect_mixed_dirs.py --show-clean    # also list clean dirs
"""

from __future__ import annotations

import argparse
import os
import sys
from collections import defaultdict
from pathlib import Path

import psycopg2
import pydicom
import requests
from dotenv import load_dotenv

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
load_dotenv(REPO_ROOT / ".env")
sys.path.insert(0, str(REPO_ROOT / "web-app"))

from db import DB_CONFIG  # noqa: E402
from orthanc_client import ORTHANC_PASS, ORTHANC_URL, ORTHANC_USER  # noqa: E402

from config import DICOM_DATA_ROOT  # noqa: E402


def _dir_files(dicom_dir: str) -> list[str]:
    return [f for f in os.listdir(dicom_dir)
            if not f.startswith(".") and os.path.isfile(os.path.join(dicom_dir, f))]


def group_dir_by_uid(dicom_dir: str) -> dict[str, dict]:
    """Read every file's header; return {SeriesInstanceUID: {count, desc, modality, seriesnumber}}."""
    groups: dict[str, dict] = defaultdict(
        lambda: {"count": 0, "desc": "", "modality": "", "seriesnumber": None})
    for fname in _dir_files(dicom_dir):
        fpath = os.path.join(dicom_dir, fname)
        try:
            ds = pydicom.dcmread(fpath, stop_before_pixels=True,
                                 specific_tags=["SeriesInstanceUID", "SeriesDescription",
                                                "Modality", "SeriesNumber"])
            suid = str(ds.SeriesInstanceUID)
        except Exception:  # noqa: BLE001
            suid = "<unreadable>"
            ds = None
        g = groups[suid]
        g["count"] += 1
        if ds is not None and not g["desc"]:
            g["desc"] = str(getattr(ds, "SeriesDescription", "") or "")
            g["modality"] = str(getattr(ds, "Modality", "") or "")
            g["seriesnumber"] = getattr(ds, "SeriesNumber", None)
    return dict(groups)


def iter_series_dirs(patient_root: str):
    """Yield (dirname_uid, desc_folder, dicom_dir) for each .../<seriesUID>/DICOM/ dir."""
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
                if os.path.isdir(dicom_dir) and _dir_files(dicom_dir):
                    yield series.name, desc.name, dicom_dir


def db_series(conn, patient: str) -> dict[str, dict]:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT seriesinstanceuid, number_of_slices, dicom_dir_path "
            "FROM image_series WHERE patient_id = %s", (patient,))
        return {r[0]: {"number_of_slices": r[1], "dicom_dir_path": r[2]}
                for r in cur.fetchall()}


def orthanc_counts(session, suid: str) -> tuple[bool, int, int]:
    """(served, total_instances, readable_instances) for a SeriesInstanceUID."""
    try:
        r = session.post(f"{ORTHANC_URL}/tools/find",
                         json={"Level": "Series", "Query": {"SeriesInstanceUID": suid},
                               "Expand": True}, timeout=60)
        r.raise_for_status()
        found = r.json()
    except Exception:  # noqa: BLE001
        return (False, 0, 0)
    if not found:
        return (False, 0, 0)
    insts = found[0].get("Instances", [])
    readable = sum(1 for i in insts
                   if session.get(f"{ORTHANC_URL}/instances/{i}/file",
                                  timeout=20).status_code == 200)
    return (True, len(insts), readable)


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    g = ap.add_mutually_exclusive_group()
    g.add_argument("--patients",
                   help="Comma-separated patient_ids to scan (required unless --all)")
    g.add_argument("--all", action="store_true",
                   help="Scan every patient dir present under dicom_data_root (slow)")
    ap.add_argument("--show-clean", action="store_true",
                    help="Also print single-UID (clean) dirs")
    ap.add_argument("--no-orthanc", action="store_true",
                    help="Skip Orthanc cross-check (disk + DB only)")
    args = ap.parse_args()

    root = str(DICOM_DATA_ROOT)
    if args.all:
        patients = sorted(e.name for e in os.scandir(root) if e.is_dir())
    elif args.patients:
        patients = [p.strip() for p in args.patients.split(",") if p.strip()]
    else:
        ap.error("provide --patients <id,...> or --all")

    session = None if args.no_orthanc else requests.Session()
    if session is not None:
        session.auth = (ORTHANC_USER, ORTHANC_PASS)

    conn = psycopg2.connect(**DB_CONFIG)
    print(f"READ-ONLY mixed-dir scan  dicom_data_root={root}  patients={len(patients)}"
          f"  orthanc={'off' if args.no_orthanc else ORTHANC_URL}")

    n_mixed = n_secondary = n_clean = 0
    try:
        for patient in patients:
            proot = os.path.join(root, patient)
            if not os.path.isdir(proot):
                continue
            dbrows = db_series(conn, patient)
            header_printed = False
            for dirname_uid, desc_folder, dicom_dir in iter_series_dirs(proot):
                groups = group_dir_by_uid(dicom_dir)
                if len(groups) <= 1:
                    n_clean += 1
                    if args.show_clean:
                        if not header_printed:
                            print(f"\n=== {patient} ===")
                            header_printed = True
                        only = next(iter(groups), "<empty>")
                        print(f"  clean  {desc_folder:28s} files={groups.get(only, {}).get('count', 0):4d}")
                    continue

                n_mixed += 1
                if not header_printed:
                    print(f"\n=== {patient} ===")
                    header_printed = True
                rel = os.path.relpath(dicom_dir, root)
                print(f"\n  MIXED dir ({len(groups)} UIDs)  {rel}")
                print(f"        dir-name UID = ...{dirname_uid[-16:]}  (folder: {desc_folder})")
                # host UID first (matches the dir name), then secondaries
                ordered = sorted(groups.items(),
                                 key=lambda kv: (kv[0] != dirname_uid, kv[1]["desc"]))
                for suid, info in ordered:
                    role = "HOST     " if suid == dirname_uid else "SECONDARY"
                    if role.startswith("SECONDARY"):
                        n_secondary += 1
                    in_db = suid in dbrows
                    db_n = dbrows.get(suid, {}).get("number_of_slices")
                    db_note = f"db_row=yes n_slices={db_n}" if in_db else "db_row=NO"
                    if session is not None:
                        served, total, readable = orthanc_counts(session, suid)
                        orth = f"orthanc served={served} inst={readable}/{total}"
                    else:
                        orth = "orthanc=skipped"
                    print(f"        {role} files={info['count']:4d}  "
                          f"desc='{info['desc']}' mod={info['modality']} "
                          f"sn={info['seriesnumber']}")
                    print(f"                  ...{suid[-16:]}  {db_note}  {orth}")

        print(f"\n{'=' * 64}")
        print(f"mixed dirs: {n_mixed}   secondary (untracked) series: {n_secondary}   "
              f"clean dirs: {n_clean}")
        print("Read-only report. No files, Postgres rows, or Orthanc index entries were touched.")
        if n_mixed:
            print("\nNext: de-mix on disk (copy->verify->remove), recompute counts, archive,\n"
                  "      upsert image_series, then reindex_series for the HOST UID(s).")
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
