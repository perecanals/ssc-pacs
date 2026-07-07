#!/usr/bin/env python3
"""
Force the Folder Indexer to rebuild specific series from their warmed on-disk
files, by purging the series' rows from indexer-plugin.db and restarting Orthanc.

Why this is needed
------------------
A series can get "stuck" when Orthanc's index points at a wrong/old directory for
it (e.g. a re-export under a differently-cased folder) and the real files were
later restored at the canonical `dicom_dir_path`. The indexer won't reconcile on
its own: the stale `Files` rows make it treat the canonical files as
already-stored (or SOPInstanceUID dedup attaches them to dead instances), so the
series shows 0 readable instances even though its files are present.

`prune_stale_index_paths.py` removes *duplicate* path rows (instance keeps a valid
path) and REST-deletes pure orphans. This tool covers the remaining case: drop
*all* index rows for a series so the next indexer scan re-registers it cleanly
from the files now on disk.

Prerequisite: the series' canonical files must be ON DISK (warm). Run
`warm_reindex.py` (or warm the study) first; this tool refuses series whose
`dicom_dir_path` has no files.

Usage:
  # Explicit series (verify mode by default)
  python scripts/cold_storage/reindex_series.py --series <suid1,suid2>
  python scripts/cold_storage/reindex_series.py --series <suid1,suid2> --execute --yes

  # Auto-detect a patient's stuck-but-warm series
  python scripts/cold_storage/reindex_series.py --patient 22-013 --execute --yes
"""

from __future__ import annotations

import argparse
import importlib.util
import os
import shutil
import sqlite3
import sys
import time
from pathlib import Path

import psycopg2
import psycopg2.extras
import requests
from dotenv import load_dotenv

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
load_dotenv(REPO_ROOT / ".env")
sys.path.insert(0, str(REPO_ROOT / "web-app"))

# Reuse the index-DB / docker helpers from the pruner (no runtime env needed to import).
_spec = importlib.util.spec_from_file_location(
    "prune_stale_index_paths", Path(__file__).resolve().parent / "prune_stale_index_paths.py"
)
_prune = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_prune)
_docker = _prune._docker
snapshot_index_db = _prune.snapshot_index_db
INDEX_DB_CONTAINER_PATH = _prune.INDEX_DB_CONTAINER_PATH
BACKUPS_DIR = _prune.BACKUPS_DIR


def db_series(conn, patient: str | None, suids: list[str] | None):
    """Return [(suid, dicom_dir_path)] for the requested patient or series."""
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        if suids:
            cur.execute(
                "SELECT seriesinstanceuid, dicom_dir_path FROM image_series "
                "WHERE seriesinstanceuid = ANY(%s)", (suids,))
        else:
            cur.execute(
                "SELECT seriesinstanceuid, dicom_dir_path FROM image_series "
                "WHERE patient_id = %s AND dicom_dir_path IS NOT NULL", (patient,))
        return [(r["seriesinstanceuid"], r["dicom_dir_path"]) for r in cur.fetchall()]


