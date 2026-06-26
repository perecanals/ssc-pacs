#!/usr/bin/env python3
"""
Warm one or more cold studies, wait for the patched Folder Indexer to (re)index
the restored files, then optionally evict them back to cold.

Use this to repair studies whose Orthanc index is missing or stale even though
their *.tar.zst archives are intact — e.g. series that were never indexed, or
"ghost" series left pointing at an old location. Warming lays the canonical files
down at each series' `dicom_dir_path`; the indexer picks them up on its next scan
(orthanc.json Indexer.Interval) and indexes them correctly. Eviction then removes
the loose files again while the index entries persist (RemoveMissingFiles: false).

After warming, run `prune_stale_index_paths.py --execute [--delete-orphans]` to
clear any stale duplicate / orphan index rows that the old location left behind.

Usage:
  # One study (canary)
  python scripts/cold_storage/warm_reindex.py --studies <studyUID>

  # All cold studies for a patient
  python scripts/cold_storage/warm_reindex.py --patient 4-1152

  # Warm + index but keep files warm (skip the eviction step)
  python scripts/cold_storage/warm_reindex.py --patient 4-1152 --no-evict
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

import psycopg2
import requests
from dotenv import load_dotenv

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
load_dotenv(REPO_ROOT / ".env")
sys.path.insert(0, str(REPO_ROOT / "web-app"))


def study_uids_for_patient(conn, patient: str) -> list[str]:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT DISTINCT studyinstanceuid FROM image_series "
            "WHERE patient_id = %s AND studyinstanceuid IS NOT NULL ORDER BY 1",
            (patient,),
        )
        return [r[0] for r in cur.fetchall()]


def indexer_interval(default: int = 60) -> int:
    import json
    try:
        cfg = json.loads((REPO_ROOT / "orthanc.json").read_text())
        return int(cfg.get("Indexer", {}).get("Interval", default))
    except (OSError, ValueError, TypeError):
        return default


def study_readability(session, url, studyuid: str) -> tuple[int, int]:
    """(n_series, n_series_whose_first_instance_reads_200) for a study.

    A series is "readable" only when its files are actually on disk AND the index
    points at them — the true signal that warming + (re)indexing has landed.
    Returns (0, 0) when the study is not indexed at all.
    """
    r = session.post(f"{url}/tools/find",
                     json={"Level": "Study", "Query": {"StudyInstanceUID": studyuid},
                           "Expand": True}, timeout=60)
    r.raise_for_status()
    found = r.json()
    if not found:
        return (0, 0)
    n_series = readable = 0
    for st in found:
        for sid in st.get("Series", []):
            s = session.get(f"{url}/series/{sid}", timeout=30)
            if s.status_code != 200:
                continue
            n_series += 1
            insts = s.json().get("Instances", [])
            if insts and session.get(f"{url}/instances/{insts[0]}/file",
                                     timeout=20).status_code == 200:
                readable += 1
    return (n_series, readable)


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--patient", help="Warm all cold studies for this patient_id")
    g.add_argument("--studies", help="Comma-separated StudyInstanceUIDs")
    ap.add_argument("--evict", action="store_true",
                    help="Evict back to cold after indexing is verified (default: leave warm)")
    ap.add_argument("--index-wait", type=int, default=240,
                    help="Max seconds to wait for the indexer per study (default 240)")
    args = ap.parse_args()

    from cache_manager import evict_study, warm_study  # noqa: E402
    from config import STORAGE_MODE  # noqa: E402
    from db import DB_CONFIG  # noqa: E402
    from orthanc_client import ORTHANC_PASS, ORTHANC_URL, ORTHANC_USER  # noqa: E402

    if STORAGE_MODE != "cold_path_cache":
        print(f"STORAGE_MODE is '{STORAGE_MODE}', not cold_path_cache. Aborting.", file=sys.stderr)
        return 2

    session = requests.Session()
    session.auth = (ORTHANC_USER, ORTHANC_PASS)
    conn = psycopg2.connect(**DB_CONFIG)

    if args.patient:
        studies = study_uids_for_patient(conn, args.patient)
    else:
        studies = [s.strip() for s in args.studies.split(",") if s.strip()]
    interval = indexer_interval()
    min_wait = interval + 15  # guarantee >=1 indexer scan happened while files were present
    print(f"Target studies: {len(studies)}  evict-after={args.evict}  "
          f"indexer_interval={interval}s")

    ok = warmed = 0
    for i, uid in enumerate(studies, 1):
        print(f"\n[{i}/{len(studies)}] {uid}")
        s0, r0 = study_readability(session, ORTHANC_URL, uid)
        print(f"  before: series={s0} readable={r0}")
        try:
            res = warm_study(uid)
            warmed += 1
            print(f"  warm_study -> {res}")
        except Exception as exc:  # noqa: BLE001
            print(f"  WARN warm failed: {exc}", file=sys.stderr)
            continue

        # Wait at least one indexer interval (so a scan runs while files are on
        # disk), then poll until every series is readable or the deadline passes.
        start = time.time()
        n_series = readable = 0
        while time.time() - start < args.index_wait:
            time.sleep(15)
            if time.time() - start < min_wait:
                continue
            n_series, readable = study_readability(session, ORTHANC_URL, uid)
            if n_series and readable == n_series:
                break
        fully = n_series > 0 and readable == n_series
        print(f"  after: series={n_series} readable={readable}  -> "
              f"{'INDEXED' if fully else 'INCOMPLETE'}")
        if fully:
            ok += 1

        if args.evict:
            if fully:
                try:
                    ev = evict_study(uid)
                    print(f"  evict_study -> {ev}")
                except Exception as exc:  # noqa: BLE001
                    print(f"  WARN evict failed: {exc}", file=sys.stderr)
            else:
                print("  SKIP evict (indexing not fully verified — left warm for inspection)")

    conn.close()
    print(f"\nDone. warmed={warmed}/{len(studies)} indexed_ok={ok}/{len(studies)}")
    print("Next: run prune_stale_index_paths.py --execute --delete-orphans to clear "
          "any stale/ghost index rows the old locations left behind.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
