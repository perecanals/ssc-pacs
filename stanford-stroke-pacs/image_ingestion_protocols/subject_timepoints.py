"""Per-subject study timepoints: the DB side of `assign_patient_timepoints`.

Timepoints, episodes and hours_to_event are properties of a study, computed over
all imaging of one *person* — a subject (Alembic 0026), which may span several
enrollments (the same person in several datasets). The clinical anchor comes
from clinical_data, joined to the subject's enrollment in the dataset whose ids
clinical_data carries (config.toml [web-app] clinical_data_dataset).

Shared by ingestion (after each case's upsert), scripts/admin/recompute_timepoints.py,
scripts/admin/reclassify_series_types.py and the patient link/split tools, so they
cannot disagree. psycopg2 cursors (RealDictCursor rows) throughout.
"""

from __future__ import annotations

from collections import defaultdict

from series_classification import RULES_VERSION, assign_patient_timepoints

CLINICAL_ANCHOR_COLUMNS = ("femoral_sheath_time", "receiving_arrival_time", "time_recognized")


def clinical_anchor_sql(has_clinical_table: bool, clinical_dataset: str | None, study_alias: str = "st"):
    """(select_columns, join, params) adding the clinical anchor columns to a
    query over ``image_study {study_alias}``.

    One clinical row per study: the first (by patient_key) enrollment of the
    study's subject that has one — restricted to ``clinical_dataset`` when set,
    because clinical_data.study_id has no dataset of its own. Without the table
    every anchor reads NULL, the shape a patient with no clinical row yields.
    """
    if not has_clinical_table:
        cols = ", ".join(f"NULL::text AS {c}" for c in CLINICAL_ANCHOR_COLUMNS)
        return cols, "", []
    restrict, params = "", []
    if clinical_dataset:
        restrict, params = " AND cq.dataset = %s", [clinical_dataset]
    join = (
        "LEFT JOIN LATERAL ("
        f"  SELECT {', '.join('c.' + c for c in CLINICAL_ANCHOR_COLUMNS)} "
        "  FROM patient cq JOIN clinical_data c ON c.study_id = cq.patient_id "
        f"  WHERE cq.subject_id = {study_alias}.subject_id{restrict} "
        "  ORDER BY cq.patient_key LIMIT 1"
        ") ca ON TRUE"
    )
    return ", ".join(f"ca.{c}" for c in CLINICAL_ANCHOR_COLUMNS), join, params


def resolve_subject_timepoints(study_rows, study_type=None, acquisition=None):
    """Group study rows by subject and run the episode/timepoint classifier.

    ``study_rows`` carry studyinstanceuid, subject_id, study_type,
    acquisitiondatetime and the clinical anchor columns. ``study_type`` /
    ``acquisition`` optionally override those per studyinstanceuid (the
    reclassify/recompute scripts propose new values before writing them).
    Returns {studyinstanceuid: assign_patient_timepoints() result}.
    """
    by_subject, clinical = defaultdict(list), {}
    for row in study_rows:
        key = row["subject_id"] or f"study:{row['studyinstanceuid']}"
        by_subject[key].append(row)
        clinical.setdefault(key, {c: row[c] for c in CLINICAL_ANCHOR_COLUMNS})
    results = {}
    for key, rows in by_subject.items():
        studies = [
            {
                "studyinstanceuid": r["studyinstanceuid"],
                "acquisition_datetime": (acquisition or {}).get(
                    r["studyinstanceuid"], r["acquisitiondatetime"]
                ),
                "study_type": (study_type or {}).get(r["studyinstanceuid"], r["study_type"]),
            }
            for r in rows
        ]
        results.update(assign_patient_timepoints(studies, clinical[key]))
    return results


def recompute_subject_timepoints(cur, subject_ids, *, has_clinical_table, clinical_dataset):
    """Recompute and write the timepoints of every study of the given subjects.

    Uses each study's stored acquisitiondatetime and study_type. Returns the
    number of studies written.
    """
    subject_ids = sorted({s for s in subject_ids if s})
    if not subject_ids:
        return 0
    cols, join, params = clinical_anchor_sql(has_clinical_table, clinical_dataset)
    cur.execute(
        f"SELECT st.studyinstanceuid, st.subject_id, st.study_type, "
        f"st.acquisitiondatetime, {cols} "
        f"FROM image_study st {join} WHERE st.subject_id = ANY(%s)",
        params + [subject_ids],
    )
    results = resolve_subject_timepoints(cur.fetchall())
    for suid, res in results.items():
        cur.execute(
            "UPDATE image_study SET episode = %s, timepoint = %s, "
            "timepoint_anchor_source = %s, hours_to_event = %s, timepoint_version = %s "
            "WHERE studyinstanceuid = %s",
            (
                res["episode"], res["timepoint"], res["timepoint_anchor_source"],
                res["hours_to_event"], RULES_VERSION, suid,
            ),
        )
    return len(results)
