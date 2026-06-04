#!/usr/bin/env python3
"""
Temporarily hide one case from the legacy DICOM tree by renaming series directories
away, then restore them later from a manifest.

This is intended for manual OHIF testing:
  1. Hide a patient/study/series from /DATA2/pacs_imaging_data
  2. Use OHIF yourself while the paths are hidden
  3. Restore the exact same directories from the manifest

No files are deleted. Directories are renamed atomically on the same filesystem.

Examples:
  python3 scripts/orthanc_holdout_case.py hide --patient-id 4-0117
  python3 scripts/orthanc_holdout_case.py hide --studyinstanceuid 1.2.3...
  python3 scripts/orthanc_holdout_case.py hide --seriesinstanceuid 1.2.3...
  python3 scripts/orthanc_holdout_case.py restore --manifest tmp/orthanc_holdout/orthanc_holdout_20260410T000000Z.json
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import psycopg2
import psycopg2.extras
from dotenv import load_dotenv

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
load_dotenv(REPO_ROOT / ".env")

sys.path.insert(0, str(REPO_ROOT / "web-app"))
from db import DB_CONFIG, get_conn  # noqa: E402

MANIFEST_DIR = REPO_ROOT / "tmp" / "orthanc_holdout"


def utc_stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def fetch_series_rows(args: argparse.Namespace) -> list[dict[str, Any]]:
    where = []
    params: list[str] = []
    if args.patient_id:
        where.append("patient_id = %s")
        params.append(args.patient_id)
    if args.studyinstanceuid:
        where.append("studyinstanceuid = %s")
        params.append(args.studyinstanceuid)
    if args.seriesinstanceuid:
        where.append("seriesinstanceuid = %s")
        params.append(args.seriesinstanceuid)
    if args.dicom_dir_path:
        where.append("dicom_dir_path = %s")
        params.append(str(args.dicom_dir_path))
    if not where:
        raise SystemExit(
            "hide requires one selector: --patient-id, --studyinstanceuid, "
            "--seriesinstanceuid, or --dicom-dir-path"
        )

    q = (
        "SELECT patient_id, studyinstanceuid, seriesinstanceuid, dicom_dir_path "
        "FROM image_series WHERE "
        + " AND ".join(where)
        + " ORDER BY patient_id, studyinstanceuid, seriesinstanceuid"
    )
    conn = get_conn()
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(q, params)
            return [dict(r) for r in cur.fetchall()]
    finally:
        conn.close()


def build_manifest(rows: list[dict[str, Any]]) -> dict[str, Any]:
    token = utc_stamp()
    entries = []
    seen: set[str] = set()
    for row in rows:
        src = str(row.get("dicom_dir_path") or "").strip()
        if not src or src in seen:
            continue
        seen.add(src)
        original = Path(src)
        hidden = original.parent / f"{original.name}.orthanc_holdout_{token}"
        entries.append(
            {
                "patient_id": row.get("patient_id"),
                "studyinstanceuid": row.get("studyinstanceuid"),
                "seriesinstanceuid": row.get("seriesinstanceuid"),
                "original_path": str(original),
                "hidden_path": str(hidden),
            }
        )
    return {
        "version": 1,
        "created_at_utc": token,
        "entry_count": len(entries),
        "entries": entries,
    }


def hide(args: argparse.Namespace) -> int:
    rows = fetch_series_rows(args)
    if not rows:
        raise SystemExit("No matching image_series rows found.")

    manifest = build_manifest(rows)
    entries = manifest["entries"]
    if not entries:
        raise SystemExit("No non-empty dicom_dir_path values found for the selected rows.")

    for e in entries:
        src = Path(e["original_path"])
        dst = Path(e["hidden_path"])
        if not src.is_dir():
            raise SystemExit(f"Source directory does not exist: {src}")
        if dst.exists():
            raise SystemExit(f"Hidden destination already exists: {dst}")

    print("Hide candidate summary")
    print(f"  matched SQL rows:  {len(rows)}")
    print(f"  unique series dirs: {len(entries)}")
    print(f"  selector:          {selection_summary(args)}")
    for e in entries[:5]:
        print(f"    {e['original_path']}")
    if len(entries) > 5:
        print(f"    ... and {len(entries) - 5} more")

    if args.dry_run:
        print("\nDry-run only. No directories renamed.")
        return 0

    MANIFEST_DIR.mkdir(parents=True, exist_ok=True)
    manifest_path = MANIFEST_DIR / f"orthanc_holdout_{manifest['created_at_utc']}.json"

    renamed: list[tuple[Path, Path]] = []
    try:
        for e in entries:
            src = Path(e["original_path"])
            dst = Path(e["hidden_path"])
            src.rename(dst)
            renamed.append((src, dst))
    except Exception:
        for src, dst in reversed(renamed):
            if dst.exists() and not src.exists():
                dst.rename(src)
        raise

    manifest_path.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    print("\nHidden successfully.")
    print(f"  manifest: {manifest_path}")
    print("Use OHIF now, then restore with:")
    print(f"  python3 {__file__} restore --manifest {manifest_path}")
    return 0


def restore(args: argparse.Namespace) -> int:
    manifest_path = args.manifest.resolve()
    if not manifest_path.is_file():
        raise SystemExit(f"Manifest not found: {manifest_path}")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    entries = manifest.get("entries") or []
    if not entries:
        raise SystemExit("Manifest has no entries.")

    for e in entries:
        src = Path(e["hidden_path"])
        dst = Path(e["original_path"])
        if not src.exists():
            raise SystemExit(f"Hidden path is missing: {src}")
        if dst.exists():
            raise SystemExit(f"Original path already exists, refusing to overwrite: {dst}")

    for e in entries:
        src = Path(e["hidden_path"])
        dst = Path(e["original_path"])
        src.rename(dst)

    print("Restore complete.")
    print(f"  restored directories: {len(entries)}")
    print(f"  manifest: {manifest_path}")
    return 0


def selection_summary(args: argparse.Namespace) -> str:
    if args.patient_id:
        return f"patient_id={args.patient_id}"
    if args.studyinstanceuid:
        return f"studyinstanceuid={args.studyinstanceuid}"
    if args.seriesinstanceuid:
        return f"seriesinstanceuid={args.seriesinstanceuid}"
    if args.dicom_dir_path:
        return f"dicom_dir_path={args.dicom_dir_path}"
    return "(none)"


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        description="Temporarily hide a patient/study/series from the legacy DICOM tree, then restore it."
    )
    sub = ap.add_subparsers(dest="cmd", required=True)

    hide_p = sub.add_parser("hide", help="Rename matching series directories away")
    sel = hide_p.add_mutually_exclusive_group(required=True)
    sel.add_argument("--patient-id")
    sel.add_argument("--studyinstanceuid")
    sel.add_argument("--seriesinstanceuid")
    sel.add_argument("--dicom-dir-path", type=Path)
    hide_p.add_argument("--dry-run", action="store_true", help="Preview only")

    restore_p = sub.add_parser("restore", help="Restore directories from a manifest")
    restore_p.add_argument("--manifest", type=Path, required=True)
    return ap


def main() -> int:
    ap = build_parser()
    args = ap.parse_args()
    if args.cmd == "hide":
        return hide(args)
    if args.cmd == "restore":
        return restore(args)
    raise SystemExit(f"Unknown command: {args.cmd}")


if __name__ == "__main__":
    raise SystemExit(main())
