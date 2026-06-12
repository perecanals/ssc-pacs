#!/usr/bin/env python3
"""
Read-only audit: compare PostgreSQL image_series.dicom_dir_path to the on-disk tree.

- Counts rows / paths in SQL
- Samples paths and checks Path(...).is_dir() on the host running this script
- Summarizes path depth and prefix patterns in SQL vs a shallow walk of --fs-root

Does not query Orthanc. Requires DB_* in .env (same as other scripts).

Example:
  python3 scripts/dicom_path_sql_fs_audit.py
  python3 scripts/dicom_path_sql_fs_audit.py --sample-rows 10000 --random
  python3 scripts/dicom_path_sql_fs_audit.py --fs-root /DATA2/pacs_imaging_data
"""

from __future__ import annotations

import argparse
import random
import sys
from collections import Counter
from pathlib import Path

import psycopg2
from dotenv import load_dotenv

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
load_dotenv(REPO_ROOT / ".env")

sys.path.insert(0, str(REPO_ROOT / "web-app"))
from config import DICOM_DATA_ROOT  # noqa: E402
from db import DB_CONFIG  # noqa: E402


def path_parts(p: str) -> list[str]:
    return [x for x in p.split("/") if x]


def path_depth(p: str) -> int:
    return len(path_parts(p))


def sql_counts(cur) -> dict[str, int]:
    cur.execute("SELECT COUNT(*) FROM image_series")
    total = cur.fetchone()[0]
    cur.execute(
        "SELECT COUNT(*) FROM image_series WHERE dicom_dir_path IS NOT NULL "
        "AND trim(dicom_dir_path) <> ''"
    )
    with_path = cur.fetchone()[0]
    cur.execute(
        "SELECT COUNT(DISTINCT dicom_dir_path) FROM image_series WHERE dicom_dir_path IS NOT NULL "
        "AND trim(dicom_dir_path) <> ''"
    )
    distinct_paths = cur.fetchone()[0]
    cur.execute("SELECT COUNT(DISTINCT seriesinstanceuid) FROM image_series")
    distinct_series = cur.fetchone()[0]
    return {
        "total_rows": total,
        "rows_with_path": with_path,
        "distinct_paths": distinct_paths,
        "distinct_seriesinstanceuid": distinct_series,
    }


def sql_prefix_histogram(cur, limit: int = 50) -> list[tuple[str, int]]:
    """First two path segments after leading / (e.g. DATA2, pacs_imaging_data)."""
    cur.execute(
        """
        SELECT
          CASE
            WHEN dicom_dir_path ~ '^/[^/]+/[^/]+' THEN substring(dicom_dir_path from '^(/[^/]+/[^/]+)')
            WHEN dicom_dir_path ~ '^/[^/]+' THEN substring(dicom_dir_path from '^(/[^/]+)')
            ELSE '(no leading slash or empty)'
          END AS prefix,
          COUNT(*) AS n
        FROM image_series
        WHERE dicom_dir_path IS NOT NULL AND trim(dicom_dir_path) <> ''
        GROUP BY 1
        ORDER BY n DESC
        LIMIT %s
        """,
        (limit,),
    )
    return [(str(r[0]), int(r[1])) for r in cur.fetchall()]


def fetch_sample_paths(cur, n: int, use_random: bool) -> list[str]:
    if use_random:
        cur.execute(
            "SELECT dicom_dir_path FROM image_series "
            "WHERE dicom_dir_path IS NOT NULL AND trim(dicom_dir_path) <> '' "
            "ORDER BY random() LIMIT %s",
            (n,),
        )
    else:
        cur.execute(
            "SELECT dicom_dir_path FROM image_series "
            "WHERE dicom_dir_path IS NOT NULL AND trim(dicom_dir_path) <> '' "
            "ORDER BY seriesinstanceuid LIMIT %s",
            (n,),
        )
    return [str(r[0]).strip() for r in cur.fetchall()]


def relative_under_root(path_str: str, root: Path) -> str | None:
    try:
        p = Path(path_str)
        if not p.is_absolute():
            return None
        rel = p.resolve().relative_to(root.resolve())
        return str(rel)
    except (ValueError, OSError):
        return None


def shallow_fs_report(root: Path, max_patients_sample: int) -> None:
    print(f"\n--- On-disk tree (--fs-root): {root} ---")
    if not root.exists():
        print("  Path does not exist on this host (nothing to compare to SQL).")
        return
    if not root.is_dir():
        print("  Exists but is not a directory.")
        return

    try:
        children = sorted(d for d in root.iterdir() if d.is_dir())
    except OSError as e:
        print(f"  Cannot list directory: {e}")
        return

    print(f"  Immediate subdirectories (e.g. patient folders): {len(children)}")
    if not children:
        return

    for d in children[:5]:
        print(f"    {d.name}/")
    if len(children) > 5:
        print(f"    ... and {len(children) - 5} more")

    sample_dirs = children[:max_patients_sample]
    if len(children) > max_patients_sample:
        sample_dirs = random.sample(children, max_patients_sample)

    print(f"\n  Deeper sample (up to {len(sample_dirs)} patient dir(s), depth capped):")
    for pd in sample_dirs:
        print(f"    {pd.name}/")
        try:
            subs = sorted(x for x in pd.iterdir() if x.is_dir())[:4]
            for s in subs:
                print(f"      {s.name}/")
                try:
                    subs2 = sorted(x for x in s.iterdir() if x.is_dir())[:3]
                    for t in subs2:
                        print(f"        {t.name}/")
                except OSError:
                    pass
        except OSError as e:
            print(f"      (list error: {e})")


