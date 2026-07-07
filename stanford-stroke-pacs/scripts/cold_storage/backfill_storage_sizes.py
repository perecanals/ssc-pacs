#!/usr/bin/env python3
"""Backfill compressed/decompressed storage sizes (MB) for every series + study.

Populates ``image_series.compressed_size_mb`` / ``decompressed_size_mb`` and the
study-level rollups on ``image_study`` (creating the columns if absent). Sizes
are decimal MB (bytes / 1e6).

Per series:
- compressed_size_mb   = file size of ``dicom_archive_path`` (NULL if no archive)
- decompressed_size_mb = sum of DICOM file content bytes. Cheap path: if the
  loose ``dicom_dir_path`` is populated on disk, sum its file sizes. Otherwise
  stream-decompress the tar.zst and sum tar member sizes (identical metric —
  member.size is content bytes, no tar padding). Our archives are written with
  zstd stream_writer, so the frame header does NOT carry the content size;
  streaming is the only way to measure an archive.

Resumable: rows with both sizes already set are skipped (use --recompute to
redo). The study rollup runs at the end over ALL studies whose series all have
sizes. Expect hours on a full backfill — the decompression stream reads the
whole archive tree.

Usage
-----
    python scripts/cold_storage/backfill_storage_sizes.py            # everything missing
    python scripts/cold_storage/backfill_storage_sizes.py --workers 4
    python scripts/cold_storage/backfill_storage_sizes.py --label sir_batch1
    python scripts/cold_storage/backfill_storage_sizes.py --recompute --limit 100
"""

from __future__ import annotations

import argparse
import concurrent.futures as cf
import os
import sys
import tarfile
import threading
import time
from pathlib import Path

import psycopg2
import zstandard as zstd
from dotenv import load_dotenv

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
load_dotenv(REPO_ROOT / ".env")
sys.path.insert(0, str(REPO_ROOT / "web-app"))

from db import DB_CONFIG  # noqa: E402

SERIES_COLUMNS_SQL = (
    "ALTER TABLE image_series ADD COLUMN IF NOT EXISTS compressed_size_mb double precision",
    "ALTER TABLE image_series ADD COLUMN IF NOT EXISTS decompressed_size_mb double precision",
)
STUDY_COLUMNS_SQL = (
    "ALTER TABLE image_study ADD COLUMN IF NOT EXISTS compressed_size_mb double precision",
    "ALTER TABLE image_study ADD COLUMN IF NOT EXISTS decompressed_size_mb double precision",
)

# Study rollup: only stamp studies whose every series has sizes, so a partial
# backfill never publishes a misleading undercount.
STUDY_ROLLUP_SQL = """
UPDATE image_study st
SET compressed_size_mb = agg.c, decompressed_size_mb = agg.d
FROM (
    SELECT studyinstanceuid,
           ROUND(SUM(compressed_size_mb)::numeric, 3)::double precision   AS c,
           ROUND(SUM(decompressed_size_mb)::numeric, 3)::double precision AS d
    FROM image_series
    GROUP BY studyinstanceuid
    HAVING COUNT(*) = COUNT(compressed_size_mb)
       AND COUNT(*) = COUNT(decompressed_size_mb)
) agg
WHERE st.studyinstanceuid = agg.studyinstanceuid
"""


def dir_content_bytes(dirpath: str) -> int:
    total = 0
    for root, _dirs, files in os.walk(dirpath):
        for f in files:
            try:
                total += os.path.getsize(os.path.join(root, f))
            except OSError:
                pass
    return total


def archive_content_bytes(archive_path: str) -> int:
    """Stream-decompress a tar.zst and sum member content sizes."""
    dctx = zstd.ZstdDecompressor()
    total = 0
    with open(archive_path, "rb") as f_in:
        with dctx.stream_reader(f_in) as z_in:
            with tarfile.open(fileobj=z_in, mode="r|") as tf:
                for member in tf:
                    if member.isfile():
                        total += member.size
    return total


