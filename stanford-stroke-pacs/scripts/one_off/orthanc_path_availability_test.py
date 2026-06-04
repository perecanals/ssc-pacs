#!/usr/bin/env python3
"""
Safe test: verify Orthanc can (not) serve an instance when its file is missing
from the indexed tree, then restore the file and verify serving works again
without re-ingesting.

What it does (with --execute only):
  1. Read candidate paths from PostgreSQL table image_series (dicom_dir_path); skip rows where
     that path is not a directory on the machine running the script (filesystem), even if the row is valid in SQL.
  2. Pick file(s) under those on-disk trees, read SOP Instance UID (pydicom).
  3. Resolve Orthanc instance ID via POST /tools/find (Orthanc is not used to choose series).
  4. GET /instances/{id}/file — expect HTTP 200.
  5. Move the file aside on disk (same filesystem: .orthanc_path_test_bak).
  6. GET again — expect failure (4xx/5xx or connection error).
  7. Move file back to the original path.
  8. GET again — expect HTTP 200.

Default (no --execute): dry-run only; no filesystem changes.

Requirements: pydicom, requests, python-dotenv, psycopg2, ORTHANC_* and DB_* in .env

Example:
  python3 scripts/orthanc_path_availability_test.py
  python3 scripts/orthanc_path_availability_test.py --execute
  python3 scripts/orthanc_path_availability_test.py --execute --patient-id 1-017
  python3 scripts/orthanc_path_availability_test.py --dicom-file /DATA2/pacs_imaging_data/.../instance.dcm
  python3 scripts/orthanc_path_availability_test.py --execute --samples 15
  python3 scripts/orthanc_path_availability_test.py --execute --samples 15 --random-sample
  python3 scripts/orthanc_path_availability_test.py --execute --samples 15 --pick max --scan-rows 8000
  python3 scripts/orthanc_path_availability_test.py --execute --samples 1 --full-series --max-series-files 0
"""

from __future__ import annotations

import argparse
import os
import random
import re
import sys
import time
from pathlib import Path
from typing import Any

import requests
from dotenv import load_dotenv

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
load_dotenv(REPO_ROOT / ".env")

try:
    import pydicom
except ImportError:
    print("This script requires pydicom: pip install pydicom", file=sys.stderr)
    raise SystemExit(1)

import psycopg2

sys.path.insert(0, str(REPO_ROOT / "web-app"))
from db import DB_CONFIG, get_conn  # noqa: E402

ORTHANC_URL = os.getenv("ORTHANC_URL", "http://localhost:8042").rstrip("/")
ORTHANC_USER = os.getenv("ORTHANC_ADMIN_USER")
ORTHANC_PASS = os.getenv("ORTHANC_ADMIN_PASSWORD")

# Smallest file we consider (many secondary-capture objects are small)
MIN_DICOM_BYTES = 256
# Default row cap for ORDER BY seriesinstanceuid (deterministic but can hit few distinct series)
DB_SCAN_LIMIT = 300
DB_SCAN_LIMIT_PER_SAMPLE = 40  # legacy; prefer _dynamic_scan_limit below
MAX_PROBE_FILES = 500
MAX_FALLBACK_READS = 120
UIDISH_NAME_RE = re.compile(r"^\d+(?:\.\d+)+$")


def orthanc_auth() -> tuple[str, str]:
    if not ORTHANC_USER or not ORTHANC_PASS:
        raise SystemExit("ORTHANC_ADMIN_USER and ORTHANC_ADMIN_PASSWORD must be set in .env")
    return (ORTHANC_USER, ORTHANC_PASS)


def _is_probably_dicom(p: Path) -> bool:
    suf = p.suffix.lower()
    return (
        suf in (".dcm", ".dic", ".img")
        or suf == ""
        or p.name.lower().endswith(".dcm")
        or UIDISH_NAME_RE.fullmatch(p.name) is not None
    )


