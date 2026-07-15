#!/usr/bin/env python3
"""
Repair series that Orthanc can no longer serve WADO-RS metadata for.

THE BUG THIS REPAIRS
--------------------
Orthanc's DICOMweb plugin caches each series' WADO-RS metadata as an Orthanc
attachment (content type 4301), and it builds that cache by READING THE DICOM
FILES — in a background worker that fires when the series goes stable.

In `cold_path_cache` mode the files are absent whenever a series is evicted,
and the Orthanc index deliberately keeps pointing at them
(`"RemoveMissingFiles": false`). So a cache computed while a series is cold is
stored as an EMPTY ARRAY, and nothing ever invalidates it. From then on every
metadata request returns HTTP 400
("The series metadata json does not contain an array") and OHIF hangs forever
on the loading spinner.

Ingestion used to delete a case's loose DICOMs the instant Orthanc's indexer
had registered them — before that worker had run — so series ingested straight
into cold storage lost the race and were cached empty. (Fixed: check 5 in
scripts/cold_storage/cleanup_loose_dicoms.py now waits for the cache.)

THE REPAIR
----------
The cache can only be rebuilt while the files are on disk. So, per series:

    warm -> DELETE attachment 4301 -> GET .../metadata (rebuilds it) -> evict

Note the ordering is load-bearing. Deleting the poisoned cache on its own is
NOT a fix: the very next metadata request, if the series is cold, re-poisons it
on the spot. The DELETE and the rebuild must both happen while the series is
warm.

Series that were already hot before the run are left hot; only series this
script warmed are evicted again afterwards (`--keep-warm` skips that).

Safe to interrupt and re-run: it re-discovers the remaining broken series each
time, so it simply resumes. Warming is done one batch at a time and evicted
before moving on, so peak disk use is bounded by a single batch.

Usage:
  # What is broken? (default: report only, no warming, no writes)
  python scripts/data_integrity/repair_dicomweb_metadata_cache.py

  # Repair everything (slow: extracts every affected series once)
  python scripts/data_integrity/repair_dicomweb_metadata_cache.py --execute

  # Repair only what is already warm — nearly free, no extraction
  python scripts/data_integrity/repair_dicomweb_metadata_cache.py --execute --hot-only

  # Scope it
  python scripts/data_integrity/repair_dicomweb_metadata_cache.py --execute --patient <patient-id>
  python scripts/data_integrity/repair_dicomweb_metadata_cache.py --execute --limit 500
"""

from __future__ import annotations

import argparse
import os
import sys
import time
from collections import defaultdict
from pathlib import Path

import psycopg2
import psycopg2.extras
import requests
from dotenv import load_dotenv

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
load_dotenv(REPO_ROOT / ".env")
sys.path.insert(0, str(REPO_ROOT / "web-app"))

from db import DB_CONFIG  # noqa: E402
from orthanc_client import (  # noqa: E402
    DICOMWEB_SERIES_METADATA_ATTACHMENT,
    EMPTY_METADATA_CACHE_MAX_BYTES,
    ORTHANC_PASS,
    ORTHANC_USER,
    rebuild_series_metadata_cache,
    series_metadata_cache_is_healthy,
)

import cache_manager  # noqa: E402
from config import STORAGE_MODE  # noqa: E402

ORTHANC_DB_CONFIG = dict(
    host=os.getenv("DB_HOST", "localhost"),
    port=os.getenv("DB_PORT", "5432"),
    dbname=os.getenv("PG_ORTHANC_DB", "orthanc"),
    user=os.getenv("PG_ORTHANC_USER", "orthanc"),
    password=os.getenv("PG_ORTHANC_PASSWORD", ""),
)

# Orthanc index internals: resources.resourcetype 2 == Series; the
# SeriesInstanceUID lives in dicomidentifiers as tag (0020,000E).
SERIES_RESOURCE_TYPE = 2
SERIES_UID_GROUP, SERIES_UID_ELEMENT = 0x0020, 0x000E

# Session-scoped advisory lock: only one repair run at a time (see main()).
REPAIR_LOCK_KEY = 8741003


