#!/usr/bin/env python3
"""Recompute image_series.series_type / image_study.study_type from series_dicom_tags.

Applies `image_ingestion_protocols/series_classification.py` — the SAME classifier
the ingestion pipeline runs at ingest time — to the DICOM tags already stored in
`series_dicom_tags`. No archive I/O: a full corpus pass is a table scan, so
iterating on the classification lexicons costs seconds rather than the ~51 minutes
a re-read of 127k cold archives would.

Re-run this whenever the lexicons change. It rewrites every matching row (not just
NULLs) — the existing data is *not* self-consistent and cannot be repaired by
filling gaps: `series_type` is NULL on the sir_batch* imports and '' on the older
ones; 768 rows carry a CTA label emitted by a retired code path; 67 MR series are
labelled CTP from a pre-modality-guard bug. Recompute-everything is the point.

Every write stamps `series_type_rule` (which rule fired) and `series_type_version`
(RULES_VERSION), so a classification can always be explained and safely redone.

MACHINE-OWNED, and independent of the human annotation labels `series_type` /
`study_type` (mirrored as label_series_type_* / label_study_type_*). This script
never reads or writes those, in either direction.

Dry-run by default — prints a confusion report (current -> proposed, counts per
rule, and the unresolved residue with example descriptions). Pass --execute to
write. Series with no `series_dicom_tags` row are skipped; run the tag backfill
first.

Examples:
    python scripts/admin/reclassify_series_types.py                    # dry-run, whole corpus
    python scripts/admin/reclassify_series_types.py --label sir_batch1
    python scripts/admin/reclassify_series_types.py --execute
"""

from __future__ import annotations

import argparse
import sys
from collections import Counter, defaultdict
from pathlib import Path

from dotenv import load_dotenv

STACK_ROOT = Path(__file__).resolve().parents[2]
load_dotenv(STACK_ROOT / ".env")
sys.path.insert(0, str(STACK_ROOT / "web-app"))
sys.path.insert(0, str(STACK_ROOT / "image_ingestion_protocols"))

from common import table_exists  # noqa: E402
from series_classification import (  # noqa: E402
    ASSIGN_RANKS_SQL,
    CLEAR_RANKS_SQL,
    RULES_VERSION,
    assign_patient_timepoints,
    classify_series,
    classify_study,
)


def _fmt(value: str | None) -> str:
    if value is None:
        return "<null>"
    if value == "":
        return "<empty>"
    return value


def _print_series_report(rows: list[dict]) -> None:
    """rows: {suid, current, proposed, rule, descr, kernel, modality}"""
    print(f"\n=== SERIES: {len(rows)} classified (rules {RULES_VERSION}) ===\n")

    print("--- proposed series_type ---")
    for value, count in Counter(r["proposed"] for r in rows).most_common():
        print(f"  {count:7}  {_fmt(value)}")

    print("\n--- rule that fired ---")
    for rule, count in Counter(r["rule"] for r in rows).most_common(25):
        print(f"  {count:7}  {rule}")

    changed = [r for r in rows if r["current"] != r["proposed"]]
    print(f"\n--- changes: {len(changed)} of {len(rows)} rows ---")
    transitions = Counter((_fmt(r["current"]), _fmt(r["proposed"])) for r in changed)
    for (before, after), count in transitions.most_common(20):
        print(f"  {count:7}  {before:12} -> {after}")

    # The residue is the artifact you iterate against: what the lexicons still
    # cannot name, ranked by how much of the corpus each unnamed family costs.
    unresolved = [r for r in rows if r["proposed"] is None]
    if unresolved:
        print(f"\n--- UNRESOLVED: {len(unresolved)} ({len(unresolved)/len(rows):.1%}) ---")
        for rule, count in Counter(r["rule"] for r in unresolved).most_common(10):
            print(f"  {count:7}  {rule}")
        print("\n  most common unresolved descriptions:")
        examples = Counter(
            (r["modality"], r["kernel"] or "-", (r["descr"] or "")[:44]) for r in unresolved
        )
        for (modality, kernel, descr), count in examples.most_common(15):
            print(f"    {count:5}  {modality:3} kern={kernel:16} {descr}")


def _print_study_report(rows: list[dict]) -> None:
    print(f"\n=== STUDIES: {len(rows)} classified ===\n")
    for value, count in Counter(r["proposed"] for r in rows).most_common():
        print(f"  {count:7}  {_fmt(value)}")

    unresolved = [r for r in rows if r["proposed"] is None]
    if unresolved:
        print(f"\n--- UNRESOLVED studies: {len(unresolved)} ---")
        for descr, count in Counter((r["descr"] or "")[:60] for r in unresolved).most_common(12):
            print(f"    {count:5}  {descr}")


