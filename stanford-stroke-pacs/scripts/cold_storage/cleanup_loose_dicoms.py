#!/usr/bin/env python3
"""
Delete loose DICOM directories that are safe to remove because:
  1. The series has a populated `dicom_archive_path` in `image_series`
  2. The archive file exists on disk and is non-empty
  3. The archive's file count matches the loose dir's file count
  4. The series is present in Orthanc's index (queried via orthanc_db PostgreSQL)

Without `--execute` the script is dry-run only and prints what it would do.

Designed to be re-runnable safely (idempotent) and suitable for cron.

Usage:
  # See what would be cleaned (default)
  python scripts/cleanup_loose_dicoms.py

  # Actually delete
  python scripts/cleanup_loose_dicoms.py --execute

  # Limit to one patient
  python scripts/cleanup_loose_dicoms.py --execute --patient 4-0551

  # Limit to specific ingestion runs (image_series.import_label)
  python scripts/cleanup_loose_dicoms.py --execute --import-label sir_batch1 --import-label sir_batch2

  # Skip the (slow) per-archive integrity check
  python scripts/cleanup_loose_dicoms.py --execute --no-deep-verify

The NIFTI sibling directory ({seriesUID}/NIFTI/) is preserved.
"""

from __future__ import annotations

import argparse
import os
import shutil
import sys
import tarfile
import time
from pathlib import Path
from typing import Any

import psycopg2
import psycopg2.extras
import zstandard as zstd
from dotenv import load_dotenv

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
load_dotenv(REPO_ROOT / ".env")

# Read paths from web-app/config.py so cleanup matches the running stack.
sys.path.insert(0, str(REPO_ROOT / "web-app"))
from config import DICOM_DATA_ROOT, STORAGE_MODE  # noqa: E402
from db import DB_CONFIG  # noqa: E402

ORTHANC_DB_CONFIG = dict(
    host=os.getenv("DB_HOST", "localhost"),
    port=os.getenv("DB_PORT", "5432"),
    dbname=os.getenv("PG_ORTHANC_DB", "orthanc"),
    user=os.getenv("PG_ORTHANC_USER"),
    password=os.getenv("PG_ORTHANC_PASSWORD"),
)

# DICOM tag for SeriesInstanceUID = (0x0020, 0x000e) = (32, 14)
SERIES_UID_TAG_GROUP = 32
SERIES_UID_TAG_ELEMENT = 14


def fetch_candidate_series(patient: str | None, study: str | None,
                           import_labels: list[str] | None = None) -> list[dict]:
    """Series with both an archive path and an existing loose dir."""
    q = (
        "SELECT seriesinstanceuid, studyinstanceuid, patient_id, "
        "       dicom_dir_path, dicom_archive_path "
        "FROM image_series "
        "WHERE dicom_archive_path IS NOT NULL "
        "  AND dicom_archive_path <> '' "
        "  AND dicom_dir_path IS NOT NULL "
        "  AND dicom_dir_path <> ''"
    )
    params: list[Any] = []
    if patient:
        q += " AND patient_id = %s"
        params.append(patient)
    if study:
        q += " AND studyinstanceuid = %s"
        params.append(study)
    if import_labels:
        q += " AND import_label = ANY(%s)"
        params.append(import_labels)

    with psycopg2.connect(**DB_CONFIG) as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(q, params)
            return list(cur.fetchall())


def fetch_orthanc_series_uids() -> set[str]:
    """One query to grab every SeriesInstanceUID Orthanc currently knows."""
    with psycopg2.connect(**ORTHANC_DB_CONFIG) as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT value FROM dicomidentifiers "
                "WHERE taggroup = %s AND tagelement = %s",
                (SERIES_UID_TAG_GROUP, SERIES_UID_TAG_ELEMENT),
            )
            return {row[0] for row in cur.fetchall()}


def count_loose_files(dicom_dir: Path) -> int:
    if not dicom_dir.is_dir():
        return 0
    return sum(1 for p in dicom_dir.rglob("*") if p.is_file())


