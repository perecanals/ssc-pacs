"""Study timepoints: the DB side of `assign_patient_timepoints`.

Timepoints, episodes and hours_to_event are properties of a study, computed over
the imaging its owning *enrollment* (``image_study.patient_key``) ingested — not
over the whole person (Alembic 0028): a dataset re-exported with new UIDs
carries copies of the same acquisitions, which must not form one series of
episodes with the originals. A study another enrollment is only a member of
keeps its owner's values. A registered clinical strategy optionally supplies a
subject-level anchor from that enrollment's clinical table.

Shared by ingestion (after each case's upsert), scripts/admin/recompute_timepoints.py,
scripts/admin/reclassify_series_types.py and the patient link/split tools, so they
cannot disagree. psycopg2 cursors (RealDictCursor rows) throughout.
"""

from __future__ import annotations

from collections import defaultdict

from autolabels.runner import refresh_after_ingest
from series_classification import RULES_VERSION, assign_patient_timepoints


def clinical_anchor_sql(cur, study_alias: str = "st"):
    """Project the first registered clinical strategy available for this subject."""
    from clinical_sources import clinical_sources, table_columns
    from psycopg2 import sql

    from autolabels import ANCHORS

    selects, params = [], []
    for source in clinical_sources(cur):
        if not source.strategy:
            continue
        if source.strategy not in ANCHORS:
            raise RuntimeError(f"{source.dataset}: local anchor strategy {source.strategy} is unavailable")
        fields = [key for key, _ in ANCHORS[source.strategy]]
        if set(fields) - table_columns(cur, source.table):
            import logging

            logging.getLogger(__name__).warning("Missing clinical anchor columns in %s", source.table)
            continue
        pairs = [sql.SQL("{},c.{}").format(sql.Literal(key), sql.Identifier(key)) for key in fields]
        selects.append(sql.SQL("""SELECT cq.patient_key,cq.dataset,jsonb_build_object('_strategy',%s::text,{}) AS clocks
            FROM patient cq JOIN {} c ON c.{}::text=cq.patient_id
            WHERE cq.subject_id={}.subject_id AND cq.dataset=%s""").format(
            sql.SQL(",").join(pairs),sql.Identifier(source.table),sql.Identifier(source.id_column),
            sql.Identifier(study_alias)).as_string(cur))
        params.extend([source.strategy,source.dataset])
    if not selects:
        return "NULL::jsonb AS clinical_anchor", "", []
    join = " LEFT JOIN LATERAL (" + " UNION ALL ".join(selects) + " ORDER BY dataset,patient_key LIMIT 1) ca ON TRUE"
    return "ca.clocks AS clinical_anchor", join, params


def resolve_subject_timepoints(study_rows, study_type=None, acquisition=None):
    """Group study rows by owning enrollment and run the episode/timepoint classifier.

    ``study_rows`` carry studyinstanceuid, patient_key (the owner), study_type,
    acquisitiondatetime and the clinical anchor columns. ``study_type`` /
    ``acquisition`` optionally override those per studyinstanceuid (the
    reclassify/recompute scripts propose new values before writing them).
    Returns {studyinstanceuid: assign_patient_timepoints() result}.
    """
    by_owner, clinical = defaultdict(list), {}
    for row in study_rows:
        key = row["patient_key"] or f"study:{row['studyinstanceuid']}"
        by_owner[key].append(row)
        clinical.setdefault(key, row["clinical_anchor"])
    results = {}
    for key, rows in by_owner.items():
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


def recompute_subject_timepoints(cur, subject_ids):
    """Recompute and write the timepoints of every study of the given subjects.

    Uses each study's stored acquisitiondatetime and study_type. Returns the
    number of studies written.
    """
    subject_ids = sorted({s for s in subject_ids if s})
    if not subject_ids:
        return 0
    cols, join, params = clinical_anchor_sql(cur)
    cur.execute(
        f"SELECT st.studyinstanceuid, st.patient_key, st.study_type, "
        f"st.acquisitiondatetime, {cols} "
        f"FROM image_study st {join} WHERE st.subject_id = ANY(%s)",
        params + [subject_ids],
    )
    # Name the columns here rather than rely on the caller's cursor factory:
    # link_patients.py passes a plain tuple cursor, ingestion a RealDictCursor.
    names = [d[0] for d in cur.description]
    results = resolve_subject_timepoints(
        row if isinstance(row, dict) else dict(zip(names, row, strict=True))
        for row in cur.fetchall()
    )
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
    refresh_after_ingest(cur, subject_ids)
    return len(results)