def _print_timepoint_report(rows: list[dict]) -> None:
    print(f"\n=== TIMEPOINT: {len(rows)} studies ===\n")
    for value, count in Counter(r["timepoint"] for r in rows).most_common():
        print(f"  {count:7}  {_fmt(value)}")

    # The anchor source is the honesty column: only femoral_sheath_time is a
    # recorded puncture. The other two are +5h / +10h estimates, so a BL/FU built
    # on them is materially weaker evidence.
    print("\n--- anchor source (only femoral_sheath_time is a RECORDED puncture) ---")
    notes = {
        "femoral_sheath_time": "recorded puncture",
        "receiving_arrival_time": "ESTIMATED (arrival + 5h)",
        "time_recognized": "ESTIMATED (recognition + 10h)",
        "thrombectomy_study": "episode's own thrombectomy study time (no clinical anchor)",
        None: "no anchor at all -> timepoint is NULL",
    }
    for source, count in Counter(r["timepoint_anchor_source"] for r in rows).most_common():
        print(f"  {count:7}  {_fmt(source):24}  {notes.get(source, '?')}")

    print("\n--- rule ---")
    for rule, count in Counter(r["timepoint_rule"] for r in rows).most_common():
        print(f"  {count:7}  {rule}")


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--label", help="restrict to one import_label")
    parser.add_argument("--patient", help="restrict to one patient_id")
    parser.add_argument("--limit", type=int, help="cap the number of series")
    parser.add_argument(
        "--execute",
        action="store_true",
        help="write the results (default: dry-run, report only)",
    )
    args = parser.parse_args()

    # Deferred so the module stays importable in a DB-free unit test.
    import psycopg2  # noqa: PLC0415
    import psycopg2.extras  # noqa: PLC0415
    from db import DB_CONFIG  # noqa: PLC0415

    if not DB_CONFIG.get("user"):
        print("DB_USER not set — check .env", file=sys.stderr)
        return 1

    where, params = ["TRUE"], []
    if args.label:
        where.append("s.import_label = %s")
        params.append(args.label)
    if args.patient:
        where.append("s.patient_id = %s")
        params.append(args.patient)

    sql = f"""
        SELECT s.seriesinstanceuid, s.studyinstanceuid, s.series_type,
               t.tags, t.same_position_count, t.n_instances_scanned
        FROM image_series s
        JOIN series_dicom_tags t USING (seriesinstanceuid)
        WHERE {' AND '.join(where)}
        ORDER BY s.seriesinstanceuid
    """
    if args.limit:
        sql += f" LIMIT {int(args.limit)}"

    conn = psycopg2.connect(**DB_CONFIG)
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(sql, params)
        series_rows = cur.fetchall()

        cur.execute("SELECT count(*) AS n FROM image_series")
        total_series = cur.fetchone()["n"]

    if not series_rows:
        print("No series with tag rows matched. Run the series_dicom_tags backfill first.")
        return 1

    missing = total_series - len(series_rows)
    if missing > 0 and not (args.label or args.patient or args.limit):
        print(
            f"NOTE: {missing} of {total_series} image_series rows have no "
            f"series_dicom_tags row and are skipped (not reclassified). "
            f"Run maintenance/scripts/backfill_series_dicom_tags.py to cover them."
        )

    results, study_types = [], defaultdict(list)
    for row in series_rows:
        tags = row["tags"] or {}
        proposed, rule = classify_series(
            tags, row["same_position_count"], row["n_instances_scanned"]
        )
        results.append({
            "suid": row["seriesinstanceuid"],
            "current": row["series_type"],
            "proposed": proposed,
            "rule": rule,
            "descr": tags.get("SeriesDescription"),
            "kernel": str(tags.get("ConvolutionKernel") or ""),
            "modality": tags.get("Modality") or "?",
        })
        study_types[row["studyinstanceuid"]].append(proposed)

    _print_series_report(results)

    # Study level. study_type is derived from StudyDescription alone — series_types
    # is passed for signature parity but deliberately unused, so a series-rule
    # change can never silently move a study's type.
    #
    # timepoint is episode-aware (assign_patient_timepoints): a patient's studies
    # are split into episodes and each anchored on its own femoral-sheath puncture
    # from lvo_clinical_data (NOT patient.stroke_date — a different clock), else its
    # own thrombectomy study. Episodes with neither get a NULL timepoint, not a guess.
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        # lvo_clinical_data is optional (site-specific import). Without it every
        # anchor column reads NULL — the exact shape a patient with no clinical
        # row already yields — so each episode falls back to its own
        # thrombectomy study.
        if table_exists(cur, "lvo_clinical_data"):
            clinical_cols = (
                "c.femoral_sheath_time, c.receiving_arrival_time, c.time_recognized"
            )
            clinical_join = "LEFT JOIN lvo_clinical_data c ON c.study_id = st.patient_id"
        else:
            clinical_cols = (
                "NULL::text AS femoral_sheath_time, "
                "NULL::text AS receiving_arrival_time, "
                "NULL::text AS time_recognized"
            )
            clinical_join = ""
        cur.execute(
            f"""
            SELECT st.studyinstanceuid, st.patient_id, st.study_type,
                   st.studydescription, st.timepoint, st.acquisitiondatetime,
                   {clinical_cols}
            FROM image_study st
            {clinical_join}
            WHERE st.studyinstanceuid = ANY(%s)
            """,
            (list(study_types.keys()),),
        )
        study_rows = cur.fetchall()

    # First pass: proposed study_type per study (thrombectomy anchoring needs it).
    proposed_study_type, by_patient, clinical_by_patient = {}, defaultdict(list), {}
    for row in study_rows:
        proposed, _ = classify_study(
            row["studydescription"], study_types.get(row["studyinstanceuid"])
        )
        proposed_study_type[row["studyinstanceuid"]] = proposed
        by_patient[row["patient_id"]].append(row)
        clinical_by_patient.setdefault(row["patient_id"], {
            "femoral_sheath_time": row["femoral_sheath_time"],
            "receiving_arrival_time": row["receiving_arrival_time"],
            "time_recognized": row["time_recognized"],
        })

    # Second pass: episode-aware timepoints, one call per patient.
    timepoint_by_suid = {}
    for patient_id, rows in by_patient.items():
        patient_studies = [
            {
                "studyinstanceuid": r["studyinstanceuid"],
                "acquisition_datetime": r["acquisitiondatetime"],
                "study_type": proposed_study_type[r["studyinstanceuid"]],
            }
            for r in rows
        ]
        timepoint_by_suid.update(
            assign_patient_timepoints(patient_studies, clinical_by_patient[patient_id])
        )

    studies = []
    for row in study_rows:
        suid = row["studyinstanceuid"]
        proposed, rule = classify_study(
            row["studydescription"], study_types.get(suid)
        )
        tp = timepoint_by_suid[suid]
        studies.append({
            "suid": suid,
            "current": row["study_type"],
            "proposed": proposed,
            "rule": rule,
            "descr": row["studydescription"],
            "current_timepoint": row["timepoint"],
            "episode": tp["episode"],
            "timepoint": tp["timepoint"],
            "timepoint_anchor_source": tp["timepoint_anchor_source"],
            "hours_to_event": tp["hours_to_event"],
            "timepoint_rule": tp["timepoint_rule"],
        })

    _print_study_report(studies)
    _print_timepoint_report(studies)

    if not args.execute:
        print("\nDRY RUN — nothing written. Re-run with --execute to apply.")
        conn.close()
        return 0

    with conn.cursor() as cur:
        psycopg2.extras.execute_batch(
            cur,
            "UPDATE image_series SET series_type = %s, series_type_rule = %s, "
            "series_type_version = %s WHERE seriesinstanceuid = %s",
            [(r["proposed"], r["rule"], RULES_VERSION, r["suid"]) for r in results],
            page_size=500,
        )
        psycopg2.extras.execute_batch(
            cur,
            "UPDATE image_study SET study_type = %s, study_type_version = %s, "
            "episode = %s, timepoint = %s, timepoint_anchor_source = %s, "
            "hours_to_event = %s, timepoint_version = %s "
            "WHERE studyinstanceuid = %s",
            [
                (
                    r["proposed"], RULES_VERSION,
                    r["episode"], r["timepoint"], r["timepoint_anchor_source"],
                    r["hours_to_event"], RULES_VERSION, r["suid"],
                )
                for r in studies
            ],
            page_size=500,
        )
        # Preference rank + the combined display label (NCCT_1, CTA_2, ...).
        # A window over each patient's series, so it runs after the type writes.
        cur.execute(ASSIGN_RANKS_SQL)
        cur.execute(CLEAR_RANKS_SQL)
    conn.commit()
    conn.close()

    print(f"\nWROTE {len(results)} series and {len(studies)} studies "
          f"(rules version = {RULES_VERSION}); ranks + series_label assigned.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