def series_first_readable(session, url, suid: str) -> tuple[bool, int]:
    """(is_served, readable_instance_count) for a series via Orthanc."""
    r = session.post(f"{url}/tools/find",
                     json={"Level": "Series", "Query": {"SeriesInstanceUID": suid},
                           "Expand": True}, timeout=60)
    r.raise_for_status()
    found = r.json()
    if not found:
        return (False, 0)
    insts = found[0].get("Instances", [])
    readable = sum(1 for i in insts
                   if session.get(f"{url}/instances/{i}/file", timeout=20).status_code == 200)
    return (True, readable)


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--series", help="Comma-separated SeriesInstanceUIDs to rebuild")
    g.add_argument("--patient", help="Auto-detect this patient's stuck-but-warm series")
    ap.add_argument("--execute", action="store_true", help="Apply (default: report)")
    ap.add_argument("--container", default="ssc-orthanc")
    ap.add_argument("--index-wait", type=int, default=180)
    ap.add_argument("--yes", action="store_true", help="Skip the stop-Orthanc prompt")
    args = ap.parse_args()

    from config import STORAGE_MODE  # noqa: E402
    from db import DB_CONFIG  # noqa: E402
    from orthanc_client import ORTHANC_PASS, ORTHANC_URL, ORTHANC_USER  # noqa: E402

    if STORAGE_MODE != "cold_path_cache":
        print(f"STORAGE_MODE is '{STORAGE_MODE}', not cold_path_cache. Aborting.", file=sys.stderr)
        return 2

    # Convert termination signals into an exception so the `finally` that restarts
    # Orthanc always runs — a killed run must never leave the container stopped.
    import signal

    def _to_exc(signum, _frame):
        raise KeyboardInterrupt(f"received signal {signum}")
    for _sig in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
        try:
            signal.signal(_sig, _to_exc)
        except (ValueError, OSError):
            pass

    session = requests.Session()
    session.auth = (ORTHANC_USER, ORTHANC_PASS)
    conn = psycopg2.connect(**DB_CONFIG)

    suids = [s.strip() for s in args.series.split(",")] if args.series else None
    candidates = db_series(conn, args.patient, suids)

    # A series needs a rebuild when its files are on disk but Orthanc does not
    # serve it cleanly: not served at all, or some instances won't read (stale /
    # wrong-path rows). NOTE: do NOT compare Orthanc's instance count to the raw
    # on-disk file count — a single physical dir can legitimately hold files from
    # more than one SeriesInstanceUID (mixed dirs), so file-count != instance-count
    # is normal and is NOT a fault. Readability is the reliable signal.
    targets = []
    for suid, ddir in candidates:
        if not ddir:
            continue
        on_disk = os.path.isdir(ddir) and any(os.scandir(ddir))
        served, readable = series_first_readable(session, ORTHANC_URL, suid)
        total = 0
        if served:
            r = session.post(f"{ORTHANC_URL}/tools/find",
                             json={"Level": "Series", "Query": {"SeriesInstanceUID": suid},
                                   "Expand": True}, timeout=60).json()
            total = len(r[0]["Instances"]) if r else 0
        healthy = served and total > 0 and readable == total
        stuck = on_disk and not healthy
        # Explicitly-named series are FORCE-rebuilt even when they look healthy.
        # Rationale: after a mixed-dir de-mix, the moved secondary's stale index
        # rows live UNDER THE HOST DIR PATH (they carry the host UID, not the
        # secondary's), so the host must be purged to clear them — yet the host
        # reads healthy (its own files are intact) and auto-detection would skip
        # it. A forced rebuild of an already-healthy series is idempotent: purge
        # its Files rows, rescan, re-verify readable==total. Refuses no-files
        # series (on_disk gate) in either mode.
        forced = bool(args.series) and on_disk and not stuck
        flag = "FORCE" if forced else ("REBUILD" if stuck else ("ok" if healthy else "skip"))
        if args.series or stuck:
            print(f"  {flag:8s} {suid}  on_disk={on_disk} served={served} "
                  f"readable={readable}/{total}")
        if stuck or forced:
            targets.append((suid, ddir))

    if not targets:
        print("\nNo stuck-but-warm series to rebuild.")
        return 0
    print(f"\n{len(targets)} series to rebuild.")

    if not args.execute:
        print("REPORT ONLY — re-run with --execute to apply.")
        return 0

    if not args.yes:
        print(f"\nThis STOPS Orthanc '{args.container}' briefly, purges these series' "
              f"index rows, and restarts so the indexer rebuilds them from disk.")
        if input("Type 'yes' to proceed: ").strip().lower() != "yes":
            print("Aborted.")
            return 0

    # Build the set of index paths to delete: every Files row whose path contains
    # a target series UID (covers wrong-dir and orphaned rows in any location).
    import tempfile
    tmp = Path(tempfile.mkdtemp(prefix="reindex-series-"))
    stopped = False
    try:
        try:
            print(f"Stopping {args.container} ...")
            _docker("stop", args.container)
            stopped = True
            BACKUPS_DIR.mkdir(parents=True, exist_ok=True)
            ts = time.strftime("%Y-%m-%dT%H%M%S")
            backup = BACKUPS_DIR / f"indexer-plugin.{ts}.db"
            snapshot_index_db(args.container, backup)
            working = tmp / "working.db"
            shutil.copy2(backup, working)
            db = sqlite3.connect(str(working))
            deleted = 0
            for suid, _ in targets:
                cur = db.execute("DELETE FROM Files WHERE path LIKE ?", (f"%/{suid}/%",))
                deleted += cur.rowcount
            db.commit()
            db.close()
            print(f"Purged {deleted} index row(s) across {len(targets)} series "
                  f"(backup: {backup})")
            _docker("cp", str(working), f"{args.container}:{INDEX_DB_CONTAINER_PATH}")
        finally:
            if stopped:
                print(f"Starting {args.container} ...")
                _docker("start", args.container, check=False)

        # Wait for the indexer to rebuild, then verify each series is served and
        # fully readable (every instance reads). See the detection note above re:
        # not using raw file counts (mixed dirs).
        time.sleep(75)  # >= one indexer scan interval (Interval=60s) + margin
        ok = 0
        deadline = time.time() + args.index_wait
        while True:
            ok = 0
            for suid, _ in targets:
                served, readable = series_first_readable(session, ORTHANC_URL, suid)
                r = session.post(f"{ORTHANC_URL}/tools/find",
                                 json={"Level": "Series", "Query": {"SeriesInstanceUID": suid},
                                       "Expand": True}, timeout=60).json()
                total = len(r[0]["Instances"]) if r else 0
                if served and total and readable == total:
                    ok += 1
            if ok == len(targets) or time.time() > deadline:
                break
            time.sleep(15)
        print(f"\nRebuilt {ok}/{len(targets)} series served + fully readable.")
        return 0 if ok == len(targets) else 3
    finally:
        conn.close()
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
