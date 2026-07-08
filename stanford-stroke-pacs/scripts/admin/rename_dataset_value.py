#!/usr/bin/env python3
"""Rename a cohort tag everywhere dataset values are stored.

Dataset tags live in two overlap-checked tables — ``patient.dataset`` (the
data) and ``users.allowed_datasets`` (per-user access grants) — plus the
``patient_labelled`` mirror. Renaming them together in one transaction is the
only safe way: renaming the patient side alone would instantly lock every
granted user out (the ``dataset && allowed_datasets`` check stops matching).

Usage:
    python scripts/admin/rename_dataset_value.py --from-value lvo --to-value 'CRISP2/LVO'
    python scripts/admin/rename_dataset_value.py --from-value lvo --to-value 'CRISP2/LVO' --execute

Dry-run (default) previews the affected rows and rolls back. Idempotent:
re-running after a rename matches zero rows. If the new tag already coexists
with the old one on a row, the result is deduped.

After renaming, remember:
  - the live image_ingestion_protocols/execute_image_ingestion_protocol.yaml
    must use the NEW tag, or the next ingestion re-creates the old one;
  - the web app's DICOMweb proxy caches scopes for up to 5 minutes — restart
    the app (or wait) if OHIF briefly 403s right after the rename.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

from dotenv import load_dotenv

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
load_dotenv(REPO_ROOT / ".env")

sys.path.insert(0, str(REPO_ROOT / "web-app"))

from db import get_conn  # noqa: E402
from labelled_table_sync import sync_labelled_rows  # noqa: E402

# array_replace swaps the tag in place; the ARRAY(SELECT DISTINCT …) wrapper
# dedupes in case the new tag already coexisted with the old on the same row.
PATIENT_SQL = """
UPDATE patient
SET dataset = ARRAY(
        SELECT DISTINCT unnest(array_replace(dataset, %(old)s, %(new)s)) ORDER BY 1
    ),
    updated_at = now()
WHERE %(old)s = ANY(dataset)
RETURNING patient_id
"""

USERS_SQL = """
UPDATE users
SET allowed_datasets = ARRAY(
        SELECT DISTINCT unnest(array_replace(allowed_datasets, %(old)s, %(new)s)) ORDER BY 1
    )
WHERE %(old)s = ANY(allowed_datasets)
RETURNING username
"""


def main() -> None:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--from-value", required=True, help="Existing dataset tag to rename.")
    parser.add_argument("--to-value", required=True, help="New name for the tag.")
    parser.add_argument(
        "--execute", action="store_true",
        help="Apply the rename. Without this flag, preview and roll back.",
    )
    args = parser.parse_args()

    old, new = args.from_value.strip(), args.to_value.strip()
    if not old or not new:
        sys.exit("Error: --from-value and --to-value must be non-empty.")
    if old == new:
        sys.exit("Error: --from-value and --to-value are identical.")

    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(PATIENT_SQL, {"old": old, "new": new})
            patient_ids = [r[0] for r in cur.fetchall()]

            cur.execute(USERS_SQL, {"old": old, "new": new})
            usernames = [r[0] for r in cur.fetchall()]

            # Keep the patient_labelled mirror in lockstep (it copies the
            # dataset column verbatim from `patient`).
            if patient_ids:
                sync_labelled_rows(conn, "patient", patient_ids)

        mode = "Renamed" if args.execute else "Would rename"
        print(f"{mode} dataset tag {old!r} -> {new!r}:")
        print(f"  patients:               {len(patient_ids)}")
        print(f"  user grants:            {len(usernames)}"
              + (f"  ({', '.join(usernames)})" if usernames else ""))
        print(f"  patient_labelled rows:  {len(patient_ids)} (synced)")

        if not patient_ids and not usernames:
            print(f"Nothing references {old!r} — nothing to do.")

        if args.execute:
            conn.commit()
            print("Committed.")
        else:
            conn.rollback()
            print("Dry-run: rolled back. Re-run with --execute to apply.")
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


if __name__ == "__main__":
    main()