def count_archive_files(archive_path: Path) -> int:
    """Open the tar.zst and count regular file members. Slow — guarded by --no-deep-verify."""
    dctx = zstd.ZstdDecompressor()
    n = 0
    with archive_path.open("rb") as f_in:
        with dctx.stream_reader(f_in) as z_in:
            with tarfile.open(fileobj=z_in, mode="r|") as tf:
                for m in tf:
                    if m.isfile():
                        n += 1
    return n


def clean_series_loose_dir(
    series_uid: str,
    dicom_dir: Path,
    archive: Path,
    *,
    series_in_orthanc: bool,
    deep_verify: bool = True,
    execute: bool = True,
) -> tuple[str, int, str | None]:
    """Verify one series' loose DICOM dir is redundant and (optionally) delete it.

    Safety checks (all must pass before deletion):
      1. archive exists and is non-empty
      2. the series is present in Orthanc's index (caller-established fact)
      3. deep_verify: archive regular-file count == loose dir file count

    The NIFTI sibling ({seriesUID}/NIFTI/) is untouched — only `dicom_dir` itself
    (the .../DICOM dir recorded in image_series.dicom_dir_path) is removed.

    Returns (status, bytes_freed, detail) with status one of:
    'cleaned', 'already_clean', 'no_archive', 'not_in_orthanc', 'count_mismatch'.
    Shared by the CLI below and the ingestion pipeline's
    cleanup_loose_after_indexing knob.
    """
    if not dicom_dir.is_dir():
        return "already_clean", 0, None
    loose_count = count_loose_files(dicom_dir)
    if loose_count == 0:
        # Empty dir — remove the empty shell so future runs don't re-check.
        if execute:
            try:
                dicom_dir.rmdir()
            except OSError:
                pass
        return "already_clean", 0, None

    if not archive.is_file() or archive.stat().st_size == 0:
        return "no_archive", 0, f"{series_uid}: archive missing or empty at {archive}"

    # Series must be in Orthanc's index. If it isn't, the patched indexer
    # hasn't picked it up yet (or never will) — refusing to delete.
    if not series_in_orthanc:
        return "not_in_orthanc", 0, None

    if deep_verify:
        try:
            archive_count = count_archive_files(archive)
        except Exception as exc:
            return "no_archive", 0, f"{series_uid}: archive verification raised {exc}"
        if archive_count != loose_count:
            return "count_mismatch", 0, (
                f"{series_uid}: archive has {archive_count} files; "
                f"loose dir has {loose_count} files at {dicom_dir}"
            )

    size = sum(p.stat().st_size for p in dicom_dir.rglob("*") if p.is_file())
    if execute:
        shutil.rmtree(dicom_dir, ignore_errors=False)
    return "cleaned", size, None


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--execute", action="store_true", help="Actually delete (default: dry-run)")
    ap.add_argument("--patient", help="Limit to a single patient_id")
    ap.add_argument("--study", help="Limit to a single studyinstanceuid")
    ap.add_argument("--import-label", action="append", dest="import_labels",
                    metavar="LABEL",
                    help="Limit to series with this image_series.import_label "
                         "(repeatable, e.g. --import-label sir_batch1 --import-label sir_batch2)")
    ap.add_argument("--limit", type=int, help="Stop after this many series")
    ap.add_argument("--no-deep-verify", action="store_true",
                    help="Skip per-archive file count comparison (faster)")
    ap.add_argument("--quiet", action="store_true", help="Only print summary")
    args = ap.parse_args()

    if STORAGE_MODE != "cold_path_cache":
        print(f"WARNING: STORAGE_MODE is '{STORAGE_MODE}' (not 'cold_path_cache'). "
              f"Loose DICOMs are still the canonical store; cleanup is unsafe. Aborting.",
              file=sys.stderr)
        return 2

    if not DB_CONFIG.get("user") or not ORTHANC_DB_CONFIG.get("user"):
        print("DB credentials missing in .env (DB_USER / PG_ORTHANC_USER)", file=sys.stderr)
        return 1

    print(f"Mode: {'EXECUTE' if args.execute else 'DRY RUN'}")
    print(f"DICOM data root: {DICOM_DATA_ROOT}")

    print("Fetching candidate series from image_series ...")
    candidates = fetch_candidate_series(args.patient, args.study, args.import_labels)
    print(f"  {len(candidates)} candidate series")

    print("Fetching indexed SeriesInstanceUIDs from Orthanc DB ...")
    orthanc_uids = fetch_orthanc_series_uids()
    print(f"  {len(orthanc_uids)} series in Orthanc index")

    cleaned = 0
    skipped_already_clean = 0
    skipped_not_in_orthanc = 0
    skipped_no_archive = 0
    skipped_count_mismatch = 0
    bytes_freed = 0
    errors: list[str] = []

    t0 = time.perf_counter()
    processed = 0
    cleaned_uids: list[str] = []

    for row in candidates:
        if args.limit and processed >= args.limit:
            break
        processed += 1

        series_uid = row["seriesinstanceuid"]
        dicom_dir = Path(row["dicom_dir_path"])
        archive = Path(row["dicom_archive_path"])

        status, size, detail = clean_series_loose_dir(
            series_uid, dicom_dir, archive,
            series_in_orthanc=series_uid in orthanc_uids,
            deep_verify=not args.no_deep_verify,
            execute=args.execute,
        )
        if detail:
            errors.append(detail)
        if status == "already_clean":
            skipped_already_clean += 1
        elif status == "no_archive":
            skipped_no_archive += 1
        elif status == "not_in_orthanc":
            skipped_not_in_orthanc += 1
        elif status == "count_mismatch":
            skipped_count_mismatch += 1
        elif status == "cleaned":
            if not args.quiet:
                verb = "Would delete" if not args.execute else "Deleting"
                print(f"  {verb} ({size/1e6:.1f} MB) {dicom_dir}")
            cleaned += 1
            bytes_freed += size
            cleaned_uids.append(series_uid)

    # Loose files are gone — drop the cleaned series' cache rows so the web
    # app reads them as cold (absence = cold, matching evict_series semantics).
    # 'warming' rows are left alone: an in-flight warm lands its own row.
    if args.execute and cleaned_uids:
        try:
            with psycopg2.connect(**DB_CONFIG) as conn, conn.cursor() as cur:
                cur.execute(
                    "DELETE FROM series_cache_state "
                    "WHERE seriesinstanceuid = ANY(%s) AND status <> 'warming'",
                    (cleaned_uids,),
                )
                print(f"Cleared {cur.rowcount} series_cache_state row(s) "
                      f"for {len(cleaned_uids)} cleaned series")
        except Exception as exc:
            print(f"WARNING: failed to clear series_cache_state rows ({exc}); "
                  f"run scripts/cold_storage/rebuild_cache_state.py to reconcile",
                  file=sys.stderr)

    elapsed = time.perf_counter() - t0
    print()
    print("=" * 60)
    print(f"Processed: {processed}")
    print(f"Cleaned (loose dirs deletable): {cleaned}")
    print(f"Already clean: {skipped_already_clean}")
    print(f"Pending Orthanc index: {skipped_not_in_orthanc}")
    print(f"Archive missing/unreadable: {skipped_no_archive}")
    print(f"Archive/loose count mismatch: {skipped_count_mismatch}")
    print(f"Bytes that would be freed: {bytes_freed/1e9:.2f} GB")
    print(f"Elapsed: {elapsed:.1f}s")
    if errors:
        print(f"\nErrors ({len(errors)}):")
        for e in errors[:50]:
            print(f"  {e}")
        if len(errors) > 50:
            print(f"  ... and {len(errors) - 50} more")
    if not args.execute:
        print("\nDRY RUN — no files were deleted. Re-run with --execute to apply.")
    return 0 if not errors else 3


if __name__ == "__main__":
    raise SystemExit(main())