def find_broken_series(include_missing: bool) -> dict[str, str]:
    """Map {SeriesInstanceUID: orthanc_series_id} for series with a bad cache.

    Read-only against orthanc_db. "Bad" means the cached metadata attachment is
    the empty-array form (<= EMPTY_METADATA_CACHE_MAX_BYTES), or — with
    include_missing — that the series has no cache at all. A missing cache is
    not yet broken, but it is a live grenade: the first metadata request while
    the series is cold will poison it.
    """
    join = "JOIN" if not include_missing else "LEFT JOIN"
    where = (
        "a.uncompressedsize <= %s"
        if not include_missing
        else "(a.uncompressedsize <= %s OR a.id IS NULL)"
    )
    sql = f"""
        SELECT dv.value AS series_uid, r.publicid AS orthanc_id
        FROM resources r
        JOIN dicomidentifiers dv
          ON dv.id = r.internalid
         AND dv.taggroup = %s AND dv.tagelement = %s
        {join} attachedfiles a
          ON a.id = r.internalid AND a.filetype = %s
        WHERE r.resourcetype = %s AND {where}
    """
    conn = psycopg2.connect(**ORTHANC_DB_CONFIG)
    try:
        with conn.cursor() as cur:
            cur.execute(
                sql,
                (
                    SERIES_UID_GROUP,
                    SERIES_UID_ELEMENT,
                    DICOMWEB_SERIES_METADATA_ATTACHMENT,
                    SERIES_RESOURCE_TYPE,
                    EMPTY_METADATA_CACHE_MAX_BYTES,
                ),
            )
            return {row[0]: row[1] for row in cur.fetchall()}
    finally:
        conn.close()


def load_series_context(series_uids: list[str], patient: str | None,
                        study: str | None) -> list[dict]:
    """Join the broken series to their study/patient and current cache state."""
    sql = """
        SELECT s.seriesinstanceuid, s.studyinstanceuid, st.patient_id,
               COALESCE(cs.status, 'cold') AS cache_status
        FROM image_series s
        JOIN image_study st ON st.studyinstanceuid = s.studyinstanceuid
        LEFT JOIN series_cache_state cs
               ON cs.seriesinstanceuid = s.seriesinstanceuid
        WHERE s.seriesinstanceuid = ANY(%s)
    """
    params: list = [series_uids]
    if patient:
        sql += " AND st.patient_id = %s"
        params.append(patient)
    if study:
        sql += " AND s.studyinstanceuid = %s"
        params.append(study)
    sql += " ORDER BY st.patient_id, s.studyinstanceuid, s.seriesinstanceuid"

    conn = psycopg2.connect(**DB_CONFIG)
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(sql, params)
            return [dict(r) for r in cur.fetchall()]
    finally:
        conn.close()