def main() -> int:
    ap = argparse.ArgumentParser(
        description="Audit image_series.dicom_dir_path vs on-disk legacy PACS root (read-only)"
    )
    ap.add_argument(
        "--fs-root",
        type=Path,
        default=None,
        help="Filesystem root to compare against (default: config.toml dicom_data_root or env)",
    )
    ap.add_argument(
        "--sample-rows",
        type=int,
        default=5000,
        metavar="N",
        help="How many SQL rows to fetch for existence checks (default: 5000)",
    )
    ap.add_argument(
        "--random",
        action="store_true",
        help="ORDER BY random() for the sample (slower on huge tables; more representative)",
    )
    ap.add_argument(
        "--patient-sample",
        type=int,
        default=3,
        metavar="N",
        help="How many patient subdirs of --fs-root to show in the shallow tree sample (default: 3)",
    )
    args = ap.parse_args()

    fs_root = (args.fs_root or DICOM_DATA_ROOT).resolve()

    if not DB_CONFIG.get("user"):
        print("DB_USER not set in .env", file=sys.stderr)
        return 1

    print("dicom_path SQL ↔ filesystem audit (read-only)")
    print(f"  PostgreSQL: {DB_CONFIG['host']} db={DB_CONFIG['dbname']}")
    print(f"  FS root:    {fs_root} (from --fs-root or config.toml / DICOM_DATA_ROOT)")

    conn = psycopg2.connect(**DB_CONFIG)
    try:
        with conn.cursor() as cur:
            counts = sql_counts(cur)
            print("\n--- image_series (SQL) ---")
            print(f"  Total rows:                  {counts['total_rows']}")
            print(f"  Rows with dicom_dir_path:    {counts['rows_with_path']}")
            print(f"  DISTINCT dicom_dir_path:     {counts['distinct_paths']}")
            print(
                f"  DISTINCT seriesinstanceuid:  {counts.get('distinct_seriesinstanceuid', '?')}"
            )

            print("\n  Top path prefixes (first 1–2 segments after /):")
            for pref, n in sql_prefix_histogram(cur, 30):
                print(f"    {n:8d}  {pref}")

            n = max(1, min(args.sample_rows, 500_000))
            paths = fetch_sample_paths(cur, n, args.random)
            print(f"\n--- Sample existence check ({len(paths)} path(s), this host) ---")

            exist = missing = 0
            missing_examples: list[str] = []
            depth_existing: Counter[int] = Counter()
            depth_missing: Counter[int] = Counter()
            rel_depth_existing: Counter[int] = Counter()

            for s in paths:
                p = Path(s)
                if p.is_dir():
                    exist += 1
                    depth_existing[path_depth(s)] += 1
                    rel = relative_under_root(s, fs_root)
                    if rel is not None:
                        rel_depth_existing[len(path_parts(rel))] += 1
                else:
                    missing += 1
                    depth_missing[path_depth(s)] += 1
                    if len(missing_examples) < 8:
                        missing_examples.append(s)

            total_s = exist + missing
            pct = 100.0 * exist / total_s if total_s else 0.0
            print(f"  is_dir() true:  {exist} ({pct:.1f}%)")
            print(f"  is_dir() false: {missing} ({100.0 - pct:.1f}%)")

            if depth_existing:
                print("  Depth (path components) when EXISTS:", dict(sorted(depth_existing.items())))
            if depth_missing:
                print("  Depth when MISSING:", dict(sorted(depth_missing.items())))
            if rel_depth_existing:
                print(
                    "  Depth relative to --fs-root when EXISTS (how deep under legacy root):",
                    dict(sorted(rel_depth_existing.items())),
                )

            if missing_examples:
                print("\n  Example paths that are NOT directories on this host:")
                for ex in missing_examples:
                    print(f"    {ex}")

            # Show a few SQL paths that exist, with relative form
            shown = 0
            print("\n  Example paths that ARE directories (with relative path under fs-root):")
            for s in paths:
                if not Path(s).is_dir():
                    continue
                rel = relative_under_root(s, fs_root)
                if rel is not None:
                    print(f"    SQL: {s}")
                    print(f"         → under fs-root: {rel}")
                    shown += 1
                else:
                    print(f"    SQL: {s}")
                    print(f"         → (not under fs-root {fs_root}; different mount/prefix?)")
                    shown += 1
                if shown >= 5:
                    break
    finally:
        conn.close()

    shallow_fs_report(fs_root, args.patient_sample)

    print(
        "\n--- Interpretation ---\n"
        "  If most sample paths are MISSING here but you know data exists elsewhere, the SQL paths\n"
        "  may still be correct for the ingest host; run this script on the machine that mounts the archive.\n"
        "  If MISSING is high on the ingest host, check prefix histogram vs actual mount path (wrong drive,\n"
        "  renamed root, or dicom_dir_path updated inconsistently).\n"
        "  DISTINCT dicom_dir_path << rows_with_path is normal (many series can share one directory).\n"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