def _dynamic_scan_limit(
    n_samples: int, random_order: bool, scan_rows_override: int | None
) -> int:
    if scan_rows_override is not None:
        return max(1, scan_rows_override)
    if random_order:
        # Random scan: need enough rows to hit many distinct series/patients
        return min(20000, max(3000, n_samples * 800))
    return min(20000, max(1500, 800 + n_samples * 500))


def _scan_candidates(
    patient_id: str | None,
    row_limit: int,
    *,
    random_order: bool,
) -> tuple[list[tuple[Path, str, str, int, Path]], dict[str, int | list[str]]]:
    """Return flat candidate list (path, series_uid, study_uid, size, series_root) and stats."""
    if not DB_CONFIG.get("user"):
        raise SystemExit("DB_USER not set in .env")
    conn = psycopg2.connect(**DB_CONFIG)
    try:
        with conn.cursor() as cur:
            q = (
                "SELECT seriesinstanceuid, studyinstanceuid, dicom_dir_path "
                "FROM image_series "
                "WHERE dicom_dir_path IS NOT NULL AND dicom_dir_path != ''"
            )
            params: list[str] = []
            if patient_id:
                q += " AND patient_id = %s"
                params.append(patient_id)
            if random_order:
                q += f" ORDER BY random() LIMIT {row_limit}"
            else:
                q += f" ORDER BY seriesinstanceuid LIMIT {row_limit}"
            cur.execute(q, params)
            rows = cur.fetchall()
    finally:
        conn.close()

    stats: dict[str, int | list[str]] = {
        "db_rows": len(rows),
        "missing_dir": 0,
        "distinct_series_in_rows": len({r[0] for r in rows}),
        "files_probable_dicom": 0,
        "files_too_small": 0,
        "sample_paths": [],
    }

    candidates: list[tuple[Path, str, str, int, Path]] = []
    probe: list[tuple[Path, str, str, int, Path]] = []

    for suid, stuid, dpath in rows:
        root = Path(dpath)
        if len(stats["sample_paths"]) < 3:
            stats["sample_paths"].append(str(root))
        if not root.is_dir():
            stats["missing_dir"] += 1
            continue
        for p in root.rglob("*"):
            if not p.is_file():
                continue
            try:
                sz = p.stat().st_size
            except OSError:
                continue
            if sz < MIN_DICOM_BYTES:
                stats["files_too_small"] += 1
                continue
            if _is_probably_dicom(p):
                stats["files_probable_dicom"] += 1
                candidates.append((p, suid, stuid, sz, root))
            elif len(probe) < MAX_PROBE_FILES:
                probe.append((p, suid, stuid, sz, root))

    if not candidates:
        tried = 0
        for p, suid, stuid, sz, root in sorted(probe, key=lambda x: x[3]):
            if tried >= MAX_FALLBACK_READS:
                break
            tried += 1
            try:
                ds = pydicom.dcmread(str(p), stop_before_pixels=True, force=True)
            except Exception:
                continue
            if getattr(ds, "SOPInstanceUID", None):
                candidates.append((p, suid, stuid, sz, root))

    stats["dir_ok_rows"] = stats["db_rows"] - stats["missing_dir"]
    stats["series_with_candidate"] = len({c[1] for c in candidates})
    return candidates, stats


def _list_dicom_files_under_series_root(root: Path) -> list[Path]:
    """All probable DICOM files under image_series.dicom_dir_path (one series tree)."""
    out: list[Path] = []
    if not root.is_dir():
        return out
    for p in root.rglob("*"):
        if not p.is_file():
            continue
        try:
            sz = p.stat().st_size
        except OSError:
            continue
        if sz < MIN_DICOM_BYTES:
            continue
        if _is_probably_dicom(p):
            out.append(p)
    return sorted(out)


