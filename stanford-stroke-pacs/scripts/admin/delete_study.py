#!/usr/bin/env python3
"""
Cleanly delete a study or series across all three layers it lives in:

  1. Orthanc index (orthanc_db + Folder-Indexer index + DICOMweb caches)
  2. stanford-stroke DB rows (image_study/image_series + side tables +
     annotations + *_labelled mirrors)
  3. on-disk files (loose dicom_dir_path tree + cold dicom_archive_path archives)

Annotations on the deleted entities are DISCARDED — the removal is captured in
`annotations_history` (append-only), so it stays auditable/recoverable, but no
values are migrated to any other study/series.

No privilege escalation needed: run this as the service user (which owns the
storage roots). `--execute` requires a typed `yes` confirmation; dry-run (the
default) prints the full plan and changes nothing.

Usage
-----
  # Review: list a patient's studies with no StudyDescription (the usual culprit)
  python scripts/admin/delete_study.py --patient <patient-id> --null-description

  # Dry-run a study delete (shows exactly what would go)
  python scripts/admin/delete_study.py --study 1.2.826...201

  # Execute (complete removal: Orthanc + DB + files + indexer purge)
  python scripts/admin/delete_study.py --study 1.2.826...201 --execute

  # Delete a single series
  python scripts/admin/delete_study.py --series 1.2.826...828 --execute

  # Sweep any orphan on-disk dirs with no image_study row
  python scripts/admin/delete_study.py --purge-orphan-files --execute
"""

from __future__ import annotations

import argparse
import getpass
import os
import sys
from pathlib import Path

import psycopg2
import psycopg2.extras
from dotenv import load_dotenv

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
load_dotenv(REPO_ROOT / ".env")

# Import web-app modules for the shared deletion core, DB config, and settings.
sys.path.insert(0, str(REPO_ROOT / "web-app"))
from db import DB_CONFIG  # noqa: E402
from deletion import (  # noqa: E402
    build_series_deletion_plan,
    build_study_deletion_plan,
    delete_index_and_db,
    find_orphan_study_dirs,
    purge_indexer_rows,
    remove_dir_list,
    remove_files,
)

from config import STORAGE_MODE  # noqa: E402


def _audit_actor() -> str:
    """Attribute the delete to the human behind sudo (SUDO_USER), else the user."""
    who = os.environ.get("SUDO_USER") or os.environ.get("USER") or getpass.getuser()
    return f"delete_study.py:{who}"


def _connect():
    conn = psycopg2.connect(**DB_CONFIG)
    with conn.cursor() as cur:
        # Session-level (survives commit) so the annotations_history trigger
        # attributes the D rows to the real operator.
        cur.execute("SET app.audit_user = %s", (_audit_actor(),))
    return conn


def _confirm(execute: bool, prompt: str, assume_yes: bool) -> bool:
    if not execute:
        return True
    if assume_yes:
        return True
    return input(prompt).strip().lower() == "yes"


def _print_plan(plan: dict) -> None:
    if plan["level"] == "study":
        print(f"  Study:   {plan['studyinstanceuid']}")
        print(f"  Patient: {plan['patient_id']}   "
              f"Description: {plan['studydescription'] or '<NULL/EMPTY>'}")
        print(f"  Series:  {plan['n_series']}")
    else:
        print(f"  Series:  {plan['seriesinstanceuid']}")
        print(f"  Study:   {plan['studyinstanceuid']}   Patient: {plan['patient_id']}")
    print(f"  Orthanc: {plan['orthanc']['id'] or '<not indexed>'}")
    print(f"  Annotations to DISCARD: {plan['n_annotations']}")
    print("  On-disk dirs to remove:")
    for d in plan["remove_dirs"]:
        print(f"    - {d}")


def _list_null_description(patient: str) -> int:
    conn = psycopg2.connect(**DB_CONFIG)
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(
                "SELECT st.studyinstanceuid, st.acquisitiondatetime, "
                "  (SELECT count(*) FROM image_series s "
                "     WHERE s.studyinstanceuid = st.studyinstanceuid) AS n_series "
                "FROM image_study st "
                "WHERE st.patient_id = %s "
                "  AND (st.studydescription IS NULL OR st.studydescription = '') "
                "ORDER BY st.acquisitiondatetime",
                (patient,),
            )
            rows = cur.fetchall()
    finally:
        conn.close()
    if not rows:
        print(f"No null/empty-description studies for patient {patient}.")
        return 0
    print(f"Null/empty-description studies for patient {patient} ({len(rows)}):")
    for r in rows:
        print(f"  {r['studyinstanceuid']}  "
              f"acq={r['acquisitiondatetime']}  series={r['n_series']}")

    flags = " ".join(f"--study {r['studyinstanceuid']}" for r in rows)
    prog = "python scripts/admin/delete_study.py"
    print("\nReview all (dry-run) — copy/paste:\n")
    print(f"  {prog} {flags}\n")
    print("Delete all — copy/paste (add --yes to skip the confirmation prompt):\n")
    print(f"  {prog} {flags} --execute")
    return 0


