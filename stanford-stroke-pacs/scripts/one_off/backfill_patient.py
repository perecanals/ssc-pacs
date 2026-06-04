#!/usr/bin/env python3
"""Backfill the `patient` registry table from existing imaging metadata.

One row per distinct patient in `image_study`:
  - stroke_date   = MIN(image_study.acquisitiondatetime) for that patient
  - import_id     = origin batch = lowest import_id among the patient's studies
  - import_label  = the label of that origin batch
  - dataset       = ARRAY[<--dataset>] (or empty array if not given)

Origin is "who first registered the patient", so it keys on the **lowest
import_id**, not the earliest acquisition date (these can disagree).

Usage:
    python scripts/one_off/backfill_patient.py --dry-run
    python scripts/one_off/backfill_patient.py --execute --dataset legacy

Idempotent: ON CONFLICT (patient_id) DO NOTHING, so it can be safely re-run
(it only inserts patients that do not yet have a row).
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

# Bootstrap imports — repo layout: scripts/one_off/ → web-app/ is two levels up.
_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
_WEB_APP_DIR = _REPO_ROOT / "web-app"
sys.path.insert(0, str(_WEB_APP_DIR))
sys.path.insert(0, str(_REPO_ROOT))

from db import get_conn  # noqa: E402

# Build patient rows from image_study: earliest acquisition as stroke_date,
# origin (lowest import_id) batch for provenance. Only insert patients missing
# from the registry so re-runs are no-ops.
INSERT_SQL = """
INSERT INTO patient (patient_id, stroke_date, import_id, import_label, dataset, created_at, updated_at)
SELECT
    sd.patient_id,
    sd.stroke_date,
    orig.import_id,
    orig.import_label,
    %s::text[],
    now(),
    now()
FROM (
    SELECT patient_id, MIN(acquisitiondatetime) AS stroke_date
    FROM image_study
    WHERE patient_id IS NOT NULL
    GROUP BY patient_id
) sd
LEFT JOIN (
    SELECT DISTINCT ON (patient_id) patient_id, import_id, import_label
    FROM image_study
    WHERE patient_id IS NOT NULL AND import_id IS NOT NULL
    ORDER BY patient_id, import_id ASC
) orig ON orig.patient_id = sd.patient_id
ON CONFLICT (patient_id) DO NOTHING
"""

COUNT_CANDIDATES_SQL = """
SELECT COUNT(*) FROM (
    SELECT DISTINCT patient_id FROM image_study WHERE patient_id IS NOT NULL
) s
WHERE NOT EXISTS (SELECT 1 FROM patient p WHERE p.patient_id = s.patient_id)
"""


def backfill(*, execute: bool, dataset: str | None) -> int:
    dataset_arr = [dataset] if dataset else []
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(COUNT_CANDIDATES_SQL)
            candidates = cur.fetchone()[0]
            print(f"Patients in image_study without a `patient` row: {candidates}")

            if candidates == 0:
                print("Nothing to backfill.")
                return 0

            if not execute:
                print(f"[DRY-RUN] Would insert {candidates} patient row(s) "
                      f"with dataset={dataset_arr}.")
                return candidates

            cur.execute(INSERT_SQL, (dataset_arr,))
            inserted = cur.rowcount
            conn.commit()
            print(f"Inserted {inserted} patient row(s) (dataset={dataset_arr}).")
            return inserted
    finally:
        conn.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--dry-run", action="store_true", help="Preview without writing")
    group.add_argument("--execute", action="store_true", help="Apply the backfill")
    parser.add_argument("--dataset", default=None,
                        help="Dataset tag for backfilled rows, e.g. 'legacy'. "
                             "Omitted → empty array.")
    args = parser.parse_args()

    backfill(execute=args.execute, dataset=args.dataset)


if __name__ == "__main__":
    main()
