#!/usr/bin/env python3
"""Rebuild series_cache_state from disk: mark each series hot/cold based on
whether its DICOM directory actually exists and has files on disk.

Cold storage is keyed by the series (one ``series_cache_state`` row per series);
study/patient status is derived by aggregating these rows. This script is the
natural reconcile — disk presence is ground truth.

Dry-run by default (prints what would change); ``--execute`` rewrites
``series_cache_state``.

Run with:
    conda activate ssc-pacs
    cd stanford-stroke-pacs
    python scripts/cold_storage/rebuild_cache_state.py [--execute]
"""
import argparse
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

from dotenv import load_dotenv

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
load_dotenv(REPO_ROOT / ".env")

sys.path.insert(0, str(REPO_ROOT / "web-app"))
from db import get_conn  # noqa: E402


def is_warm(path: str | None) -> bool:
    if not path:
        return False
    try:
        return os.path.isdir(path) and any(True for _ in os.scandir(path))
    except PermissionError:
        return False


def ts():
    return datetime.now().strftime("%H:%M:%S")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--execute", action="store_true",
                        help="Write the changes. Default is a dry-run report.")
    args = parser.parse_args()

    t_start = time.monotonic()
    print(f"[{ts()}] connecting to DB...")

    conn = get_conn()
    cur = conn.cursor()

    print(f"[{ts()}] querying series...")
    cur.execute("""
        SELECT seriesinstanceuid, dicom_dir_path,
               COALESCE(dicom_archive_path, '') <> '' AS has_archive
        FROM   image_series
        WHERE  dicom_dir_path IS NOT NULL
    """)
    rows = cur.fetchall()
    print(f"[{ts()}] {len(rows)} series to check  ({time.monotonic()-t_start:.1f}s)")

    print(f"[{ts()}] loading current series_cache_state...")
    cur.execute("SELECT seriesinstanceuid, status FROM series_cache_state")
    current = {r[0]: r[1] for r in cur.fetchall()}
    print(f"[{ts()}] {len(current)} series_cache_state rows loaded  ({time.monotonic()-t_start:.1f}s)")

    now = datetime.now(timezone.utc)
    to_hot = []
    to_cold = []

    print(f"[{ts()}] checking disk state (one dot = 500 series)...")
    t_disk = time.monotonic()
    skipped_no_archive = 0
    for i, (suid, path, has_archive) in enumerate(rows, 1):
        warm = is_warm(path)
        was = current.get(suid, "missing")
        if warm and was != "hot":
            # Never mark an archive-less series hot: a hot row makes the
            # loose dir TTL-evictable, and evict_series deletes it without
            # checking the archive — that loose dir may be the only copy.
            if has_archive:
                to_hot.append((suid, path))
            else:
                skipped_no_archive += 1
        elif not warm and was == "hot":
            to_cold.append(suid)

        if i % 500 == 0:
            elapsed = time.monotonic() - t_disk
            rate = i / elapsed
            eta = (len(rows) - i) / rate if rate > 0 else 0
            print(f"  [{ts()}] {i}/{len(rows)}  hot={len(to_hot)}  cold={len(to_cold)}"
                  f"  {rate:.0f} series/s  ETA {eta:.0f}s", flush=True)

    t_disk_done = time.monotonic()
    print(f"[{ts()}] disk check done in {t_disk_done - t_disk:.1f}s")
    print()
    print(f"  Series checked:      {len(rows)}")
    print(f"  → would mark hot:    {len(to_hot)}")
    print(f"  → would mark cold:   {len(to_cold)}")
    print(f"  → warm but archive-less (left as-is): {skipped_no_archive}")
    print(f"  → already correct:   {len(rows) - len(to_hot) - len(to_cold) - skipped_no_archive}")

    if not args.execute:
        print("\nDry-run (default): no changes written. Re-run with --execute to apply.")
        return

    if to_hot:
        print(f"\n[{ts()}] writing {len(to_hot)} hot rows...")
        # last_accessed_at must be set: run_eviction's TTL sweep skips rows
        # where it is NULL, so hot rows without it are never evicted.
        cur.executemany("""
            INSERT INTO series_cache_state (seriesinstanceuid, status, warmed_at, last_accessed_at, cache_path)
            VALUES (%s, 'hot', %s, %s, %s)
            ON CONFLICT (seriesinstanceuid) DO UPDATE
                SET status           = 'hot',
                    warmed_at        = EXCLUDED.warmed_at,
                    last_accessed_at = EXCLUDED.last_accessed_at,
                    cache_path       = EXCLUDED.cache_path
        """, [(suid, now, now, path) for suid, path in to_hot])
        print(f"[{ts()}] hot rows written  ({time.monotonic()-t_start:.1f}s)")

    if to_cold:
        print(f"[{ts()}] writing {len(to_cold)} cold rows...")
        cur.execute("""
            UPDATE series_cache_state
            SET    status = 'cold', warmed_at = NULL, cache_path = NULL
            WHERE  seriesinstanceuid = ANY(%s)
        """, (to_cold,))
        print(f"[{ts()}] cold rows written  ({time.monotonic()-t_start:.1f}s)")

    conn.commit()
    print(f"\n[{ts()}] Done in {time.monotonic()-t_start:.1f}s total."
          f"  Marked {len(to_hot)} hot, {len(to_cold)} cold.")


if __name__ == "__main__":
    main()
