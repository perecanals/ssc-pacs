#!/usr/bin/env python3
"""Preview, activate, recompute and export enrollment-specific study labels."""

import argparse
import csv
import json
import os
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(ROOT / "image_ingestion_protocols"), str(ROOT / "web-app")]

from autolabels import RULES  # noqa: E402
from autolabels.runner import predict, refresh, tuples  # noqa: E402
from db import get_conn  # noqa: E402
from psycopg2.extras import RealDictCursor  # noqa: E402


def preview(cur, dataset, rule, directory):
    cur.execute(
        """SELECT a.studyinstanceuid,upper(trim(a.value))
        FROM annotations a JOIN study_visibility v USING(studyinstanceuid)
        JOIN patient p ON p.patient_key=v.patient_key WHERE p.dataset=%s AND a.level='study'
        AND a.label='timepoint' AND upper(trim(a.value)) IN ('BLO','BLT')""",
        (dataset,),
    )
    refs = dict(tuples(cur))
    counts = Counter()
    predictions = list(predict(cur, dataset, rule))
    for p in predictions:
        counts["studies"] += 1
        counts[f"value/{p.value}"] += 1
        counts[f"reason/{p.reason}"] += 1
        ref = refs.get(p.studyinstanceuid)
        if ref and p.value in ("BLO", "BLT") and ref != p.value:
            counts["hospital_conflicts"] += 1
        if ref and p.value == "PRE":
            counts["hospital_reference_predicted_PRE"] += 1
        if p.general_timepoint == "BL" and rule.output == "effective_timepoint":
            source = p.evidence.get("recognition_source") or "missing"
            counts[f"baseline_recognition/{source}"] += 1
        if p.reason == "direct_presentation" and not ref:
            counts["direct_without_exact_reference"] += 1
    if rule.output == "effective_timepoint":
        for p in predictions:
            ref = refs.get(p.studyinstanceuid)
            arrival = p.evidence.get("arrival_minutes")
            if ref and arrival is not None and ref != ("BLO" if arrival < 0 else "BLT"):
                counts["arrival_boundary_conflicts"] += 1
    if directory:
        directory.mkdir(mode=0o700, parents=True, exist_ok=False)
        for conflicts, filename in ((False, "predictions.csv"), (True, "conflicts.csv")):
            with (directory / filename).open("x", newline="") as stream:
                writer = csv.writer(stream)
                writer.writerow(
                    [
                        "patient_key",
                        "studyinstanceuid",
                        "general_timepoint",
                        rule.output,
                        "reason",
                        "evidence",
                        "reference",
                        "rule",
                        "version",
                    ]
                )
                for p in predictions:
                    ref = refs.get(p.studyinstanceuid)
                    if conflicts and not (
                        rule.output == "effective_timepoint" and ref and p.value in ("BLO", "BLT") and p.value != ref
                    ):
                        continue
                    writer.writerow(
                        [
                            p.patient_key,
                            p.studyinstanceuid,
                            p.general_timepoint,
                            p.value,
                            p.reason,
                            json.dumps(p.evidence, sort_keys=True),
                            ref,
                            rule.name,
                            rule.version,
                        ]
                    )
        (directory / "coverage.json").write_text(json.dumps(counts, indent=2, sort_keys=True) + "\n")
    print(json.dumps(counts, indent=2, sort_keys=True))


def export(cur, args):
    cur.execute(
        """SELECT r.patient_key,r.studyinstanceuid,s.timepoint AS general_timepoint,
        r.output,r.value,r.reason,g.rule,g.version,r.evidence,a.value AS reference
        FROM dataset_autolabel_result r JOIN dataset_autolabel_rule g USING(dataset,output)
        JOIN study_visibility v USING(patient_key,studyinstanceuid)
        JOIN image_study s USING(studyinstanceuid)
        LEFT JOIN annotations a ON a.studyinstanceuid=s.studyinstanceuid
        AND a.level='study' AND a.label='timepoint'
        WHERE r.dataset=%s AND (%s::text IS NULL OR g.rule=%s)
        AND (NOT %s OR (r.output='effective_timepoint' AND r.value IN ('BLO','BLT')
            AND upper(trim(a.value)) IN ('BLO','BLT') AND upper(trim(a.value))<>r.value))
        ORDER BY r.patient_key,r.studyinstanceuid,r.output""",
        (args.dataset, args.rule, args.rule, args.conflicts),
    )
    with open(args.file, "x", newline="", opener=lambda p, f: os.open(p, f, 0o600)) as stream:
        writer = csv.writer(stream)
        writer.writerow([d.name for d in cur.description])
        for row in cur:
            writer.writerow([json.dumps(v, sort_keys=True) if isinstance(v, dict) else v for v in row.values()])


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    for cmd in ("list", "preview", "enable", "run", "export", "disable"):
        sub = commands.add_parser(cmd)
        sub.add_argument("--dataset", required=cmd != "list")
        if cmd != "list":
            sub.add_argument("--rule", required=cmd in ("preview", "enable", "disable"))
        if cmd in ("preview", "enable"):
            sub.add_argument("--version", required=True, type=int)
        if cmd == "preview":
            sub.add_argument("--directory", type=Path, help="New local directory for predictions, conflicts and counts")
        if cmd == "export":
            sub.add_argument("--file", required=True, type=Path)
            sub.add_argument("--conflicts", action="store_true")
    args = parser.parse_args()
    conn = get_conn()
    try:
        with conn, conn.cursor(cursor_factory=RealDictCursor) as cur:
            if args.dataset:
                lock = " FOR UPDATE" if args.command in ("enable", "disable") else ""
                cur.execute("SELECT 1 FROM dataset WHERE name=%s" + lock, (args.dataset,))
                if not cur.fetchone():
                    parser.error("Unknown dataset")
            if args.command == "list":
                print("Available:", ", ".join(f"{n} v{v}" for n, v in RULES))
                cur.execute(
                    "SELECT * FROM dataset_autolabel_rule WHERE %s::text IS NULL OR dataset=%s",
                    (args.dataset, args.dataset),
                )
                print(json.dumps(cur.fetchall(), indent=2))
            elif args.command in ("preview", "enable"):
                rule = RULES.get((args.rule, args.version))
                if not rule:
                    parser.error("Unknown rule version")
                if args.command == "preview":
                    preview(cur, args.dataset, rule, args.directory)
                    return
                cur.execute(
                    """INSERT INTO dataset_autolabel_rule AS g(dataset,output,rule,version)
                    VALUES (%s,%s,%s,%s) ON CONFLICT(dataset,output) DO UPDATE SET version=excluded.version
                    WHERE g.rule=excluded.rule RETURNING output""",
                    (args.dataset, rule.output, rule.name, rule.version),
                )
                if not cur.fetchone():
                    parser.error("Output already has another active rule")
                refresh(cur, args.dataset, output=rule.output)
            elif args.command == "export":
                export(cur, args)
            elif args.command == "disable":
                cur.execute(
                    "DELETE FROM dataset_autolabel_rule WHERE dataset=%s AND rule=%s", (args.dataset, args.rule)
                )
            else:
                output = None
                if args.rule:
                    cur.execute(
                        "SELECT output FROM dataset_autolabel_rule WHERE dataset=%s AND rule=%s",
                        (args.dataset, args.rule),
                    )
                    row = cur.fetchone()
                    if not row:
                        parser.error("Rule is not enabled")
                    output = row["output"]
                refresh(cur, args.dataset, output=output)
    finally:
        conn.close()


if __name__ == "__main__":
    main()