def measure_series(row: tuple) -> tuple:
    """(suid, dicom_dir, archive) -> (suid, comp_mb|None, decomp_mb|None, err|None)."""
    suid, ddir, archive = row
    comp_mb = decomp_mb = None
    err = None
    try:
        if archive and os.path.isfile(archive):
            comp_mb = round(os.path.getsize(archive) / 1e6, 3)
        if ddir and os.path.isdir(ddir):
            n = dir_content_bytes(ddir)
            if n > 0:
                decomp_mb = round(n / 1e6, 3)
        if decomp_mb is None and archive and os.path.isfile(archive):
            decomp_mb = round(archive_content_bytes(archive) / 1e6, 3)
    except Exception as exc:  # keep going; report at the end
        err = f"{type(exc).__name__}: {exc}"
    return suid, comp_mb, decomp_mb, err


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--label", default=None, help="Restrict to one import_label.")
    ap.add_argument("--limit", type=int, default=None, help="Cap number of series (pilot).")
    ap.add_argument("--workers", type=int, default=3,
                    help="Parallel measurement workers (disk-bound; keep modest).")
    ap.add_argument("--recompute", action="store_true",
                    help="Re-measure rows that already have sizes.")
    ap.add_argument("--commit-every", type=int, default=200)
    args = ap.parse_args()

    conn = psycopg2.connect(**DB_CONFIG)
    conn.autocommit = False
    cur = conn.cursor()
    for stmt in SERIES_COLUMNS_SQL + STUDY_COLUMNS_SQL:
        cur.execute(stmt)
    conn.commit()

    where = ["dicom_dir_path IS NOT NULL OR dicom_archive_path IS NOT NULL"]
    params: list = []
    if not args.recompute:
        where.append("(compressed_size_mb IS NULL OR decompressed_size_mb IS NULL)")
    if args.label:
        where.append("import_label = %s")
        params.append(args.label)
    sql = ("SELECT seriesinstanceuid, dicom_dir_path, dicom_archive_path "
           "FROM image_series WHERE (" + ") AND (".join(where) + ")")
    if args.limit:
        sql += f" LIMIT {int(args.limit)}"
    cur.execute(sql, params)
    todo = cur.fetchall()
    print(f"Series to measure: {len(todo)}", flush=True)

    done = 0
    errors: list[tuple[str, str]] = []
    pending: list[tuple] = []
    lock = threading.Lock()
    t0 = time.time()

    def flush_pending():
        if not pending:
            return
        cur.executemany(
            "UPDATE image_series SET compressed_size_mb = %s, decompressed_size_mb = %s "
            "WHERE seriesinstanceuid = %s", pending)
        conn.commit()
        pending.clear()

    with cf.ThreadPoolExecutor(max_workers=args.workers) as ex:
        for suid, comp, decomp, err in ex.map(measure_series, todo):
            with lock:
                done += 1
                if err:
                    errors.append((suid, err))
                else:
                    pending.append((comp, decomp, suid))
                    if len(pending) >= args.commit_every:
                        flush_pending()
                if done % 500 == 0:
                    rate = done / max(time.time() - t0, 1e-9)
                    eta_min = (len(todo) - done) / max(rate, 1e-9) / 60
                    print(f"  {done}/{len(todo)}  ({rate:.1f}/s, ETA {eta_min:.0f} min)",
                          flush=True)
    flush_pending()

    print("Rolling up study totals…", flush=True)
    cur.execute(STUDY_ROLLUP_SQL)
    print(f"  studies updated: {cur.rowcount}", flush=True)
    conn.commit()

    cur.execute("SELECT COUNT(*) FROM image_series WHERE decompressed_size_mb IS NULL")
    remaining = cur.fetchone()[0]
    cur.execute("SELECT COUNT(*) FROM image_series WHERE decompressed_size_mb > 1000")
    over_1gb = cur.fetchone()[0]
    conn.close()

    print(f"\nDone. measured={done - len(errors)}  errors={len(errors)}  "
          f"series still without decompressed size: {remaining}")
    print(f"Series over 1 GB decompressed: {over_1gb}")
    if errors:
        print("Errors (first 20):")
        for suid, err in errors[:20]:
            print(f"  {suid}: {err}")
    return 0 if not errors else 3


if __name__ == "__main__":
    raise SystemExit(main())