def expand_cases_full_series(
    picked: list[tuple[Path, str, str, int, Path]],
    max_per_series: int | None,
) -> tuple[list[tuple[Path, str, str, int]], list[tuple[str, int]]]:
    """Flatten to one row per instance; also return (series_uid, file_count) per series."""
    out: list[tuple[Path, str, str, int]] = []
    counts: list[tuple[str, int]] = []
    for _p, suid, stuid, _sz, root in picked:
        files = _list_dicom_files_under_series_root(root)
        n = len(files)
        if max_per_series is not None and n > max_per_series:
            files = files[:max_per_series]
            n = len(files)
        counts.append((suid, n))
        for fp in files:
            out.append((fp, suid, stuid, 0))
    return out, counts


def pick_dicom_file(patient_id: str | None) -> tuple[Path, str, str]:
    """Return (path, seriesinstanceuid, studyinstanceuid) — smallest file overall."""
    files, _stats = pick_dicom_files(patient_id, n_samples=1, random_order=False, pick="min")
    p, suid, stuid, _, _root = files[0]
    return p, suid, stuid


def pick_dicom_files(
    patient_id: str | None,
    n_samples: int,
    *,
    random_order: bool = False,
    pick: str = "min",
    scan_rows: int | None = None,
) -> tuple[list[tuple[Path, str, str, int, Path]], dict[str, int | list[str]]]:
    """Up to n_samples series: one representative file each (min or max size in that series).

    Each item is (path, series_uid, study_uid, size, series_root) where series_root is
    dicom_dir_path for that row (used to expand --full-series).

    Returns (list, stats) so callers can report how many series were available vs requested.
    """
    row_limit = _dynamic_scan_limit(n_samples, random_order, scan_rows)
    candidates, stats = _scan_candidates(patient_id, row_limit, random_order=random_order)

    if not candidates:
        print("\nDiagnostics (why nothing was selected):", file=sys.stderr)
        print(f"  image_series rows scanned: {stats['db_rows']}", file=sys.stderr)
        print(f"  distinct series in those rows: {stats.get('distinct_series_in_rows', '?')}", file=sys.stderr)
        print(f"  dicom_dir_path not a directory on this machine: {stats['missing_dir']}", file=sys.stderr)
        print(f"  files skipped (< {MIN_DICOM_BYTES} B): {stats['files_too_small']}", file=sys.stderr)
        print(f"  probable-DICOM files seen: {stats['files_probable_dicom']}", file=sys.stderr)
        print(
            f"  rows with existing dir: {stats.get('dir_ok_rows', '?')}; "
            f"series that yielded ≥1 candidate file: {stats.get('series_with_candidate', 0)}",
            file=sys.stderr,
        )
        print("  sample dicom_dir_path values:", file=sys.stderr)
        for s in stats["sample_paths"]:
            print(f"    {s}", file=sys.stderr)
        raise SystemExit(
            "No suitable DICOM file found. Often: DB paths point to /DATA2/... but this host "
            "does not have that mount, or use --dicom-file PATH to test a known file."
        )

    # One representative file per series (smallest or largest in that series tree)
    per_series: dict[str, tuple[Path, str, str, int, Path]] = {}
    for p, suid, stuid, sz, root in candidates:
        prev = per_series.get(suid)
        if prev is None:
            per_series[suid] = (p, suid, stuid, sz, root)
        elif pick == "max":
            if sz > prev[3]:
                per_series[suid] = (p, suid, stuid, sz, root)
        else:
            if sz < prev[3]:
                per_series[suid] = (p, suid, stuid, sz, root)

    items = list(per_series.values())
    if random_order:
        random.shuffle(items)
    else:
        items = sorted(items, key=lambda x: x[3])
    stats["distinct_series_on_disk"] = len(per_series)
    return items[: max(1, n_samples)], stats


def classify_missing_response(
    baseline_len: int, missing_status: int, missing_len: int
) -> str:
    """After renaming source file away: does Orthanc still serve full payload?"""
    if missing_status != 200:
        return "path_likely (HTTP not 200 while file missing)"
    if baseline_len > 1000 and missing_len > 1000 and missing_len >= baseline_len * 0.95:
        return "internal_copy_likely (HTTP 200, ~same size while file missing)"
    if missing_len < 500:
        return "path_likely (HTTP 200 but tiny body)"
    return "ambiguous"


