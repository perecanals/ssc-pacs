#!/usr/bin/env python3
"""Backfill: register series that are in `image_series` but missing from Orthanc.

Detects series whose `image_series` row has no counterpart in Orthanc's index
(live `/tools/lookup` per candidate) — e.g. after an indexing failure or a scan
truncated by an Orthanc restart — and re-registers them via the fork's on-demand
scan endpoint (`scoped_index.py` → `POST /indexer/scan`). It passes `Force=true`,
which drops any stale index rows first, so it handles both "never scanned" and
"orphaned-row" (POST failed mid-scan) cases uniformly. Registers in BOUNDED
PASSES with a settle between them — one huge uninterrupted scan can OOM Orthanc
core (VM-global; the very failure mode this tool remediates), so large batches
are split (default 350 series / 40k instances per pass, 120 s settle; see
scoped_index.register_in_bounded_passes).

Usage
-----
    python scripts/cold_storage/reindex_missing_series.py                        # dry-run, everything
    python scripts/cold_storage/reindex_missing_series.py --label my_batch       # dry-run, one import_label
    python scripts/cold_storage/reindex_missing_series.py --label my_batch --execute
    python scripts/cold_storage/reindex_missing_series.py --exclude-label huge_batch --execute
    python scripts/cold_storage/reindex_missing_series.py --pass-instances 20000 --execute  # gentler passes
"""

from __future__ import annotations

import argparse
import concurrent.futures as cf
import os
import sys
from pathlib import Path

import psycopg2
import requests
from dotenv import load_dotenv

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
load_dotenv(REPO_ROOT / ".env")
sys.path.insert(0, str(REPO_ROOT / "web-app"))

import scoped_index as si  # noqa: E402


