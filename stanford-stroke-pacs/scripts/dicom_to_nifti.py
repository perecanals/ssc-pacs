#!/usr/bin/env python3
"""
On-demand DICOM → NIFTI converter.

Three input modes:

  --dir <path>           Convert a loose DICOM directory directly
  --archive <path>       Extract a tar.zst archive to a temp dir, then convert
  --series-uid <uid>     Look up dicom_dir_path in image_series. If the loose
                         dir is present (warm), convert from it. If absent
                         (cold), pass --warm-if-cold to warm via cache_manager
                         first; otherwise the script aborts.

Output:
  --out <path>           Output .nii.gz path. Defaults to the canonical
                         {dicom_dir_parent}/NIFTI/image.nii.gz when --dir or
                         --series-uid is used. Required for --archive.

Reuses:
  - image_integration_protocols/utils.convert_dicom_to_nifti
  - companion/cache_manager.warm_study, _is_series_dir_warm, untar_zst
"""

from __future__ import annotations

import argparse
import os
import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
# Make companion + integration utilities importable.
sys.path.insert(0, str(REPO_ROOT / "companion"))
sys.path.insert(0, str(REPO_ROOT / "image_integration_protocols"))

import psycopg2
import psycopg2.extras
from dotenv import load_dotenv

load_dotenv(REPO_ROOT / ".env")

from utils import convert_dicom_to_nifti  # noqa: E402
from cache_manager import _is_series_dir_warm, untar_zst, warm_study  # noqa: E402

DB_CONFIG = dict(
    host=os.getenv("DB_HOST", "localhost"),
    port=os.getenv("DB_PORT", "5432"),
    dbname=os.getenv("DB_NAME", "stanford-stroke"),
    user=os.getenv("DB_USER"),
    password=os.getenv("DB_PASSWORD"),
)


def lookup_series(series_uid: str) -> dict:
    with psycopg2.connect(**DB_CONFIG) as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(
                "SELECT seriesinstanceuid, studyinstanceuid, dicom_dir_path, dicom_archive_path "
                "FROM image_series WHERE seriesinstanceuid = %s LIMIT 1",
                (series_uid,),
            )
            row = cur.fetchone()
            if not row:
                raise SystemExit(f"Series not found in image_series: {series_uid}")
            return dict(row)


def default_nifti_out(dicom_dir: Path) -> Path:
    return dicom_dir.parent / "NIFTI" / "image.nii.gz"


def convert_from_dir(dicom_dir: Path, out: Path) -> Path:
    if not dicom_dir.is_dir():
        raise SystemExit(f"DICOM directory does not exist: {dicom_dir}")
    if not any(dicom_dir.iterdir()):
        raise SystemExit(f"DICOM directory is empty: {dicom_dir}")
    out.parent.mkdir(parents=True, exist_ok=True)
    print(f"Converting {dicom_dir} -> {out}")
    convert_dicom_to_nifti(str(dicom_dir), str(out))
    if not out.is_file():
        raise SystemExit(f"Conversion appeared to succeed but output is missing: {out}")
    print(f"Wrote {out} ({out.stat().st_size:,} bytes)")
    return out


def convert_from_archive(archive: Path, out: Path) -> Path:
    if not archive.is_file():
        raise SystemExit(f"Archive does not exist: {archive}")
    out.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="dicom_to_nifti_") as tmpdir:
        tmp_path = Path(tmpdir)
        print(f"Extracting {archive} to {tmp_path}")
        untar_zst(archive, tmp_path)
        print(f"Converting {tmp_path} -> {out}")
        convert_dicom_to_nifti(str(tmp_path), str(out))
    if not out.is_file():
        raise SystemExit(f"Conversion appeared to succeed but output is missing: {out}")
    print(f"Wrote {out} ({out.stat().st_size:,} bytes)")
    return out


def convert_from_series_uid(series_uid: str, out: Path | None, warm_if_cold: bool) -> Path:
    row = lookup_series(series_uid)
    dicom_dir = row.get("dicom_dir_path")
    if not dicom_dir:
        raise SystemExit(f"Series {series_uid} has no dicom_dir_path in image_series")
    dicom_dir = Path(dicom_dir)

    if not _is_series_dir_warm(str(dicom_dir)):
        if not warm_if_cold:
            raise SystemExit(
                f"Series {series_uid} is cold (loose dir empty/missing at {dicom_dir}). "
                f"Re-run with --warm-if-cold to warm the study first."
            )
        study_uid = row.get("studyinstanceuid")
        if not study_uid:
            raise SystemExit(f"Series {series_uid} has no studyinstanceuid; can't warm")
        print(f"Warming study {study_uid} ...")
        result = warm_study(study_uid)
        if not result.get("ok"):
            raise SystemExit(f"warm_study returned {result}")
        if not _is_series_dir_warm(str(dicom_dir)):
            raise SystemExit(
                f"warm_study completed but loose dir is still empty: {dicom_dir}"
            )

    if out is None:
        out = default_nifti_out(dicom_dir)
    return convert_from_dir(dicom_dir, out)


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    grp = ap.add_mutually_exclusive_group(required=True)
    grp.add_argument("--dir", type=Path, help="Loose DICOM directory")
    grp.add_argument("--archive", type=Path, help="Path to a tar.zst archive")
    grp.add_argument("--series-uid", type=str, help="SeriesInstanceUID (looks up dicom_dir_path)")
    ap.add_argument("--out", type=Path, help="Output .nii.gz path")
    ap.add_argument(
        "--warm-if-cold",
        action="store_true",
        help="With --series-uid: warm the study via cache_manager if the loose dir is empty",
    )
    args = ap.parse_args()

    if args.archive and not args.out:
        raise SystemExit("--archive requires --out (no canonical sibling location)")

    if args.dir:
        out = args.out or default_nifti_out(args.dir.resolve())
        convert_from_dir(args.dir.resolve(), out.resolve())
        return 0
    if args.archive:
        convert_from_archive(args.archive.resolve(), args.out.resolve())
        return 0
    if args.series_uid:
        out = args.out.resolve() if args.out else None
        convert_from_series_uid(args.series_uid, out, args.warm_if_cold)
        return 0
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
