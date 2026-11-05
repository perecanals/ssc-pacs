#!/usr/bin/env python3
"""Read-only series-family evaluation; never treat missing manual labels as negatives.

Develop on PRECISE and CRISP2/LVO 2-* only. Freeze that implementation before
--cohort test, which excludes shared studies AND subjects from development.
Outputs contain project identifiers; keep them in the local maintenance folder.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import re
import sys
from collections import Counter, defaultdict
from datetime import UTC, datetime
from pathlib import Path

STACK_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(STACK_ROOT / "image_ingestion_protocols"))
from series_classification import RULES_VERSION, classify_series_batch

# Explicit aliases only. Quality/workflow suffixes and spelling guesses are
# reported separately, not silently converted into high-confidence references.
MANUAL_FAMILIES = {
    "dwi": "DWI",
    "adc": "ADC",
    "mrp": "PWI",
    "pwi": "PWI",
    "tof": "MRA",
    "mra": "MRA",
    "flair": "FLAIR",
    "gre": "GRE",
    "swi": "SWI",
    "asl_source": "ASL_SOURCE",
    "asl_cbf": "ASL_CBF",
    "asl_delay": "ASL_DELAY",
    "asl_other": "ASL_OTHER",
    "rapid_summary": "RAPID_SUMMARY",
    "phi": "DOCUMENT",
    "pre": "DSA",
    "post": "DSA",
    "bonewin": "CT_BONE",
    "ncct_contrast": "CECT",
}
TAG_FIELDS = (
    "Modality",
    "SeriesDescription",
    "StudyDescription",
    "ImageType",
    "ConvolutionKernel",
    "ContrastBolusAgent",
    "SliceThickness",
    "AcquisitionTime",
)


def manual_family(value: str | None) -> str | None:
    value = (value or "").strip().lower()
    match = re.fullmatch(r"(ncct|cta|ctp)\d*", value)
    return match[1].upper() if match else MANUAL_FAMILIES.get(value)


def predicted_family(value: str | None) -> str | None:
    return "MRA" if value in {"MRA_TOF", "MRA_CE"} else value or None


def code_digest() -> str:
    digest = hashlib.sha256()
    for path in (
        Path(__file__),
        STACK_ROOT / "image_ingestion_protocols/series_detection.py",
        STACK_ROOT / "image_ingestion_protocols/series_classification.py",
        STACK_ROOT / "image_ingestion_protocols/dicom_tags.py",
        STACK_ROOT / "image_ingestion_protocols/image_ingestion_protocol.py",
        STACK_ROOT / "scripts/admin/reclassify_series_types.py",
    ):
        digest.update(path.read_bytes())
    return digest.hexdigest()


def fetch_rows(cohort: str) -> list[dict]:
    # Use the application's configured endpoint; no libpq defaults or secrets
    # on the command line. The session cannot write even if a query is changed.
    sys.path.insert(0, str(STACK_ROOT / "web-app"))
    import psycopg2
    import psycopg2.extras
    from db import DB_CONFIG

    train = (
        "p.dataset='PRECISE' OR (p.dataset='CRISP2/LVO' AND p.patient_id LIKE '2-%%')"
    )
    condition = "s.studyinstanceuid IN (SELECT studyinstanceuid FROM training_studies)"
    if cohort == "test":
        condition = """NOT EXISTS (SELECT 1 FROM training_studies t WHERE t.studyinstanceuid=s.studyinstanceuid)
        AND NOT EXISTS (SELECT 1 FROM training_subjects t WHERE t.subject_id=s.subject_id)"""
    query = f"""
    WITH training_subjects AS (SELECT DISTINCT p.subject_id FROM patient p WHERE {train}),
    training_studies AS (SELECT DISTINCT sv.studyinstanceuid FROM patient p
      JOIN study_visibility sv USING(patient_key) WHERE {train})
    SELECT s.seriesinstanceuid, s.studyinstanceuid, s.patient_key, s.subject_id,
           p.dataset, s.series_type, s.series_type_version, a.value AS manual,
           t.same_position_count, t.n_instances_scanned,
           (SELECT jsonb_object_agg(k,v) FROM jsonb_each(t.tags) kv(k,v) WHERE k=ANY(%s)) AS tags
    FROM image_series s JOIN patient p ON p.patient_key=s.patient_key
    LEFT JOIN series_dicom_tags t ON t.seriesinstanceuid=s.seriesinstanceuid
    LEFT JOIN annotations a ON a.seriesinstanceuid=s.seriesinstanceuid AND a.level='series' AND a.label='series_type'
    WHERE {condition} ORDER BY s.seriesinstanceuid
    """
    with psycopg2.connect(**DB_CONFIG) as conn:
        conn.set_session(readonly=True, isolation_level="REPEATABLE READ")
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cursor:
            cursor.execute(query, (list(TAG_FIELDS),))
            return [dict(row) for row in cursor.fetchall()]


def evaluate(rows: list[dict]) -> tuple[dict, list[dict]]:
    totals = defaultdict(Counter)
    correct = defaultdict(Counter)
    baseline = defaultdict(Counter)
    predicted = defaultdict(Counter)
    unlabeled_predictions = Counter()
    skipped = Counter()
    confusion = Counter()
    errors = []
    unknown_tags = 0
    predictions = classify_series_batch(rows)
    for row in rows:
        tags = row.get("tags") or {}
        unknown_tags += not bool(tags)
        auto, rule = predictions[row["seriesinstanceuid"]]
        auto = predicted_family(auto)
        manual = row.get("manual")
        target = manual_family(manual)
        if not manual:
            unlabeled_predictions[auto or "NULL"] += 1
            continue
        if target is None:
            skipped[manual] += 1
            continue
        groups = ("all", row.get("dataset") or "unknown")
        for group in groups:
            totals[group][target] += 1
            correct[group][target] += auto == target
            baseline[group][target] += (
                predicted_family(row.get("series_type")) == target
            )
            predicted[group][auto or "NULL"] += 1
        confusion[(target, auto or "NULL")] += 1
        if auto != target:
            errors.append(
                {
                    "patient_key": row.get("patient_key"),
                    "seriesinstanceuid": row["seriesinstanceuid"],
                    "manual": manual,
                    "target": target,
                    "predicted": auto,
                    "rule": rule,
                    "description": tags.get("SeriesDescription"),
                    "kernel": tags.get("ConvolutionKernel"),
                }
            )
    groups = {}
    for group, counts in totals.items():
        groups[group] = {
            target: {
                "labeled": n,
                "correct": correct[group][target],
                "baseline_correct": baseline[group][target],
                "recall_on_labeled": correct[group][target] / n,
                "precision_among_labeled": correct[group][target]
                / predicted[group][target]
                if predicted[group][target]
                else None,
            }
            for target, n in counts.most_common()
        }
    return {
        "series": len(rows),
        "subjects": len({r.get("subject_id") for r in rows}),
        "missing_tags": unknown_tags,
        "groups": groups,
        "excluded_manual_labels": dict(skipped),
        "unlabeled_predictions": dict(unlabeled_predictions),
        "confusion": [
            {"manual_family": m, "predicted": p, "n": n}
            for (m, p), n in sorted(confusion.items())
        ],
        "interpretation": "Recall and precision restricted to clear manual family labels. Unlabeled predictions are unknown, not false positives. Pre/Post normalize to DSA identity only; quality and treatment phase are not predicted.",
    }, errors


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cohort", choices=["development", "test"], required=True)
    parser.add_argument(
        "--snapshot",
        type=Path,
        help="evaluate an existing JSON snapshot instead of querying the DB",
    )
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument(
        "--freeze",
        action="store_true",
        help="record the development code hash for the final test",
    )
    parser.add_argument(
        "--frozen",
        type=Path,
        help="development freeze.json required for final-test evaluation",
    )
    args = parser.parse_args()
    if args.cohort == "test":
        if (
            not args.frozen
            or json.loads(args.frozen.read_text())["code_sha256"] != code_digest()
        ):
            parser.error(
                "final test requires an unchanged implementation and --frozen development/freeze.json"
            )
        if args.freeze:
            parser.error(
                "freeze the implementation on development, not on the final test"
            )
    if args.output.exists():
        parser.error("use a new output directory to preserve previous evaluations")
    rows = (
        json.loads(args.snapshot.read_text())
        if args.snapshot
        else fetch_rows(args.cohort)
    )
    args.output.mkdir(parents=True)
    if not args.snapshot:
        (args.output / "snapshot.json").write_text(json.dumps(rows))
    metrics, errors = evaluate(rows)
    metrics.update(
        cohort=args.cohort,
        rules_version=RULES_VERSION,
        code_sha256=code_digest(),
        evaluated_at=datetime.now(UTC).isoformat(),
    )
    (args.output / "metrics.json").write_text(json.dumps(metrics, indent=2))
    with (args.output / "disagreements.csv").open("w") as stream:
        fields = [
            "patient_key",
            "seriesinstanceuid",
            "manual",
            "target",
            "predicted",
            "rule",
            "description",
            "kernel",
        ]
        writer = csv.DictWriter(stream, fieldnames=fields)
        writer.writeheader()
        writer.writerows(errors)
    if args.freeze:
        (args.output / "freeze.json").write_text(
            json.dumps(
                {
                    "code_sha256": code_digest(),
                    "frozen_at": datetime.now(UTC).isoformat(),
                },
                indent=2,
            )
        )
    print(
        json.dumps(
            {
                "series": metrics["series"],
                "all": metrics["groups"].get("all", {}),
                "excluded_labels": metrics["excluded_manual_labels"],
            },
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