def pick_dicom_file_explicit(path: Path) -> tuple[Path, str, str]:
    path = path.resolve()
    if not path.is_file():
        raise SystemExit(f"Not a file: {path}")
    ds = pydicom.dcmread(str(path), stop_before_pixels=True, force=True)
    suid = str(getattr(ds, "SeriesInstanceUID", "") or "")
    stuid = str(getattr(ds, "StudyInstanceUID", "") or "")
    return path, suid, stuid


def read_sop_instance_uid(path: Path) -> str:
    ds = pydicom.dcmread(str(path), stop_before_pixels=True, force=True)
    uid = getattr(ds, "SOPInstanceUID", None)
    if not uid:
        raise ValueError(f"No SOPInstanceUID in {path}")
    return str(uid)


def read_instance_uids(path: Path) -> tuple[str, str, str]:
    ds = pydicom.dcmread(str(path), stop_before_pixels=True, force=True)
    study_uid = str(getattr(ds, "StudyInstanceUID", None) or "")
    series_uid = str(getattr(ds, "SeriesInstanceUID", None) or "")
    sop_uid = str(getattr(ds, "SOPInstanceUID", None) or "")
    if not study_uid or not series_uid or not sop_uid:
        raise ValueError(
            f"Missing StudyInstanceUID / SeriesInstanceUID / SOPInstanceUID in {path}"
        )
    return study_uid, series_uid, sop_uid


def orthanc_find_instance_id(sop_instance_uid: str) -> str:
    auth = orthanc_auth()
    r = requests.post(
        f"{ORTHANC_URL}/tools/find",
        json={"Level": "Instance", "Query": {"SOPInstanceUID": sop_instance_uid}},
        auth=auth,
        timeout=60,
    )
    if r.status_code != 200:
        raise RuntimeError(f"Orthanc /tools/find failed: {r.status_code} {r.text[:500]}")
    ids = r.json()
    if not ids:
        raise RuntimeError(
            f"No Orthanc instance for SOPInstanceUID={sop_instance_uid}. "
            "Is this study indexed? Try a patient Orthanc already scanned."
        )
    if len(ids) > 1:
        print(f"Note: multiple Orthanc IDs for SOP (using first): {ids[:3]}...")
    return str(ids[0])


def fetch_instance_file(orthanc_instance_id: str) -> tuple[int, int]:
    """Return (http_status, content_length or -1)."""
    auth = orthanc_auth()
    r = requests.get(
        f"{ORTHANC_URL}/instances/{orthanc_instance_id}/file",
        auth=auth,
        timeout=120,
    )
    ln = len(r.content) if r.content else 0
    return r.status_code, ln


def fetch_instance_dicomweb(
    study_instance_uid: str, series_instance_uid: str, sop_instance_uid: str
) -> tuple[int, int]:
    """Return (http_status, content_length) via DICOMweb RetrieveInstance."""
    auth = orthanc_auth()
    r = requests.get(
        (
            f"{ORTHANC_URL}/dicom-web/studies/{study_instance_uid}"
            f"/series/{series_instance_uid}/instances/{sop_instance_uid}"
        ),
        headers={"Accept": 'multipart/related; type="application/dicom"'},
        auth=auth,
        timeout=120,
    )
    ln = len(r.content) if r.content else 0
    return r.status_code, ln


def fetch_instance_payload(
    *,
    transport: str,
    orthanc_instance_id: str,
    study_instance_uid: str,
    series_instance_uid: str,
    sop_instance_uid: str,
) -> tuple[int, int]:
    if transport == "dicom-web":
        return fetch_instance_dicomweb(study_instance_uid, series_instance_uid, sop_instance_uid)
    return fetch_instance_file(orthanc_instance_id)


