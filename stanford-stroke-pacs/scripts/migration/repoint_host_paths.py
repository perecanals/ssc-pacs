#!/usr/bin/env python3
"""Repoint host-path columns after a cluster port (e.g. macOS -> Linux).

When you restore the ``stanford-stroke`` dump onto a new host, every host-path
column still carries the *old* host's storage roots. The Web App reads these
paths natively (warm/evict, NIfTI generation, labelled exports), so they must be
rewritten to the new roots — keeping the path *tail* byte-identical so that
``<new root>/<tail>`` still maps to the same ``/dicom-data/<tail>`` the ported
Orthanc index expects.

This is the scripted, transactional form of the manual SQL in
``docs/operations/cluster_migration.md`` §3. It:

  * reads the **new** roots from ``config.toml`` (``dicom_data_root`` /
    ``cold_archive_root``) — never guessed;
  * auto-detects the **old** roots from the data (override with --old-loose /
    --old-archive);
  * rewrites only the leading prefix via ``new || substring(col from len(old)+1)``
    (prefix-safe: the tail is preserved exactly, not string-replaced);
  * runs every column in a **single transaction** — all-or-nothing;
  * resets ``series_cache_state`` to ``cold`` (host-specific runtime state that
    must not carry over — a genuinely-warm series re-detects its files on next
    access);
  * re-audits afterward that no column still holds an un-migrated prefix.

**Dry-run by default** — it prints the plan and rolls back. Pass ``--apply`` to
commit. Idempotent: re-running after a successful apply is a no-op.

After it applies, verify with the read-only checker:

    python scripts/migration/reconcile_migration.py

Usage:
    python scripts/migration/repoint_host_paths.py                 # dry-run (auto-detect)
    python scripts/migration/repoint_host_paths.py --apply         # commit
    python scripts/migration/repoint_host_paths.py \
        --old-loose /Volumes/Expansion/ssc-pacs-data/imaging_data \
        --old-archive /Volumes/Expansion/ssc-pacs-data/compressed --apply
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(REPO_ROOT / "web-app"))

from db import DB_CONFIG, get_conn  # noqa: E402

from config import (  # noqa: E402
    COLD_ARCHIVE_ROOT,
    DICOM_DATA_ROOT,
    STORAGE_MODE,
)

GREEN, RED, YELLOW, CYAN, BOLD, NC = (
    "\033[0;32m", "\033[0;31m", "\033[1;33m", "\033[0;36m", "\033[1m", "\033[0m"
)

# Host-path columns, grouped by which root they live under. Each is
# existence-guarded at runtime, so a deployment missing a table/column (e.g. an
# empty labelled mirror) is skipped rather than erroring.
LOOSE_COLS = [
    ("image_series", "dicom_dir_path"),
    ("image_series", "nifti_path"),
    ("image_study", "study_path"),
    ("series_cache_state", "cache_path"),
    ("image_series_labelled", "dicom_dir_path"),
    ("image_series_labelled", "nifti_path"),
    ("image_study_labelled", "study_path"),
]
ARCHIVE_COLS = [
    ("image_series", "dicom_archive_path"),
    ("image_series_labelled", "dicom_archive_path"),
]


def _section(title: str) -> None:
    print(f"\n{BOLD}{title}{NC}")


def _column_exists(cur, tbl: str, col: str) -> bool:
    cur.execute(
        "SELECT 1 FROM information_schema.columns "
        "WHERE table_schema='public' AND table_name=%s AND column_name=%s",
        (tbl, col),
    )
    return cur.fetchone() is not None


def _autodetect_old_root(cur, cols: list[tuple[str, str]], new_root: str) -> str | None:
    """Infer the old root by locating the new root's basename in sampled values.

    Old and new roots share the same final component (only the prefix above it
    changed, tails identical) — e.g. old ``/Volumes/.../imaging_data`` and new
    ``/media/.../imaging_data`` both end in ``/imaging_data``. So the old root is
    everything up to and including that ``/<basename>`` in an old-host value.
    Returns None if nothing is found or the candidates are ambiguous.
    """
    marker = "/" + Path(new_root).name  # e.g. "/imaging_data" or "/compressed"
    candidates: set[str] = set()
    for tbl, col in cols:
        if not _column_exists(cur, tbl, col):
            continue
        # Sample rows whose value contains the marker but is NOT already under
        # the new root (those are already migrated and tell us nothing).
        cur.execute(
            f"SELECT {col} FROM {tbl} "  # noqa: S608 — tbl/col from a fixed allow-list
            f"WHERE {col} IS NOT NULL AND {col} <> '' "
            f"AND {col} LIKE %s AND {col} NOT LIKE %s LIMIT 50",
            (f"%{marker}/%", f"{new_root}/%"),
        )
        for (val,) in cur.fetchall():
            idx = val.find(marker + "/")
            if idx >= 0:
                candidates.add(val[: idx + len(marker)])
    if len(candidates) == 1:
        return next(iter(candidates))
    if len(candidates) > 1:
        print(f"  {YELLOW}⚠{NC} multiple old-root candidates found: {sorted(candidates)}")
    return None


def _repoint_group(cur, cols: list[tuple[str, str]], old_root: str, new_root: str) -> int:
    """Prefix-swap old_root -> new_root across a column group. Returns rows changed."""
    changed = 0
    for tbl, col in cols:
        if not _column_exists(cur, tbl, col):
            print(f"  {CYAN}ℹ{NC} {tbl}.{col} absent — skipped")
            continue
        # Prefix-safe: keep the tail exactly, swap only the leading old_root.
        cur.execute(
            f"UPDATE {tbl} SET {col} = %s || substring({col} from %s) "  # noqa: S608
            f"WHERE {col} LIKE %s",
            (new_root, len(old_root) + 1, old_root + "/%"),
        )
        n = cur.rowcount
        changed += n
        colour = GREEN if n else CYAN
        print(f"  {colour}•{NC} {tbl}.{col}: {n} rows")
    return changed


def _audit_remaining(cur, old_roots: list[str]) -> int:
    """Count any text column still holding one of the old prefixes. 0 == clean."""
    total = 0
    cur.execute(
        "SELECT table_name, column_name FROM information_schema.columns "
        "WHERE table_schema='public' AND data_type IN ('text','character varying')"
    )
    for tbl, col in cur.fetchall():
        conds = " OR ".join([f"{col} LIKE %s" for _ in old_roots])
        cur.execute(
            f"SELECT count(*) FROM {tbl} WHERE {col} IS NOT NULL AND ({conds})",  # noqa: S608
            tuple(f"{r}%" for r in old_roots),
        )
        n = cur.fetchone()[0]
        if n:
            total += n
            print(f"  {RED}✘{NC} {tbl}.{col}: {n} rows still on an old prefix")
    return total


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("--apply", action="store_true",
                    help="Commit the changes (default: dry-run — plan then roll back)")
    ap.add_argument("--old-loose", default=None,
                    help="Old dicom_data_root prefix (default: auto-detect)")
    ap.add_argument("--old-archive", default=None,
                    help="Old cold_archive_root prefix (default: auto-detect)")
    ap.add_argument("--no-reset-cache", action="store_true",
                    help="Do not reset series_cache_state to 'cold'")
    args = ap.parse_args()

    if not DB_CONFIG.get("user"):
        print("DB_USER not set in .env", file=sys.stderr)
        return 1

    new_loose = str(DICOM_DATA_ROOT)
    new_archive = str(COLD_ARCHIVE_ROOT)

    print(f"{BOLD}=== Repoint host paths ==={NC}")
    print(f"  DB:          {DB_CONFIG['dbname']} @ {DB_CONFIG['host']}:{DB_CONFIG['port']}")
    print(f"  mode:        {STORAGE_MODE}")
    print(f"  new loose:   {new_loose}")
    print(f"  new archive: {new_archive}")
    print(f"  {'APPLY (will commit)' if args.apply else 'DRY-RUN (rolls back)'}")

    conn = get_conn()
    try:
        cur = conn.cursor()

        # --- resolve old roots (explicit or auto-detected) ---
        _section("Old roots")
        old_loose = args.old_loose or _autodetect_old_root(cur, LOOSE_COLS, new_loose)
        old_archive = args.old_archive or _autodetect_old_root(cur, ARCHIVE_COLS, new_archive)
        for label, val in (("loose", old_loose), ("archive", old_archive)):
            if val:
                print(f"  {GREEN}✔{NC} old {label}: {val}")
            else:
                print(f"  {CYAN}ℹ{NC} old {label}: none detected (nothing to rewrite)")
        if not old_loose and not old_archive:
            print(f"\n  {GREEN}{BOLD}Nothing to do — no old prefixes present.{NC}")
            conn.rollback()
            return 0
        for label, old, new in (("loose", old_loose, new_loose), ("archive", old_archive, new_archive)):
            if old and old == new:
                print(f"\n  {RED}old {label} == new {label} ({old}); refusing (no-op).{NC}")
                conn.rollback()
                return 1

        # --- rewrite (single transaction) ---
        changed = 0
        if old_loose:
            _section(f"Loose tree  {old_loose}  ->  {new_loose}")
            changed += _repoint_group(cur, LOOSE_COLS, old_loose, new_loose)
        if old_archive:
            _section(f"Archives    {old_archive}  ->  {new_archive}")
            changed += _repoint_group(cur, ARCHIVE_COLS, old_archive, new_archive)

        # --- reset host-specific cache state ---
        if not args.no_reset_cache and _column_exists(cur, "series_cache_state", "status"):
            _section("Reset series_cache_state -> cold")
            cur.execute(
                "UPDATE series_cache_state SET status='cold', "
                "warming_started_at=NULL, error_message=NULL WHERE status <> 'cold'"
            )
            print(f"  {GREEN}•{NC} {cur.rowcount} rows reset to cold")

        # --- post-audit within the same (uncommitted) transaction ---
        _section("Audit: rows still on an old prefix")
        old_roots = [r for r in (old_loose, old_archive) if r]
        remaining = _audit_remaining(cur, old_roots)
        if remaining == 0:
            print(f"  {GREEN}✔{NC} none — every column is under a new root")

        # --- commit or roll back ---
        print(f"\n{BOLD}{'-' * 40}{NC}")
        if remaining:
            print(f"  {RED}{BOLD}Audit failed ({remaining} rows) — rolling back.{NC}")
            conn.rollback()
            return 1
        if args.apply:
            conn.commit()
            print(f"  {GREEN}{BOLD}Applied: {changed} path rows rewritten, committed.{NC}")
            print(f"  {CYAN}Next: python scripts/migration/reconcile_migration.py{NC}")
        else:
            conn.rollback()
            print(f"  {YELLOW}{BOLD}Dry-run: {changed} rows would change. Re-run with --apply.{NC}")
        return 0
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


if __name__ == "__main__":
    raise SystemExit(main())