def _purge_orphans(execute: bool, assume_yes: bool) -> int:
    conn = psycopg2.connect(**DB_CONFIG)
    try:
        orphans = find_orphan_study_dirs(conn)
    finally:
        conn.close()
    if not orphans:
        print("No orphan study directories found (DB and disk agree).")
        return 0
    print(f"{'EXECUTE' if execute else 'DRY RUN'}: "
          f"{len(orphans)} orphan study dir(s) with no image_study row:")
    for d in orphans:
        print(f"  - {d}")
    if not _confirm(execute, f"\nType 'yes' to remove {len(orphans)} dir(s): ", assume_yes):
        print("Aborted.")
        return 0
    res = remove_dir_list(orphans, execute=execute)
    # Purge the Folder-Indexer rows for the (now-removed) loose dirs. Archive-root
    # dirs are silently skipped inside purge_indexer_rows (never indexed).
    idx = purge_indexer_rows(orphans, execute=execute)
    print(f"\nRemoved: {len(res['removed'])}   Already gone: {len(res['missing'])}   "
          f"Indexer dirs purged: {len(idx['purged'])}")
    if idx["skipped_nonempty"]:
        print(f"WARNING: indexer purge skipped {len(idx['skipped_nonempty'])} dir(s) "
              f"that still contain files: {idx['skipped_nonempty']}", file=sys.stderr)
    if not execute:
        print("DRY RUN — nothing was deleted. Re-run with --execute to apply.")
    return 0


def _delete_targets(args) -> int:
    conn = _connect()
    try:
        plans = []
        for uid in args.study or []:
            plan = build_study_deletion_plan(conn, uid)
            if plan is None:
                print(f"WARNING: study {uid} not found in image_study — skipping.",
                      file=sys.stderr)
                continue
            plans.append(plan)
        for uid in args.series or []:
            plan = build_series_deletion_plan(conn, uid)
            if plan is None:
                print(f"WARNING: series {uid} not found in image_series — skipping.",
                      file=sys.stderr)
                continue
            plans.append(plan)

        if not plans:
            print("Nothing to do (no valid --study/--series targets).")
            return 1

        total_annot = sum(p["n_annotations"] for p in plans)
        print(f"Mode: {'EXECUTE' if args.execute else 'DRY RUN'}")
        print(f"Targets: {len(plans)}   Annotations to discard (total): {total_annot}\n")
        for plan in plans:
            _print_plan(plan)
            print()

        if not _confirm(
            args.execute,
            f"Type 'yes' to delete {len(plans)} target(s) and discard "
            f"{total_annot} annotation(s): ",
            args.yes,
        ):
            print("Aborted.")
            return 0

        for plan in plans:
            label = plan.get("studyinstanceuid") or plan.get("seriesinstanceuid")
            idb = delete_index_and_db(conn, plan, execute=args.execute)
            files = remove_files(plan, execute=args.execute)
            # Purge the Orthanc Folder-Indexer rows AFTER the files are gone
            # (a Force scan re-registers any file still on disk — see deletion.py).
            idx = purge_indexer_rows(plan.get("indexer_dirs", []), execute=args.execute)
            # In dry-run the loose dirs still exist, so the purge guard defers them
            # (skipped_nonempty) — on --execute they're removed first, then purged.
            # Report the eventual purge count so the dry-run isn't misleading.
            purge_n = (len(idx["purged"]) if args.execute
                       else len(idx["purged"]) + len(idx["skipped_nonempty"]))
            purge_label = "indexer_purged" if args.execute else "indexer_to_purge"
            verb = "Deleted" if args.execute else "Would delete"
            print(f"{verb} {plan['level']} {label}: "
                  f"orthanc={idb['orthanc_deleted']} "
                  f"annotations={idb['annotations']} "
                  f"image_series={idb['image_series']} "
                  f"image_study={idb['image_study']} "
                  f"dirs_removed={len(files['removed'])} "
                  f"dirs_missing={len(files['missing'])} "
                  f"pruned={len(files.get('pruned', []))} "
                  f"{purge_label}={purge_n}")
            # Only meaningful on --execute: a dir still populated *after* removal
            # would be re-registered by the Force scan. In dry-run it's expected.
            if args.execute and idx["skipped_nonempty"]:
                print(f"  WARNING: indexer purge skipped {len(idx['skipped_nonempty'])} "
                      f"dir(s) that still contain files (would resurrect): "
                      f"{idx['skipped_nonempty']}", file=sys.stderr)
    finally:
        conn.close()

    if not args.execute:
        print("\nDRY RUN — nothing was deleted. Re-run with --execute to apply.")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("--study", action="append", metavar="UID",
                    help="StudyInstanceUID to delete (repeatable)")
    ap.add_argument("--series", action="append", metavar="UID",
                    help="SeriesInstanceUID to delete (repeatable)")
    ap.add_argument("--patient", metavar="ID",
                    help="With --null-description: which patient to list")
    ap.add_argument("--null-description", action="store_true",
                    help="List a patient's null/empty-StudyDescription studies "
                         "(review only; never deletes)")
    ap.add_argument("--purge-orphan-files", action="store_true",
                    help="Remove on-disk study dirs with no image_study row "
                         "(the sweep after a UI index+DB delete)")
    ap.add_argument("--execute", action="store_true",
                    help="Actually delete (default: dry-run). Requires root.")
    ap.add_argument("--yes", action="store_true",
                    help="Skip the typed confirmation prompt")
    args = ap.parse_args()

    if STORAGE_MODE != "cold_path_cache":
        print(f"WARNING: STORAGE_MODE is '{STORAGE_MODE}' (not 'cold_path_cache'); "
              "this tool targets the cold-storage layout. Aborting.", file=sys.stderr)
        return 2
    if not DB_CONFIG.get("user"):
        print("DB_USER not set in .env", file=sys.stderr)
        return 1

    if args.null_description:
        if not args.patient:
            print("--null-description requires --patient", file=sys.stderr)
            return 1
        return _list_null_description(args.patient)

    if args.purge_orphan_files:
        return _purge_orphans(args.execute, args.yes)

    if not args.study and not args.series:
        ap.error("give at least one --study/--series, or use --null-description / "
                 "--purge-orphan-files")
    return _delete_targets(args)


if __name__ == "__main__":
    raise SystemExit(main())
