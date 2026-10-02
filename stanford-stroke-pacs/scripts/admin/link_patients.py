#!/usr/bin/env python3
"""Link patients across datasets: declare that two enrollments are one person.

A patient is an *enrollment* — one (dataset, patient_id) pair — and the
enrollments of the same person share a *subject* (Alembic 0026). Linking puts a
patient of one dataset into another's subject. The two then share imaging
(each study stays owned by the enrollment that ingested it, and is visible from
both) and study/series labels; patient labels stay separate per enrollment, and
each dataset's users see only their own dataset's identifiers.

The input is a CSV (or Excel) table. ``link`` rows read "this patient is the same
person as that already-ingested one":

    dataset,patient_id,link_dataset,link_patient_id
    OUTERLIMITS,OL-0001,CRISP2/LVO,2-076

The ``link_dataset`` / ``link_patient_id`` enrollment must exist. The
``dataset`` / ``patient_id`` one is created when it does not exist yet (with no
imaging of its own); when it does, its whole subject — including imaging it
owns — joins the other's. ``unlink`` takes ``dataset,patient_id`` rows and makes
each such enrollment its own person again, taking the imaging it owns with it.

Dry-run by default: validates every row and reports what would change, then
rolls back. ``--apply`` writes everything in one transaction, then re-derives
what follows from a person's imaging (stroke dates, series ranks, timepoints,
the labelled mirrors). See docs/operations/linking_patients.md.

Usage:
    python scripts/admin/link_patients.py link outerlimits_links.csv
    python scripts/admin/link_patients.py link outerlimits_links.csv --apply
    python scripts/admin/link_patients.py unlink mistakes.csv --apply

Link *before* ingesting a dataset that carries the same imaging as an existing
one: ingestion refuses a study owned by a different, unlinked person.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import psycopg2
from dotenv import load_dotenv

STACK_ROOT = Path(__file__).resolve().parents[2]
load_dotenv(STACK_ROOT / ".env")
sys.path.insert(0, str(STACK_ROOT / "web-app"))
sys.path.insert(0, str(STACK_ROOT / "image_ingestion_protocols"))

from common import table_exists  # noqa: E402
from db import DB_CONFIG  # noqa: E402
from patient_identity import (  # noqa: E402
    create_enrollment,
    dataset_slug,
    enrollment,
    enrollment_key,
    link,
    refresh_subjects,
    unlink,
)

from config import CLINICAL_DATA_DATASET  # noqa: E402

LINK_COLUMNS = ("dataset", "patient_id", "link_dataset", "link_patient_id")
UNLINK_COLUMNS = ("dataset", "patient_id")


def _load(path: Path, columns) -> list[dict]:
    # Deferred: only reading the table needs pandas (not installed in the
    # web-app test environment, which imports this module).
    import pandas as pd  # noqa: PLC0415

    if path.suffix.lower() in (".xlsx", ".xls"):
        df = pd.read_excel(path, dtype=str)
    else:
        df = pd.read_csv(path, dtype=str)
    missing = [c for c in columns if c not in df.columns]
    if missing:
        sys.exit(f"Error: {path} lacks column(s) {missing}; expected {list(columns)}.")
    rows = []
    for number, record in enumerate(df[list(columns)].to_dict(orient="records"), start=2):
        values = {c: ("" if pd.isna(v) else str(v).strip()) for c, v in record.items()}
        if not any(values.values()):
            continue
        blank = [c for c, v in values.items() if not v]
        if blank:
            sys.exit(f"Error: line {number}: empty {blank}.")
        rows.append({"line": number, **values})
    return rows


def _link_rows(cur, rows, source_name: str) -> tuple[list[str], set[str]]:
    report, touched = [], set()
    pairs = set()
    for row in rows:
        where = f"line {row['line']}"
        for ds in (row["dataset"], row["link_dataset"]):
            if dataset_slug(cur, ds) is None:
                raise ValueError(f"{where}: dataset {ds!r} is not registered "
                                 "(scripts/admin/manage_datasets.py add)")
        if (row["dataset"], row["patient_id"]) == (row["link_dataset"], row["link_patient_id"]):
            raise ValueError(f"{where}: a patient cannot be linked to itself")
        if (row["dataset"], row["patient_id"]) in pairs:
            raise ValueError(f"{where}: {row['dataset']}/{row['patient_id']} appears twice")
        pairs.add((row["dataset"], row["patient_id"]))
        target_key = enrollment_key(cur, row["link_dataset"], row["link_patient_id"])
        if target_key is None:
            raise ValueError(f"{where}: {row['link_dataset']}/{row['link_patient_id']} "
                             "is not enrolled — ingest it first")
        target = enrollment(cur, target_key)
        source_key = enrollment_key(cur, row["dataset"], row["patient_id"])
        if source_key is None:
            source_key = create_enrollment(
                cur, row["dataset"], row["patient_id"], target["subject_id"],
                import_label=f"link:{source_name}",
            )
            report.append(f"  enroll {source_key} as {target_key}'s person (new)")
            touched.add(target["subject_id"])
            continue
        old, new = link(cur, source_key, target_key)
        if old == new:
            report.append(f"  {source_key} already linked to {target_key}")
        else:
            report.append(f"  link {source_key} -> {target_key} (subject {old} joins {new})")
            touched.update((old, new))
    return report, touched


def _unlink_rows(cur, rows) -> tuple[list[str], set[str]]:
    report, touched = [], set()
    for row in rows:
        key = enrollment_key(cur, row["dataset"], row["patient_id"])
        if key is None:
            raise ValueError(f"line {row['line']}: {row['dataset']}/{row['patient_id']} "
                             "is not enrolled")
        mine, rest = unlink(cur, key)
        if rest is None:
            report.append(f"  {key} was not linked")
        else:
            report.append(f"  unlink {key} (own subject {mine}; the rest: {rest})")
            touched.update((mine, rest))
    return report, touched


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("command", choices=("link", "unlink"))
    parser.add_argument("file", type=Path, help="CSV or Excel table (see the columns above)")
    parser.add_argument("--apply", action="store_true",
                        help="Write the changes (default: dry-run, roll back)")
    args = parser.parse_args()

    if not DB_CONFIG.get("user"):
        print("DB_USER not set — check .env", file=sys.stderr)
        return 1
    rows = _load(args.file, LINK_COLUMNS if args.command == "link" else UNLINK_COLUMNS)
    if not rows:
        print("No rows.")
        return 0

    conn = psycopg2.connect(**DB_CONFIG)
    try:
        with conn.cursor() as cur:
            cur.execute("SET LOCAL app.audit_user = %s", (f"link:{args.file.name}",))
            try:
                if args.command == "link":
                    report, touched = _link_rows(cur, rows, args.file.name)
                else:
                    report, touched = _unlink_rows(cur, rows)
            except ValueError as exc:
                conn.rollback()
                print(f"Error: {exc}. Nothing was written.", file=sys.stderr)
                return 1
            print(f"{'Applying' if args.apply else 'Dry-run'}: {len(rows)} row(s)")
            print("\n".join(report))
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
    print(f"\nCommitted; re-derived {len(touched)} subject(s).")
    if args.command == "unlink":
        print("Restart the web app (sudo systemctl restart ssc-web-app): its DICOMweb "
              "access cache may otherwise keep the old cross-dataset access for up to "
              "5 minutes.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