def _detect_missing(cur, session, orthanc_url, labels_sql, params, workers):
    """Return [(suid, dir, ninst, label)] for candidate series absent from Orthanc."""
    cur.execute(
        "SELECT seriesinstanceuid, dicom_dir_path, COALESCE(number_of_slices,0), "
        "import_label FROM image_series "
        "WHERE dicom_dir_path IS NOT NULL " + labels_sql,
        params,
    )
    candidates = cur.fetchall()

    def is_missing(row):
        try:
            r = session.post(f"{orthanc_url}/tools/lookup", data=row[0], timeout=15)
            return None if (r.ok and any(x.get("Type") == "Series" for x in r.json())) else row
        except requests.RequestException:
            return row

    missing = []
    with cf.ThreadPoolExecutor(max_workers=workers) as ex:
        for res in ex.map(is_missing, candidates):
            if res:
                missing.append(res)
    return candidates, missing


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--label", default=None, help="Restrict to one import_label.")
    ap.add_argument("--exclude-label", action="append", default=[],
                    help="Skip an import_label (repeatable; e.g. a huge batch "
                         "to handle separately).")
    ap.add_argument("--limit", type=int, default=None, help="Cap number of series (pilot).")
    ap.add_argument("--include-archive-only", action="store_true",
                    help="Warm archive-only (cold) series from tar.zst before indexing.")
    ap.add_argument("--workers", type=int, default=16, help="Parallel Orthanc lookups.")
    ap.add_argument("--pass-series", type=int, default=si.MAX_SERIES_PER_PASS,
                    help="Max series per bounded scan pass.")
    ap.add_argument("--pass-instances", type=int, default=si.MAX_INSTANCES_PER_PASS,
                    help="Max summed instances per bounded scan pass.")
    ap.add_argument("--settle", type=int, default=si.SETTLE_S,
                    help="Seconds to wait between passes (lets Orthanc memory settle).")
    ap.add_argument("--max-file-mb", type=int, default=800,
                    help="Skip series containing a single file larger than this "
                         "(registering a file costs Orthanc ~2-3x its size in RAM; "
                         "a multi-GB multiframe file OOMs the VM regardless of "
                         "pass size). 0 disables the guard.")
    ap.add_argument("--execute", action="store_true", help="Apply (default: report only).")
    args = ap.parse_args()

    from config import DICOM_DATA_ROOT, STORAGE_MODE  # noqa: E402
    from db import DB_CONFIG  # noqa: E402
    from orthanc_client import ORTHANC_PASS, ORTHANC_URL, ORTHANC_USER  # noqa: E402

    if STORAGE_MODE != "cold_path_cache":
        sys.exit(f"STORAGE_MODE is {STORAGE_MODE!r}, not cold_path_cache. Aborting.")

    if args.label:
        labels_sql, params = "AND import_label = %s", (args.label,)
    elif args.exclude_label:
        labels_sql = "AND (import_label IS NULL OR import_label <> ALL(%s))"
        params = (list(args.exclude_label),)
    else:
        labels_sql, params = "", ()

    session = requests.Session()
    session.auth = (ORTHANC_USER, ORTHANC_PASS)
    host_root = str(DICOM_DATA_ROOT)

    conn = psycopg2.connect(**DB_CONFIG)
    with conn.cursor() as cur:
        print("Detecting missing series (live Orthanc lookup)…")
        candidates, missing = _detect_missing(
            cur, session, ORTHANC_URL, labels_sql, params, args.workers)
    conn.close()
    print(f"  candidates={len(candidates)}  missing from Orthanc={len(missing)}")
    if not missing:
        print("Nothing to do.")
        return 0

    loose, archive_only, oversized = [], [], []
    for suid, ddir, n, _lbl in missing:
        target = si.SeriesTarget(suid, ddir, int(n))
        if os.path.isdir(ddir) and any(os.scandir(ddir)):
            if args.max_file_mb and si.dir_max_file_bytes(ddir) > args.max_file_mb * 1e6:
                oversized.append(target)
            else:
                loose.append(target)
        else:
            archive_only.append(target)

    print(f"  loose on disk: {len(loose)}")
    print(f"  archive-only:  {len(archive_only)}"
          + ("  (will warm first)" if args.include_archive_only
             else "  (SKIPPED — pass --include-archive-only)"))
    if oversized:
        print(f"  oversized:     {len(oversized)}  (SKIPPED — contain a file "
              f">{args.max_file_mb} MB that would OOM Orthanc; handle separately):")
        for t in oversized:
            print(f"    {t.suid}  {t.dicom_dir_path}")

    targets = list(loose)
    if args.include_archive_only:
        targets += archive_only
    if args.limit:
        targets = targets[:args.limit]

    tot_inst = sum(t.n_instances for t in targets)
    print(f"\nPlan: register {len(targets)} series (~{tot_inst:,} instances) via "
          f"POST /indexer/scan (Force=true) in bounded passes "
          f"(≤{args.pass_series} series / ≤{args.pass_instances:,} instances per "
          f"pass, {args.settle}s settle).")

    if not args.execute:
        print("\nDRY-RUN — re-run with --execute to apply.")
        return 0

    if args.include_archive_only and archive_only:
        from cache_manager import warm_series  # noqa: E402
        warm = [t for t in targets if t in archive_only]
        print(f"\nWarming {len(warm)} archive-only series…")
        for i in range(0, len(warm), 50):
            warm_series([t.suid for t in warm[i:i + 50]])

    summary = si.register_in_bounded_passes(
        targets, host_root=host_root, orthanc_url=ORTHANC_URL, session=session,
        force=True, granularity="series",
        max_series_per_pass=args.pass_series,
        max_instances_per_pass=args.pass_instances,
        settle_s=args.settle,
    )
    print(f"\nDone. Registered {summary['registered']}/{summary['targets']} series "
          f"in {summary['passes']} pass(es)"
          + (" [TRUNCATED — re-run to continue]" if summary["truncated"] else "") + ".")
    print("Verify: python scripts/data_integrity/reconcile.py")
    return 0 if summary["registered"] == summary["targets"] else 3


if __name__ == "__main__":
    raise SystemExit(main())
