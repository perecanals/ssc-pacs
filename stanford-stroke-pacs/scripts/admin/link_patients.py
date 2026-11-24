#!/usr/bin/env python3
"""Link patients across datasets: declare that two enrollments are one person.

Standalone linking also shares studies currently owned by the named reference
enrollment. Use --identity-only for an identity link without adding memberships.
Ingestion links retain their own same-UID study membership workflow.

The input is a CSV (or Excel) table. ``link`` rows read "this patient is the same
person as that already-ingested one":

    dataset,patient_id,link_dataset,link_patient_id
    OUTERLIMITS,OL-0001,CRISP2/LVO,2-076

``link`` also takes the JSON link file that ingestion accepts as
``patient_links`` (one dataset linked to one reference dataset):

    {"dataset": "OUTERLIMITS", "link_dataset": "CRISP2/LVO",
     "links": {"OL-0001": "2-076"}}

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
    python scripts/admin/link_patients.py link outerlimits_links.json --apply
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

from db import DB_CONFIG  # noqa: E402
from linked_view import after_identity_change  # noqa: E402
from patient_identity import (  # noqa: E402
    apply_links,
    enrollment_key,
    load_links_json,
    refresh_subjects,
    unlink,
)

LINK_COLUMNS = ("dataset", "patient_id", "link_dataset", "link_patient_id")
UNLINK_COLUMNS = ("dataset", "patient_id")


def _load(path: Path, columns) -> list[dict]:
    if path.suffix.lower() == ".json":
        if columns != LINK_COLUMNS:
            sys.exit(f"Error: {path}: a JSON link file is for 'link'; unlink takes a CSV.")
        try:
            return load_links_json(path)
        except ValueError as exc:
            sys.exit(f"Error: {exc}")
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


# Resolve only the manifest's directed pairs, even on subjects with a third enrollment.
_PAIRS = """
WITH pairs AS (
    SELECT dest.patient_key AS dest, dest.patient_id, dest.subject_id, ref.patient_key AS ref
    FROM unnest(%s::text[], %s::text[], %s::text[], %s::text[]) x(ds, id, ref_ds, ref_id)
    JOIN patient dest ON (dest.dataset, dest.patient_id) = (x.ds, x.id)
    JOIN patient ref ON (ref.dataset, ref.patient_id) = (x.ref_ds, x.ref_id)
)
"""


def standalone_links(cur, rows, filename, *, identity_only=False, instrument=None):
    report, touched = apply_links(cur, rows, filename)
    params = tuple([r[col] for r in rows] for col in LINK_COLUMNS)
    if not identity_only:
        cur.execute(_PAIRS + """
            , added AS (
                INSERT INTO study_member (patient_key, studyinstanceuid)
                SELECT p.dest, st.studyinstanceuid FROM pairs p
                JOIN image_study st ON st.patient_key = p.ref
                ON CONFLICT DO NOTHING RETURNING patient_key, studyinstanceuid
            )
            SELECT p.subject_id, p.dest, count(DISTINCT a.studyinstanceuid),
                   count(s.seriesinstanceuid)
            FROM added a JOIN pairs p ON p.dest = a.patient_key
            LEFT JOIN image_series s USING (studyinstanceuid)
            GROUP BY p.subject_id, p.dest
        """, params)
        changes = cur.fetchall()
        touched.update(subject for subject, _, _, _ in changes)
        report.append(f"New study memberships: {sum(r[2] for r in changes)}; "
                      f"series covered: {sum(r[3] for r in changes)}; "
                      f"enrollments gaining studies: {len(changes)}")
    if instrument:
        cur.execute("SELECT 1 FROM label_definitions WHERE level='patient' AND instrument=%s",
                    (instrument,))
        if cur.fetchone() is None:
            raise ValueError(f"no patient labels for instrument {instrument!r}")
        cur.execute(_PAIRS + """
            , candidates AS (
                SELECT p.*, a.label, a.value FROM pairs p JOIN annotations a
                ON a.patient_key = p.ref AND a.level = 'patient'
                JOIN label_definitions d ON d.name = a.label AND d.level = a.level
                WHERE d.instrument = %s
            ), added AS (
                INSERT INTO annotations (level, patient_key, patient_id, label, value, created_by)
                SELECT 'patient', dest, patient_id, label, value, %s FROM candidates
                ON CONFLICT (patient_key, label) WHERE level='patient' DO NOTHING
                RETURNING patient_key, label
            )
            SELECT c.subject_id, c.dest, c.label, added.patient_key IS NOT NULL,
                   existing.value IS DISTINCT FROM c.value
            FROM candidates c LEFT JOIN added ON (added.patient_key,added.label)=(c.dest,c.label)
            LEFT JOIN annotations existing ON existing.level='patient'
                AND (existing.patient_key,existing.label)=(c.dest,c.label)
        """, (*params, instrument, f"link:{filename}"))
        values = cur.fetchall()
        copied = sum(row[3] for row in values)
        touched.update(subject for subject, _, _, added, _ in values if added)
        conflicts = [(dest, label) for _, dest, label, added, differs in values
                     if not added and differs]
        report.extend(f"  kept differing patient label {dest}: {label}" for dest, label in conflicts)
        report.append(f"Patient labels copied: {copied}; skipped: {len(values) - copied}; "
                      f"differing: {len(conflicts)}")
    return report, touched


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("command", choices=("link", "unlink"))
    parser.add_argument("file", type=Path, help="CSV or Excel table (see the columns above)")
    parser.add_argument("--apply", action="store_true",
                        help="Write the changes (default: dry-run, roll back)")
    parser.add_argument("--identity-only", action="store_true",
                        help="Link identity without adding study memberships")
    parser.add_argument("--copy-patient-instrument", metavar="NAME",
                        help="Copy missing reference patient values from this instrument")
    args = parser.parse_args()
    if args.command == "unlink" and (args.identity_only or args.copy_patient_instrument):
        parser.error("study sharing and instrument options apply only to link")

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
                    report, touched = standalone_links(
                        cur, rows, args.file.name, identity_only=args.identity_only,
                        instrument=args.copy_patient_instrument,
                    )
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
            refresh_subjects(cur, conn, touched)
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
    print(f"\nCommitted; re-derived {len(touched)} subject(s).")
    conn = psycopg2.connect(**DB_CONFIG)
    try:
        for line in after_identity_change(conn):
            print(line)
    finally:
        conn.close()
    if args.command == "unlink":
        print("Restart the web app (sudo systemctl restart ssc-web-app): its DICOMweb "
              "access cache may otherwise keep the old cross-dataset access for up to "
              "5 minutes.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