def _print_case_header(
    index: int, total: int, path: Path, series_uid: str, study_uid: str
) -> None:
    print(f"\n{'='*60}")
    print(f"Sample {index}/{total}")
    print(f"{'='*60}")
    print(f"File: {path}")
    print(f"  seriesinstanceuid: {series_uid or '(none in header)'}")
    st_disp = study_uid[:50] + "..." if len(study_uid) > 50 else study_uid
    print(f"  studyinstanceuid: {st_disp or '(none in header)'}")


def run_one_sample(path: Path, execute: bool, transport: str) -> dict[str, Any]:
    """Baseline probe; if execute, move/rename test and restore. Returns result dict."""
    out: dict[str, Any] = {"path": str(path), "skipped": False, "error": None}
    try:
        try:
            study_uid, series_uid, sop = read_instance_uids(path)
            oid = orthanc_find_instance_id(sop)
        except (ValueError, RuntimeError) as e:
            out["skipped"] = True
            out["error"] = str(e)
            print(f"  SKIP: {e}")
            return out
        out["study_uid"] = study_uid
        out["series_uid"] = series_uid
        out["sop"] = sop
        out["orthanc_id"] = oid
        st_b, len_b = fetch_instance_payload(
            transport=transport,
            orthanc_instance_id=oid,
            study_instance_uid=study_uid,
            series_instance_uid=series_uid,
            sop_instance_uid=sop,
        )
        out["baseline_status"] = st_b
        out["baseline_len"] = len_b
        print(f"  SOPInstanceUID: {sop}")
        print(f"  Orthanc ID:     {oid}")
        print(f"  Transport:      {transport}")
        print(f"  Baseline GET:   HTTP {st_b}, ~{len_b} bytes")
        if st_b != 200:
            out["skipped"] = True
            out["error"] = "baseline_not_200"
            return out

        backup = path.with_name(path.name + ".orthanc_path_test_bak")
        out["classification"] = None
        out["missing_status"] = out["missing_len"] = None
        out["after_status"] = out["after_len"] = None
        out["restore_ok"] = None

        if not execute:
            out["classification"] = "dry_run"
            print(f"  Backup name:    {backup.name}")
            return out

        if backup.exists():
            out["skipped"] = True
            out["error"] = f"backup_exists:{backup}"
            return out

        print(f"  [1/4] Move aside -> {backup.name}")
        path.rename(backup)
        time.sleep(0.5)
        try:
            print("  [2/4] GET while file missing...")
            st_m, len_m = fetch_instance_payload(
                transport=transport,
                orthanc_instance_id=oid,
                study_instance_uid=study_uid,
                series_instance_uid=series_uid,
                sop_instance_uid=sop,
            )
            out["missing_status"] = st_m
            out["missing_len"] = len_m
            print(f"        HTTP {st_m}, ~{len_m} bytes")
            out["classification"] = classify_missing_response(len_b, st_m, len_m)
            print(f"  --> {out['classification']}")

            print("  [3/4] Restore file...")
            backup.rename(path)
            time.sleep(0.5)

            print("  [4/4] GET after restore...")
            st_a, len_a = fetch_instance_payload(
                transport=transport,
                orthanc_instance_id=oid,
                study_instance_uid=study_uid,
                series_instance_uid=series_uid,
                sop_instance_uid=sop,
            )
            out["after_status"] = st_a
            out["after_len"] = len_a
            print(f"        HTTP {st_a}, ~{len_a} bytes")
            out["restore_ok"] = st_a == 200 and (
                abs(len_a - len_b) <= max(100, len_b // 50) or len_b == 0
            )
        except Exception:
            if backup.exists() and not path.exists():
                print("  Restoring file after error...", file=sys.stderr)
                backup.rename(path)
            raise

        return out
    except Exception as e:
        out["skipped"] = True
        out["error"] = str(e)
        print(f"  ERROR: {e}", file=sys.stderr)
        return out


def main() -> int:
    ap = argparse.ArgumentParser(description="Test Orthanc serve-after-file-missing-then-restore")
    ap.add_argument(
        "--execute",
        action="store_true",
        help="Actually move the file aside and back (default: dry-run only)",
    )
    ap.add_argument("--patient-id", help="Limit image_series rows to this patient_id")
    ap.add_argument(
        "--dicom-file",
        type=Path,
        help="Use this file instead of picking from image_series (must exist on this host)",
    )
    ap.add_argument(
        "--samples",
        type=int,
        default=1,
        metavar="N",
        help="Number of series to test (one file per distinct series; default: 1)",
    )
    ap.add_argument(
        "--random-sample",
        action="store_true",
        help="ORDER BY random() when reading image_series (more spread; slower on large tables)",
    )
    ap.add_argument(
        "--pick",
        choices=("min", "max"),
        default="min",
        help="Per series: test smallest (min) or largest (max) DICOM file under dicom_dir_path (default: min)",
    )
    ap.add_argument(
        "--scan-rows",
        type=int,
        metavar="N",
        help="Override max image_series rows to scan (default scales with --samples and --random-sample)",
    )
    ap.add_argument(
        "--full-series",
        action="store_true",
        help="After choosing series(es), test every probable DICOM file under each series dicom_dir_path "
        "(not just one slice). Use --max-series-files to cap per series.",
    )
    ap.add_argument(
        "--max-series-files",
        type=int,
        default=500,
        metavar="N",
        help="With --full-series: max instances to test per series (default: 500; 0 = no limit)",
    )
    ap.add_argument(
        "--transport",
        choices=("dicom-web", "instance-file"),
        default="dicom-web",
        help="Orthanc route to test: DICOMweb RetrieveInstance (closer to OHIF) or raw /instances/{id}/file "
        "(default: dicom-web)",
    )
    args = ap.parse_args()

    if args.samples < 1:
        raise SystemExit("--samples must be >= 1")
    if args.samples > 1 and args.dicom_file:
        raise SystemExit("--samples > 1 cannot be combined with --dicom-file")
    cap: int | None = None
    if args.full_series:
        if args.max_series_files < 0:
            raise SystemExit("--max-series-files must be >= 0")
        cap = None if args.max_series_files == 0 else args.max_series_files

    print("Orthanc path availability test")
    print(f"  Paths:   PostgreSQL image_series.dicom_dir_path (not Orthanc's DB)")
    print(f"  Orthanc: {ORTHANC_URL} (only /tools/find + GET after a file is chosen)")
    print(f"  Mode:    {'EXECUTE (will touch files)' if args.execute else 'DRY-RUN (no changes)'}")
    print(f"  Route:   {args.transport}")
    mode = "full series (all slices under dicom_dir_path)" if args.full_series else "one instance per series"
    print(f"  Samples: {args.samples} series, {mode}, pick={args.pick}")
    if args.random_sample:
        print("  SQL:     ORDER BY random() on image_series")
    if args.scan_rows is not None:
        print(f"  scan-rows: {args.scan_rows}")
    if args.full_series:
        print(
            f"  full-series cap: {'none' if cap is None else cap} file(s) per series",
        )

    pick_stats: dict[str, int | list[str]] | None = None
    if args.dicom_file:
        p, su, st = pick_dicom_file_explicit(args.dicom_file)
        root = p.parent
        picked = [(p, su, st, 0, root)]
        if args.full_series:
            cases, series_counts = expand_cases_full_series(picked, cap)
            print(
                f"\n  --dicom-file + --full-series: using tree root {root} "
                f"({sum(c for _, c in series_counts)} file(s)).",
            )
        else:
            cases = [(p, su, st, 0)]
    else:
        picked, pick_stats = pick_dicom_files(
            args.patient_id,
            args.samples,
            random_order=args.random_sample,
            pick=args.pick,
            scan_rows=args.scan_rows,
        )
        if len(picked) < args.samples and pick_stats is not None:
            md = pick_stats.get("missing_dir", "?")
            swc = pick_stats.get("series_with_candidate")
            dok = pick_stats.get("dir_ok_rows")
            extra = ""
            if swc is not None and dok is not None:
                extra = (
                    f"Only {swc} of {dok} directory rows produced ≥1 probable DICOM file under "
                    f"dicom_dir_path (unique seriesinstanceuid in SQL does not imply files still present). "
                )
            print(
                f"\nNote: only {len(picked)} series to test after scan of "
                f"{pick_stats['db_rows']} PostgreSQL image_series row(s) "
                f"({md} row(s) skipped: path not a directory here). "
                f"{pick_stats.get('distinct_series_in_rows', '?')} distinct series UIDs in that row set. "
                f"{extra}"
                "Orthanc does not choose this list. If series_with_candidate is low, dirs may be empty, "
                "files may not match the script’s DICOM filename heuristics, or the scan did not finish walking all rows.",
                file=sys.stderr,
            )
        if args.full_series:
            cases, series_counts = expand_cases_full_series(picked, cap)
            total = sum(c for _, c in series_counts)
            print(
                f"\n  Expanded to {total} instance(s) across {len(series_counts)} series "
                f"(cap={'none' if cap is None else cap} per series).",
            )
        else:
            cases = [(a, b, c, d) for (a, b, c, d, _r) in picked]

    print(
        "\nNote: Orthanc's Docker volume (~GiB) cannot hold a full copy of a large archive (~TiB). "
        "If some instances still serve while the source path is missing, Orthanc likely has an "
        "attachment for those instances only; others may still read from /dicom-data."
    )

    results: list[dict[str, Any]] = []
    for i, (path, series_uid, study_uid, _sz) in enumerate(cases, 1):
        _print_case_header(i, len(cases), path, series_uid, study_uid)
        r = run_one_sample(path, args.execute, args.transport)
        results.append(r)
        if r.get("error") == "baseline_not_200":
            print("  (skip: baseline not 200)")
        elif (r.get("error") or "").startswith("backup_exists"):
            print(f"  SKIP: {r['error']}")

    # Summary
    print(f"\n{'#'*60}\nSUMMARY ({len(results)} sample(s))\n{'#'*60}")

    if args.execute:
        internal = path_backed = amb = skip = 0
        for r in results:
            if r.get("skipped"):
                skip += 1
                continue
            c = r.get("classification") or ""
            if "internal_copy_likely" in c:
                internal += 1
            elif "path_likely" in c:
                path_backed += 1
            elif "ambiguous" in c:
                amb += 1

        print(
            f"  internal_copy_likely (HTTP 200 + ~same size while file missing): {internal}\n"
            f"  path_likely (not 200 or tiny body while missing):               {path_backed}\n"
            f"  ambiguous:                                                       {amb}\n"
            f"  skipped (lookup/baseline/backup):                              {skip}"
        )
        detail_limit = 40
        if len(results) <= detail_limit:
            for r in results:
                c = r.get("classification")
                if c and c != "dry_run":
                    short = str(r["path"])[-72:]
                    print(f"    ...{short} -> {c}")
        else:
            print(
                f"    (per-file lines omitted: {len(results)} samples; showing counts only; "
                f"re-run with fewer series or lower --max-series-files to list each)",
            )
        bad_restore = [r for r in results if r.get("restore_ok") is False]
        if bad_restore:
            print(f"\nWARNING: {len(bad_restore)} sample(s) did not restore fetch cleanly.")
            return 3
        return 0

    print("Dry-run complete (no files moved). To execute:")
    cmd = f"python3 {sys.argv[0]} --execute --samples {args.samples}"
    if args.pick != "min":
        cmd += f" --pick {args.pick}"
    if args.transport != "dicom-web":
        cmd += f" --transport {args.transport}"
    if args.random_sample:
        cmd += " --random-sample"
    if args.scan_rows is not None:
        cmd += f" --scan-rows {args.scan_rows}"
    if args.full_series:
        cmd += " --full-series"
        if args.max_series_files != 500:
            cmd += f" --max-series-files {args.max_series_files}"
    if args.patient_id:
        cmd += f" --patient-id {args.patient_id}"
    print(f"  {cmd}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
