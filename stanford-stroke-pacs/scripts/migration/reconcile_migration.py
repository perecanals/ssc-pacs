#!/usr/bin/env python3
"""Post-migration reconciliation for a cluster port (e.g. Linux -> macOS).

Read-only. Run this on the *target* host right after restoring the SQL
cluster, migrating the Orthanc index, and re-pointing the storage paths.
It verifies that the ported deployment is internally consistent *before*
you trust it:

    1. Storage config points at paths that exist on the new host.
    2. Orthanc is reachable and its index (orthanc_db) was restored
       (non-zero study/series counts).
    3. The Folder Indexer's SQLite state volume is present
       (``indexer-plugin.db`` inside the ``<project>_ssc-orthanc-storage``
       volume, which also holds the only copy of OHIF SR annotations) —
       without it Orthanc cannot serve a single instance.
    4. ``image_series`` host-path columns were re-pointed to the new host:
       no row still carries an old-host prefix, and the ``*.tar.zst``
       archives actually exist on disk.

This script never mutates either database or the filesystem. After it
passes, run the standard two-DB reconciliation for the full
index-vs-metadata diff (it also feeds the Prometheus gauges):

    python scripts/data_integrity/reconcile.py

See documentation/operations/cluster_migration.md for the full runbook.

Usage:
    python scripts/migration/reconcile_migration.py
    python scripts/migration/reconcile_migration.py --limit 500   # sample
    python scripts/migration/reconcile_migration.py --skip-volume # no docker
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
from pathlib import Path

import requests

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(REPO_ROOT / "web-app"))

from config import (  # noqa: E402
    COLD_ARCHIVE_ROOT,
    DICOM_DATA_ROOT,
    STORAGE_MODE,
)
from db import DB_CONFIG, get_conn  # noqa: E402

ORTHANC_URL = os.getenv("ORTHANC_URL", "http://localhost:8042")
ORTHANC_USER = os.getenv("ORTHANC_ADMIN_USER", "")
ORTHANC_PASS = os.getenv("ORTHANC_ADMIN_PASSWORD", "")

GREEN, RED, YELLOW, CYAN, NC = "\033[0;32m", "\033[0;31m", "\033[1;33m", "\033[0;36m", "\033[0m"


class Reporter:
    """Collects pass/fail/warn lines and tracks whether any check failed."""

    def __init__(self) -> None:
        self.failed = False

    def ok(self, msg: str) -> None:
        print(f"  {GREEN}✔{NC} {msg}")

    def warn(self, msg: str) -> None:
        print(f"  {YELLOW}⚠{NC} {msg}")

    def fail(self, msg: str) -> None:
        print(f"  {RED}✘{NC} {msg}")
        self.failed = True

    def info(self, msg: str) -> None:
        print(f"  {CYAN}ℹ{NC} {msg}")


def _section(title: str) -> None:
    print(f"\n\033[1m{title}\033[0m")


# ---------------------------------------------------------------------------
# 1. Storage config / paths exist on the new host
# ---------------------------------------------------------------------------

def check_config(r: Reporter) -> None:
    _section("[1/4] Storage configuration")
    r.info(f"mode={STORAGE_MODE}")
    roots = {
        "dicom_data_root": DICOM_DATA_ROOT,
        "cold_archive_root": COLD_ARCHIVE_ROOT,
    }
    for name, path in roots.items():
        if path.is_dir():
            r.ok(f"{name} exists: {path}")
        else:
            # archive root is mandatory in cold mode; dicom_data_root is the
            # bind-mount source and may legitimately be empty but must exist.
            mandatory = name == "cold_archive_root" and STORAGE_MODE == "cold_path_cache"
            (r.fail if mandatory else r.warn)(f"{name} not found on disk: {path}")


# ---------------------------------------------------------------------------
# 2. Orthanc reachable + index restored
# ---------------------------------------------------------------------------

def check_orthanc(r: Reporter) -> None:
    _section("[2/4] Orthanc index (orthanc_db restored?)")
    if not ORTHANC_USER or not ORTHANC_PASS:
        r.fail("ORTHANC_ADMIN_USER / ORTHANC_ADMIN_PASSWORD not set in .env")
        return
    try:
        resp = requests.get(
            f"{ORTHANC_URL}/statistics", auth=(ORTHANC_USER, ORTHANC_PASS), timeout=10
        )
        resp.raise_for_status()
    except Exception as exc:  # noqa: BLE001
        r.fail(f"Orthanc unreachable at {ORTHANC_URL}: {exc}")
        return
    s = resp.json()
    studies = s.get("CountStudies", 0)
    series = s.get("CountSeries", 0)
    r.info(
        f"patients={s.get('CountPatients', '?')} studies={studies} "
        f"series={series} instances={s.get('CountInstances', '?')}"
    )
    if studies and series:
        r.ok("Orthanc index is populated (restore succeeded)")
    else:
        r.fail("Orthanc index is empty — orthanc_db not restored, or wrong DB")


# ---------------------------------------------------------------------------
# 3. Folder Indexer SQLite state volume present
# ---------------------------------------------------------------------------

def check_indexer_volume(r: Reporter, volume: str) -> None:
    _section("[3/4] Folder Indexer state volume")
    try:
        proc = subprocess.run(
            ["docker", "run", "--rm", "-v", f"{volume}:/v", "alpine",
             "test", "-f", "/v/indexer-plugin.db"],
            capture_output=True, timeout=120,
        )
    except FileNotFoundError:
        r.warn("docker not found on PATH — skipping (verify the volume manually)")
        return
    except Exception as exc:  # noqa: BLE001
        r.warn(f"could not inspect volume {volume}: {exc} — verify manually")
        return
    if proc.returncode == 0:
        r.ok(f"indexer-plugin.db present in volume '{volume}'")
    else:
        r.fail(
            f"indexer-plugin.db MISSING from volume '{volume}'. Orthanc cannot "
            "read instances until it is restored (or the cache is re-scanned)."
        )


# ---------------------------------------------------------------------------
# 4. image_series host paths re-pointed to the new host
# ---------------------------------------------------------------------------

def check_paths(r: Reporter, limit: int) -> None:
    _section("[4/4] host paths re-pointed")
    expected_prefixes = tuple(str(p) for p in (DICOM_DATA_ROOT, COLD_ARCHIVE_ROOT))
    sql = "SELECT patient_id, seriesinstanceuid, dicom_dir_path, dicom_archive_path FROM image_series"
    if limit:
        sql += f" LIMIT {int(limit)}"

    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(sql)
            rows = cur.fetchall()
    finally:
        conn.close()

    total = len(rows)
    off_prefix = 0          # path not under any configured root -> old host
    archive_missing = 0     # dicom_archive_path recorded but file absent
    archive_null = 0
    sample_off: list[str] = []
    sample_missing: list[str] = []

    for _pid, _uid, dir_path, arch_path in rows:
        if dir_path and not str(dir_path).startswith(expected_prefixes):
            off_prefix += 1
            if len(sample_off) < 5:
                sample_off.append(str(dir_path))
        if arch_path:
            if not Path(arch_path).exists():
                archive_missing += 1
                if len(sample_missing) < 5:
                    sample_missing.append(str(arch_path))
        else:
            archive_null += 1

    r.info(f"rows examined: {total}")
    if off_prefix == 0:
        r.ok("all dicom_dir_path values are under a configured storage root")
    else:
        r.fail(
            f"{off_prefix} rows still point at an un-migrated host prefix "
            "(backfill dicom_dir_path / dicom_archive_path)"
        )
        for p in sample_off:
            r.info(f"    e.g. {p}")

    if STORAGE_MODE == "cold_path_cache":
        if archive_null:
            r.warn(f"{archive_null} rows have NULL dicom_archive_path (not archived)")
        if archive_missing == 0:
            r.ok("every recorded dicom_archive_path exists on disk")
        else:
            r.fail(f"{archive_missing} archives recorded in DB are missing on disk")
            for p in sample_missing:
                r.info(f"    e.g. {p}")

    # Other host-path columns a port must also re-point. Backfilling only
    # image_series.dicom_dir_path leaves these on the old prefix — silent until
    # warm (study_path -> cache_path), NIfTI generation, or a labelled export
    # reads them. Each entry is existence-guarded so the check stays portable.
    extra_cols = [
        ("image_study", "study_path"),
        ("image_series", "nifti_path"),
        ("cache_state", "cache_path"),
        ("image_series_labelled", "dicom_dir_path"),
        ("image_series_labelled", "nifti_path"),
        ("image_series_labelled", "dicom_archive_path"),
        ("image_study_labelled", "study_path"),
    ]
    off_extra: list[tuple[str, str, int]] = []
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            for tbl, col in extra_cols:
                cur.execute(
                    "SELECT 1 FROM information_schema.columns WHERE table_schema='public' "
                    "AND table_name=%s AND column_name=%s",
                    (tbl, col),
                )
                if not cur.fetchone():
                    continue  # column absent in this deployment
                cond = " AND ".join(f"{col} NOT LIKE %s" for _ in expected_prefixes)
                cur.execute(
                    # Empty string == "no path recorded" (e.g. nifti_path for series
                    # with no NIfTI); treat it like NULL, not an un-migrated prefix.
                    f"SELECT count(*) FROM {tbl} WHERE {col} IS NOT NULL AND {col} <> '' AND {cond}",  # noqa: S608 (cols are a fixed allow-list)
                    tuple(f"{p}%" for p in expected_prefixes),
                )
                n = cur.fetchone()[0]
                if n:
                    off_extra.append((tbl, col, n))
    finally:
        conn.close()

    if not off_extra:
        r.ok("study_path / nifti_path / *_labelled paths all under a configured root")
    else:
        for tbl, col, n in off_extra:
            r.fail(f"{n} rows in {tbl}.{col} still point at an un-migrated host prefix")


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("--limit", type=int, default=0, help="Examine only N image_series rows (0 = all)")
    ap.add_argument("--volume", default="stanford-stroke-pacs_ssc-orthanc-storage",
                    help="Orthanc storage Docker volume name (Compose prefixes the "
                         "docker-compose.yml key with the project dir name; override "
                         "if your project name differs)")
    ap.add_argument("--skip-orthanc", action="store_true", help="Skip the Orthanc reachability check")
    ap.add_argument("--skip-volume", action="store_true", help="Skip the indexer-volume check")
    args = ap.parse_args()

    if not DB_CONFIG.get("user"):
        print("DB_USER not set in .env", file=sys.stderr)
        return 1

    print("\033[1m=== Migration Reconciliation ===\033[0m")
    print(f"  DB:      {DB_CONFIG['dbname']} @ {DB_CONFIG['host']}:{DB_CONFIG['port']}")
    print(f"  Orthanc: {ORTHANC_URL}")

    r = Reporter()
    check_config(r)
    if not args.skip_orthanc:
        check_orthanc(r)
    if not args.skip_volume:
        check_indexer_volume(r, args.volume)
    check_paths(r, args.limit)

    print("\n\033[1m" + "-" * 40 + "\033[0m")
    if r.failed:
        print(f"  {RED}\033[1mMigration NOT consistent — resolve the failures above.{NC}")
        print(f"  {CYAN}Then re-run, and finish with: python scripts/data_integrity/reconcile.py{NC}")
        return 1
    print(f"  {GREEN}\033[1mMigration checks passed.{NC}")
    print(f"  {CYAN}Next: python scripts/data_integrity/reconcile.py  (full index-vs-metadata diff){NC}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
