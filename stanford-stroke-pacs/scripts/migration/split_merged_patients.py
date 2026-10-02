#!/usr/bin/env python3
"""Separate patients that Alembic 0026 linked but are in fact different people.

Before 0026 a patient row was keyed by patient_id alone, so a second dataset
ingesting the same id merged into the existing row (its dataset tag added to
the row's array). 0026 turns such a row into one enrollment per dataset, all
*linked* (one subject, imaging owned by the first dataset alphabetically) — the
faithful reading of the old data. Where the shared id was a coincidence, this
script splits them: within each subject shared by an enrollment of
``--dataset``, studies whose ``image_study.import_label`` matches
``--import-label-like`` (a SQL LIKE pattern) are handed to that enrollment,
which then becomes its own person. Series follow their study; patient-level
labels stay with the enrollment they were recorded on; files are not moved.

Production use (the 12 CRISP2/LVO + PRECISE ids 11-001…11-010, 12-001, 12-002):

    python scripts/migration/split_merged_patients.py --dataset PRECISE \\
        --import-label-like 'precise\\_%'             # dry-run: per-patient plan
    python scripts/migration/split_merged_patients.py --dataset PRECISE \\
        --import-label-like 'precise\\_%' --apply

Then run scripts/data_integrity/reconcile.py (ownership mismatches must be 0)
and restart the web app (its DICOMweb access cache otherwise keeps the old
cross-dataset access for up to 5 minutes).
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import psycopg2
import psycopg2.extras
from dotenv import load_dotenv

STACK_ROOT = Path(__file__).resolve().parents[2]
load_dotenv(STACK_ROOT / ".env")
sys.path.insert(0, str(STACK_ROOT / "web-app"))
sys.path.insert(0, str(STACK_ROOT / "image_ingestion_protocols"))

from common import table_exists  # noqa: E402
from db import DB_CONFIG  # noqa: E402
from patient_identity import move_studies, refresh_subjects, unlink  # noqa: E402

from config import CLINICAL_DATA_DATASET  # noqa: E402

# Linked enrollments of `dataset` and the studies of their subject, owned by
# another enrollment, whose import_label matches.
PLAN_SQL = """
SELECT e.patient_key, e.subject_id, st.studyinstanceuid, st.import_label
FROM patient e
JOIN image_study st ON st.subject_id = e.subject_id AND st.patient_key <> e.patient_key
WHERE e.dataset = %(dataset)s
  AND EXISTS (SELECT 1 FROM patient o
              WHERE o.subject_id = e.subject_id AND o.patient_key <> e.patient_key)
  AND st.import_label LIKE %(pattern)s
ORDER BY e.patient_key, st.studyinstanceuid
"""


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--dataset", required=True,
                        help="Dataset (name) whose linked enrollments to split off")
    parser.add_argument("--import-label-like", required=True,
                        help="SQL LIKE pattern on image_study.import_label selecting "
                             "that dataset's studies (escape _ as \\_)")
    parser.add_argument("--apply", action="store_true",
                        help="Write the changes (default: dry-run, roll back)")
    args = parser.parse_args()

    if not DB_CONFIG.get("user"):
        print("DB_USER not set — check .env", file=sys.stderr)
        return 1

    conn = psycopg2.connect(**DB_CONFIG)
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("SET LOCAL app.audit_user = 'split_merged_patients'")
            cur.execute(PLAN_SQL, {"dataset": args.dataset, "pattern": args.import_label_like})
            plan: dict[str, dict] = {}
            for row in cur.fetchall():
                entry = plan.setdefault(row["patient_key"], {
                    "subject": row["subject_id"], "studies": [], "labels": set(),
                })
                entry["studies"].append(row["studyinstanceuid"])
                entry["labels"].add(row["import_label"])

            if not plan:
                print("Nothing to split: no linked enrollment of that dataset shares a "
                      "subject holding matching studies.")
                conn.rollback()
                return 0

            touched = set()
            print(f"{'Splitting' if args.apply else 'Would split'} {len(plan)} patient(s):")
            for key, entry in sorted(plan.items()):
                moved = move_studies(cur, entry["studies"], key)
                mine, rest = unlink(cur, key)
                touched.update(s for s in (mine, rest) if s)
                print(f"  {key}: takes {moved} studie(s) "
                      f"[{', '.join(sorted(entry['labels']))}] from {entry['subject']}; "
                      f"now its own person ({mine})")

            if not args.apply:
                conn.rollback()
                print("\nDRY RUN — rolled back. Re-run with --apply to write.")
                return 0
            refresh_subjects(
                cur, conn, touched,
                has_clinical_table=table_exists(cur, "clinical_data"),
                clinical_dataset=CLINICAL_DATA_DATASET,
            )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
    print(f"\nCommitted; re-derived {len(touched)} subject(s). Now run "
          "scripts/data_integrity/reconcile.py and restart the web app.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