def repair_batch(batch: list[dict], orthanc_ids: dict[str, str],
                 session: requests.Session, *, keep_warm: bool,
                 quiet: bool) -> tuple[int, int, list[str]]:
    """Warm -> rebuild -> evict one batch. Returns (repaired, failed, details)."""
    uids = [r["seriesinstanceuid"] for r in batch]
    # Only evict what we warmed; series the user already had hot stay hot.
    warmed_by_us = [r["seriesinstanceuid"] for r in batch
                    if r["cache_status"] != "hot"]

    warm_res = cache_manager.warm_series(uids)
    if not warm_res.get("ok"):
        return 0, len(uids), [
            f"warm failed for {len(uids)} series: {warm_res.get('error')}"
        ]

    repaired, failed, details = 0, 0, []
    for row in batch:
        suid = row["seriesinstanceuid"]
        oid = orthanc_ids.get(suid)
        if not oid:
            failed += 1
            details.append(f"{suid}: not resolvable in Orthanc")
            continue
        # warm_series' own safety net may already have rebuilt this; only
        # touch it if it is still unhealthy.
        if series_metadata_cache_is_healthy(oid, session=session):
            repaired += 1
            continue
        ok = rebuild_series_metadata_cache(
            row["studyinstanceuid"], suid, oid, session=session
        )
        if ok:
            repaired += 1
        else:
            failed += 1
            details.append(f"{suid}: metadata rebuild returned an empty result")

    if warmed_by_us and not keep_warm:
        # A failed eviction must never abort the run. The repair itself is
        # already committed (the metadata cache is rebuilt); failing to evict
        # only means the files stay on disk, which the next pass — or the TTL
        # evictor — will reclaim. Aborting here would strand 19k series.
        try:
            cache_manager.evict_series(warmed_by_us)
        except Exception as exc:
            details.append(
                f"evict failed for {len(warmed_by_us)} series (repair itself is "
                f"done; files left warm): {exc}"
            )

    if not quiet:
        print(f"    repaired {repaired}, failed {failed}, "
              f"{'kept warm' if keep_warm else f'evicted {len(warmed_by_us)}'}")
    return repaired, failed, details


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("--execute", action="store_true",
                    help="Actually repair (default: report only)")
    ap.add_argument("--patient", help="Limit to one patient_id")
    ap.add_argument("--study", help="Limit to one studyinstanceuid")
    ap.add_argument("--limit", type=int,
                    help="Repair at most this many series (resume-friendly)")
    ap.add_argument("--batch-size", type=int, default=40, metavar="N",
                    help="Series warmed at once before being evicted again "
                         "(default: 40). Bounds peak disk use.")
    ap.add_argument("--hot-only", action="store_true",
                    help="Only repair series that are already warm — no "
                         "extraction, so this is nearly free")
    ap.add_argument("--keep-warm", action="store_true",
                    help="Do not evict series this script warmed")
    ap.add_argument("--include-missing", action="store_true",
                    help="Also rebuild series that have no metadata cache yet "
                         "(not broken, but will poison on first cold request)")
    ap.add_argument("--quiet", action="store_true", help="Only print the summary")
    args = ap.parse_args()

    if STORAGE_MODE != "cold_path_cache":
        print(f"storage.mode is '{STORAGE_MODE}', not 'cold_path_cache' — "
              f"this repair does not apply.")
        return 0

    # Single-instance guard. Two concurrent runs compute the same broken-series
    # set, warm the same series and then race each other's rmtree — whoever
    # loses the final rmdir() dies with ENOENT. Hold a session-scoped advisory
    # lock for the life of the run; a second invocation exits immediately.
    # Only --execute takes it: a dry run is read-only, so it stays usable for
    # checking progress while a repair is in flight.
    if args.execute:
        guard = psycopg2.connect(**DB_CONFIG)
        with guard.cursor() as gcur:
            gcur.execute("SELECT pg_try_advisory_lock(%s)", (REPAIR_LOCK_KEY,))
            if not gcur.fetchone()[0]:
                print("Another repair run is already in progress (advisory lock "
                      "held). Wait for it to finish, or stop it, then re-run — "
                      "the repair resumes where it left off.")
                return 1

    print("Scanning Orthanc's index for series with a broken metadata cache...")
    orthanc_ids = find_broken_series(args.include_missing)
    if not orthanc_ids:
        print("No broken series found. Nothing to do.")
        return 0

    rows = load_series_context(list(orthanc_ids), args.patient, args.study)
    if args.hot_only:
        rows = [r for r in rows if r["cache_status"] == "hot"]
    if args.limit:
        rows = rows[: args.limit]

    if not rows:
        print(f"{len(orthanc_ids)} broken series in Orthanc, but none match "
              f"the given filters.")
        return 0

    by_patient: dict[str, list[dict]] = defaultdict(list)
    for r in rows:
        by_patient[r["patient_id"]].append(r)
    n_hot = sum(1 for r in rows if r["cache_status"] == "hot")

    print(f"  {len(orthanc_ids)} series with a broken/absent metadata cache")
    print(f"  {len(rows)} selected, across {len(by_patient)} patients "
          f"({n_hot} already warm, {len(rows) - n_hot} need extracting)")

    if not args.execute:
        print("\nDry run — nothing changed. Re-run with --execute to repair.")
        print("Tip: --hot-only --execute repairs the already-warm series with "
              "no extraction cost.")
        return 0

    session = requests.Session()
    session.auth = (ORTHANC_USER, ORTHANC_PASS)

    t0 = time.perf_counter()
    total_repaired = total_failed = 0
    all_details: list[str] = []

    for patient_id, prows in sorted(by_patient.items()):
        if not args.quiet:
            print(f"  {patient_id}: {len(prows)} series")
        for i in range(0, len(prows), args.batch_size):
            batch = prows[i : i + args.batch_size]
            repaired, failed, details = repair_batch(
                batch, orthanc_ids, session,
                keep_warm=args.keep_warm or args.hot_only,
                quiet=args.quiet,
            )
            total_repaired += repaired
            total_failed += failed
            all_details.extend(details)

    elapsed = time.perf_counter() - t0
    print("\n" + "=" * 60)
    print(f"Repaired: {total_repaired}")
    print(f"Failed:   {total_failed}")
    print(f"Elapsed:  {elapsed / 60:.1f} min")
    if all_details:
        print("\nDetails (first 20):")
        for d in all_details[:20]:
            print(f"  {d}")
    return 1 if total_failed else 0


if __name__ == "__main__":
    sys.exit(main())
