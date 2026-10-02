#!/usr/bin/env python3
"""Recompute acquisition datetimes, episodes, and study timepoints from series_dicom_tags.

Applies `image_ingestion_protocols/series_classification.py` — the SAME logic the
ingestion pipeline runs — to the DICOM tags already stored in `series_dicom_tags`.
No archive I/O: a full corpus pass is a table scan.

Three things, in order:

  1. Rebuild each series' `acquisitiondatetime` (+ `acquisitiondatetime_source`) with
     the Acquisition -> Study precedence (`construct_acquisition_datetime`). ~16% of
     series carry no acquisition tag and fall to the StudyDate encounter clock;
     `acquisitiondatetime_source` records which was used.
  2. A study's `acquisitiondatetime` is the earliest of its series' rebuilt clocks.
  3. Group each person's studies — a subject, which may span several dataset
     enrollments (Alembic 0026) — into episodes (a >45-day inter-study gap starts a
     new one) and label each study BL / THROMBECTOMY / FU against its OWN episode's
     anchor: the clinical puncture for the episode it falls in, else that episode's
     thrombectomy study (`assign_patient_timepoints`). The clinical row is the one
     of the subject's enrollment in config's `clinical_data_dataset`.

Every write stamps `timepoint_version` (RULES_VERSION), so a classification can
always be explained and safely redone. MACHINE-OWNED, and independent of the human
annotation labels (`label_timepoint_*`); this script never reads or writes those.

Dry-run by default — prints what would change (episode counts, timepoint
transitions, anchor sources, datetime-source shifts). Pass --execute to write.
Series with no `series_dicom_tags` row keep their existing datetime; run
maintenance/scripts/backfill_series_dicom_tags.py first for full coverage.

Examples:
    python scripts/admin/recompute_timepoints.py                 # dry-run, whole corpus
    python scripts/admin/recompute_timepoints.py --patient crisp2-lvo__11-004
    python scripts/admin/recompute_timepoints.py --execute
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

from common import subject_for_patient_arg, table_exists  # noqa: E402
from series_classification import (  # noqa: E402
    RULES_VERSION,
    construct_acquisition_datetime,
)
from subject_timepoints import (  # noqa: E402
    clinical_anchor_sql,
    resolve_subject_timepoints,
)

from config import CLINICAL_DATA_DATASET  # noqa: E402


def _fmt(value) -> str:
    if value is None:
        return "<null>"
    if value == "":
        return "<empty>"
    return str(value)


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--label", help="restrict to one import_label")
    parser.add_argument(
        "--patient",
        help="restrict to one patient (all imaging of that person): a patient_key, "
             "or a patient_id that names one person",
    )
    parser.add_argument("--limit", type=int, help="cap the number of subjects (people)")
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

    subject_id = subject_for_patient_arg(args.patient) if args.patient else None
    where, params = ["TRUE"], []
    if args.label:
        where.append("s.import_label = %s")
        params.append(args.label)
    if subject_id:
        where.append("s.subject_id = %s")
        params.append(subject_id)

    conn = psycopg2.connect(**DB_CONFIG)

    # --- 1. Series: rebuild acquisitiondatetime from the tag store ----------
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(
            f"""
            SELECT s.seriesinstanceuid, s.studyinstanceuid,
                   s.acquisitiondatetime AS current_dt, t.tags
            FROM image_series s
            LEFT JOIN series_dicom_tags t USING (seriesinstanceuid)
            WHERE {' AND '.join(where)}
            ORDER BY s.subject_id, s.studyinstanceuid, s.seriesinstanceuid
            """,
            params,
        )
        series_rows = cur.fetchall()

    if not series_rows:
        print("No series matched.")
        conn.close()
        return 1

    series_updates = []           # (new_dt, source, seriesinstanceuid)
    study_series = defaultdict(list)   # studyinstanceuid -> [(new_dt, source)]
    no_tags = 0
    source_counter: Counter = Counter()
    for row in series_rows:
        if row["tags"]:
            new_dt, source = construct_acquisition_datetime(row["tags"])
        else:
            new_dt, source = None, None
            no_tags += 1
        if new_dt is None:
            # No tag row, or nothing parsed — keep the existing clock, source unknown.
            new_dt, source = row["current_dt"], None
        series_updates.append((new_dt, source, row["seriesinstanceuid"]))
        study_series[row["studyinstanceuid"]].append((new_dt, source))
        source_counter[source] += 1

    # --- 2. Study acquisitiondatetime = earliest series clock ---------------
    study_dt, study_dt_source = {}, {}
    for suid, clocks in study_series.items():
        dated = [(dt, src) for dt, src in clocks if dt is not None]
        if dated:
            earliest = min(dated, key=lambda p: p[0])
            study_dt[suid], study_dt_source[suid] = earliest
        else:
            study_dt[suid], study_dt_source[suid] = None, None

    # --- 3. Studies + clinical anchors --------------------------------------
    study_where, study_params = ["TRUE"], []
    if args.label:
        study_where.append("st.import_label = %s")
        study_params.append(args.label)
    if subject_id:
        study_where.append("st.subject_id = %s")
        study_params.append(subject_id)

    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        # clinical_data is optional (a deployment may not have it); without it
        # every anchor reads NULL and each episode falls back to its own
        # thrombectomy study.
        clinical_cols, clinical_join, clinical_params = clinical_anchor_sql(
            table_exists(cur, "clinical_data"), CLINICAL_DATA_DATASET
        )
        cur.execute(
            f"""
            SELECT st.studyinstanceuid, st.subject_id, st.study_type,
                   st.acquisitiondatetime,
                   st.timepoint AS current_timepoint, st.episode AS current_episode,
                   {clinical_cols}
            FROM image_study st
            {clinical_join}
            WHERE {' AND '.join(study_where)}
            ORDER BY st.subject_id, st.studyinstanceuid
            """,
            clinical_params + study_params,
        )
        study_rows = cur.fetchall()

    subject_ids = list(dict.fromkeys(r["subject_id"] for r in study_rows))
    if args.limit:
        keep = set(subject_ids[: args.limit])
        subject_ids = subject_ids[: args.limit]
        study_rows = [r for r in study_rows if r["subject_id"] in keep]

    study_results = resolve_subject_timepoints(study_rows, acquisition=study_dt)
    current_by_suid = {r["studyinstanceuid"]: r for r in study_rows}

    # --- Report -------------------------------------------------------------
    print(f"\n=== DATETIME SOURCE (series, rules {RULES_VERSION}) ===\n")
    for source, count in source_counter.most_common():
        print(f"  {count:8}  {_fmt(source)}")
    if no_tags:
        print(f"\n  {no_tags} series had no series_dicom_tags row — datetime left as-is.")

    print(f"\n=== EPISODES ({len(subject_ids)} subjects) ===\n")
    per_subject_max = defaultdict(int)
    for suid, res in study_results.items():
        sid = current_by_suid[suid]["subject_id"]
        if res["episode"]:
            per_subject_max[sid] = max(per_subject_max[sid], res["episode"])
    multi = {p: n for p, n in per_subject_max.items() if n > 1}
    print(f"  {len(multi)} subject(s) split into >1 episode:")
    for sid, n in sorted(multi.items(), key=lambda kv: -kv[1]):
        print(f"    {sid:24} {n} episodes")

    print(f"\n=== TIMEPOINT ({len(study_results)} studies) ===\n")
    for value, count in Counter(r["timepoint"] for r in study_results.values()).most_common():
        print(f"  {count:8}  {_fmt(value)}")

    print("\n--- anchor source ---")
    for src, count in Counter(
        r["timepoint_anchor_source"] for r in study_results.values()
    ).most_common():
        print(f"  {count:8}  {_fmt(src)}")

    changed = [
        suid for suid, r in study_results.items()
        if r["timepoint"] != current_by_suid[suid]["current_timepoint"]
    ]
    print(f"\n--- timepoint changes: {len(changed)} of {len(study_results)} studies ---")
    transitions = Counter(
        (_fmt(current_by_suid[s]["current_timepoint"]), _fmt(study_results[s]["timepoint"]))
        for s in changed
    )
    for (before, after), count in transitions.most_common(20):
        print(f"  {count:8}  {before:14} -> {after}")

    if not args.execute:
        print("\nDRY RUN — nothing written. Re-run with --execute to apply.")
        conn.close()
        return 0

    # --- Write --------------------------------------------------------------
    with conn.cursor() as cur:
        psycopg2.extras.execute_batch(
            cur,
            "UPDATE image_series SET acquisitiondatetime = %s, "
            "acquisitiondatetime_source = %s WHERE seriesinstanceuid = %s",
            series_updates,
            page_size=1000,
        )
        study_updates = [
            (
                study_dt.get(suid),
                study_dt_source.get(suid),
                res["episode"],
                res["timepoint"],
                res["timepoint_anchor_source"],
                res["hours_to_event"],
                RULES_VERSION,
                suid,
            )
            for suid, res in study_results.items()
        ]
        psycopg2.extras.execute_batch(
            cur,
            "UPDATE image_study SET acquisitiondatetime = %s, "
            "acquisitiondatetime_source = %s, episode = %s, timepoint = %s, "
            "timepoint_anchor_source = %s, hours_to_event = %s, timepoint_version = %s "
            "WHERE studyinstanceuid = %s",
            study_updates,
            page_size=500,
        )
    conn.commit()
    conn.close()

    print(
        f"\nWROTE {len(series_updates)} series and {len(study_results)} studies "
        f"(rules version = {RULES_VERSION})."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
