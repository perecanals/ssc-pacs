#!/usr/bin/env python3
"""Rebuild cache_state from disk: mark studies hot/cold based on whether
their series DICOM directories actually exist and have files on disk.

Run with:
    conda activate ssc-pacs
    cd stanford-stroke-pacs
    python scripts/cold_storage/rebuild_cache_state.py [--dry-run]
"""
import os
import sys
import time
import argparse
from datetime import datetime, timezone

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "../../web-app"))
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
    parser.add_argument("--dry-run", action="store_true",
                        help="Print what would change without writing to DB")
    args = parser.parse_args()

    t_start = time.monotonic()
    print(f"[{ts()}] connecting to DB...")

    conn = get_conn()
    cur = conn.cursor()

    print(f"[{ts()}] querying studies...")
    cur.execute("""
        SELECT i.studyinstanceuid,
               array_agg(i.dicom_dir_path ORDER BY i.seriesinstanceuid) AS series_paths,
               st.study_path
        FROM   image_series  i
        JOIN   image_study   st USING (studyinstanceuid)
        WHERE  i.dicom_dir_path IS NOT NULL
        GROUP  BY i.studyinstanceuid, st.study_path
    """)
    rows = cur.fetchall()
    print(f"[{ts()}] {len(rows)} studies to check  ({time.monotonic()-t_start:.1f}s)")

    print(f"[{ts()}] loading current cache_state...")
    cur.execute("SELECT studyinstanceuid, status FROM cache_state")
    current = {r[0]: r[1] for r in cur.fetchall()}
    print(f"[{ts()}] {len(current)} cache_state rows loaded  ({time.monotonic()-t_start:.1f}s)")

    now = datetime.now(timezone.utc)
    to_hot = []
    to_cold = []

    print(f"[{ts()}] checking disk state (one dot = 50 studies)...")
    t_disk = time.monotonic()
    for i, (uid, paths, study_path) in enumerate(rows, 1):
        all_warm = paths and all(is_warm(p) for p in paths)
        was = current.get(uid, "missing")
        if all_warm and was != "hot":
            to_hot.append((uid, study_path))
        elif not all_warm and was == "hot":
            to_cold.append(uid)

        if i % 50 == 0:
            elapsed = time.monotonic() - t_disk
            rate = i / elapsed
            eta = (len(rows) - i) / rate if rate > 0 else 0
            print(f"  [{ts()}] {i}/{len(rows)}  hot={len(to_hot)}  cold={len(to_cold)}"
                  f"  {rate:.0f} studies/s  ETA {eta:.0f}s", flush=True)

    t_disk_done = time.monotonic()
    print(f"[{ts()}] disk check done in {t_disk_done - t_disk:.1f}s")
    print()
    print(f"  Studies checked:     {len(rows)}")
    print(f"  → would mark hot:    {len(to_hot)}")
    print(f"  → would mark cold:   {len(to_cold)}")
    print(f"  → already correct:   {len(rows) - len(to_hot) - len(to_cold)}")

    if args.dry_run:
        print("\n--dry-run: no changes written.")
        return

    if to_hot:
        print(f"\n[{ts()}] writing {len(to_hot)} hot rows...")
        cur.executemany("""
            INSERT INTO cache_state (studyinstanceuid, status, warmed_at, cache_path)
            VALUES (%s, 'hot', %s, %s)
            ON CONFLICT (studyinstanceuid) DO UPDATE
                SET status     = 'hot',
                    warmed_at  = EXCLUDED.warmed_at,
                    cache_path = EXCLUDED.cache_path
        """, [(uid, now, path) for uid, path in to_hot])
        print(f"[{ts()}] hot rows written  ({time.monotonic()-t_start:.1f}s)")

    if to_cold:
        print(f"[{ts()}] writing {len(to_cold)} cold rows...")
        cur.execute("""
            UPDATE cache_state
            SET    status = 'cold', warmed_at = NULL, cache_path = NULL
            WHERE  studyinstanceuid = ANY(%s)
        """, (to_cold,))
        print(f"[{ts()}] cold rows written  ({time.monotonic()-t_start:.1f}s)")

    conn.commit()
    print(f"\n[{ts()}] Done in {time.monotonic()-t_start:.1f}s total."
          f"  Marked {len(to_hot)} hot, {len(to_cold)} cold.")


if __name__ == "__main__":
    main()
