"""Batch-load only requested inputs, then evaluate the same function on every path."""

from collections import defaultdict
from dataclasses import dataclass

from psycopg2 import sql
from psycopg2.extras import Json, execute_values

from . import RULES
from .model import Study


@dataclass
class Prediction:
    patient_key: str
    studyinstanceuid: str
    general_timepoint: str | None
    value: str | None
    reason: str
    evidence: dict


def tuples(cur):
    return [tuple(row.values()) if isinstance(row, dict) else tuple(row) for row in cur.fetchall()]


def predict(cur, dataset, rule, subjects=None):
    from clinical_sources import clinical_source_for, table_columns

    fields = sql.SQL("'{}'::jsonb")
    join = sql.SQL("")
    source = clinical_source_for(cur, dataset) if rule.clinical_fields else None
    if source:
        columns = table_columns(cur, source.table)
        pairs = [
            sql.SQL("{}, c.{}").format(sql.Literal(k), sql.Identifier(k)) for k in rule.clinical_fields if k in columns
        ]
        fields = sql.SQL("jsonb_build_object({})").format(sql.SQL(", ").join(pairs))
        join = sql.SQL("LEFT JOIN {} c ON c.{}::text=p.patient_id").format(
            sql.Identifier(source.table), sql.Identifier(source.id_column)
        )
    scope = sql.SQL("AND p.subject_id=ANY(%s)") if subjects is not None else sql.SQL("")
    cur.execute(
        sql.SQL("""SELECT p.patient_key, s.studyinstanceuid, s.timepoint,
        s.acquisitiondatetime, s.acquisitiondatetime_source,s.timepoint_anchor_source, {}
        FROM patient p JOIN study_visibility v USING(patient_key)
        JOIN image_study s USING(studyinstanceuid) {} WHERE p.dataset=%s {}""").format(fields, join, scope),
        [dataset] if subjects is None else [dataset, list(subjects)],
    )
    rows = tuples(cur)
    series = defaultdict(list)
    if rule.dicom_fields and rows:
        pairs = [sql.SQL("{}, t.tags -> {}").format(sql.Literal(k), sql.Literal(k)) for k in rule.dicom_fields]
        cur.execute(
            sql.SQL("""SELECT s.studyinstanceuid,jsonb_build_object({})
            FROM image_series s LEFT JOIN series_dicom_tags t USING(seriesinstanceuid)
            WHERE s.studyinstanceuid=ANY(%s) ORDER BY s.seriesinstanceuid""").format(sql.SQL(", ").join(pairs)),
            ([row[1] for row in rows],),
        )
        for uid, tags in tuples(cur):
            series[uid].append(tags)
    for key, uid, general, acquired, clock, anchor, clinical in rows:
        decision = rule.evaluate(Study(general, acquired, clinical, tuple(series[uid])))
        yield Prediction(
            key,
            uid,
            general,
            decision.value,
            decision.reason,
            dict(decision.evidence, clock_source=clock, anchor_source=anchor),
        )


def refresh(cur, dataset=None, subjects=None, output=None):
    # Serialize first activation with incoming studies, even before a registration exists.
    cur.execute(
        """SELECT d.name FROM dataset d WHERE (%s::text IS NULL OR d.name=%s)
           AND (%s::text[] IS NULL OR EXISTS (
               SELECT FROM patient p WHERE p.dataset=d.name AND p.subject_id=ANY(%s)))
           ORDER BY d.name FOR SHARE""",
        (dataset, dataset, None if subjects is None else list(subjects), None if subjects is None else list(subjects)),
    )
    datasets = [name for (name,) in tuples(cur)]
    cur.execute(
        """SELECT dataset, output, rule, version FROM dataset_autolabel_rule
        WHERE dataset=ANY(%s) AND (%s::text IS NULL OR output=%s)
        ORDER BY dataset, output FOR UPDATE""",
        (datasets, output, output),
    )
    for name, slot, rule_name, version in tuples(cur):
        rows = list(predict(cur, name, RULES[rule_name, version], subjects))
        cur.execute(
            """DELETE FROM dataset_autolabel_result r WHERE dataset=%s AND output=%s
            AND NOT EXISTS (SELECT FROM study_visibility v JOIN patient p USING(patient_key)
                WHERE v.studyinstanceuid=r.studyinstanceuid AND v.patient_key=r.patient_key
                AND p.dataset=r.dataset)""",
            (name, slot),
        )
        execute_values(
            cur,
            """INSERT INTO dataset_autolabel_result AS r
            (dataset,output,patient_key,studyinstanceuid,value,reason,evidence) VALUES %s
            ON CONFLICT(patient_key,studyinstanceuid,output) DO UPDATE SET
            value=excluded.value,reason=excluded.reason,evidence=excluded.evidence
            WHERE (r.value,r.reason,r.evidence) IS DISTINCT FROM
                  (excluded.value,excluded.reason,excluded.evidence)""",
            [(name, slot, p.patient_key, p.studyinstanceuid, p.value, p.reason, Json(p.evidence)) for p in rows],
            page_size=500,
        )


def refresh_after_ingest(cur, subjects):
    """Keep the base ingestion transaction usable when an optional rule fails."""
    import logging

    cur.execute("SAVEPOINT autolabel_refresh")
    try:
        refresh(cur, subjects=subjects)
    except Exception:
        cur.execute("ROLLBACK TO SAVEPOINT autolabel_refresh")
        logging.getLogger(__name__).exception(
            "Dataset autolabel refresh failed. Retry affected datasets with manage_autolabels.py run."
        )
    finally:
        cur.execute("RELEASE SAVEPOINT autolabel_refresh")
