#!/usr/bin/env python3
"""Prepare an approval-only removal/annotation-merge proposal from verified duplicates.

Reads live ownership, study visibility and manual annotations; writes reports
only. There is deliberately no execute option. Cross-patient matches, conflicts,
changed inputs and cross-study matches cannot produce removal proposals.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import random
import sys
from collections import Counter, defaultdict
from datetime import UTC, datetime
from pathlib import Path

import duplicate_series_audit as audit


def fetch_state(uids: list[str]) -> dict[str, dict]:
    sys.path.insert(0, str(audit.STACK_ROOT / "web-app"))
    import psycopg2
    from db import DB_CONFIG
    from psycopg2.extras import RealDictCursor

    conn = psycopg2.connect(**DB_CONFIG)
    try:
        conn.set_session(readonly=True, isolation_level="REPEATABLE READ")
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("""SELECT s.seriesinstanceuid, s.studyinstanceuid, s.patient_id,
                s.patient_key, s.subject_id, s.number_of_slices, s.dicom_archive_path,
                s.dicom_dir_path, t.n_instances_scanned,
                st.patient_id AS study_patient_id, st.patient_key AS study_patient_key
                FROM image_series s LEFT JOIN image_study st USING(studyinstanceuid)
                LEFT JOIN series_dicom_tags t USING(seriesinstanceuid)
                WHERE s.seriesinstanceuid = ANY(%s)""", (uids,))
            state = {r["seriesinstanceuid"]: dict(r, annotations=[], study_annotations=[], enrollments=[])
                     for r in cur}
            studies = sorted({r["studyinstanceuid"] for r in state.values()})
            cur.execute("""SELECT sv.studyinstanceuid, sv.patient_key, p.patient_id,
                p.dataset, p.subject_id, sv.is_owner
                FROM study_visibility sv JOIN patient p USING(patient_key)
                WHERE sv.studyinstanceuid = ANY(%s) ORDER BY sv.studyinstanceuid, sv.patient_key""", (studies,))
            enrollments = defaultdict(list)
            for row in cur:
                enrollments[row["studyinstanceuid"]].append(dict(row))
            cur.execute("""SELECT id, level, seriesinstanceuid, studyinstanceuid,
                label, value, notes, created_by, created_at FROM annotations
                WHERE (level = 'series' AND seriesinstanceuid = ANY(%s))
                   OR (level = 'study' AND studyinstanceuid = ANY(%s))
                ORDER BY label, id""", (uids, studies))
            study_annotations = defaultdict(list)
            for row in cur:
                if row["level"] == "series":
                    state[row["seriesinstanceuid"]]["annotations"].append(dict(row))
                else:
                    study_annotations[row["studyinstanceuid"]].append(dict(row))
            for row in state.values():
                row["enrollments"] = enrollments[row["studyinstanceuid"]]
                row["study_annotations"] = study_annotations[row["studyinstanceuid"]]
            return state
    finally:
        conn.close()


def label_plan(members: list[dict], keeper: str) -> tuple[list[dict], list[dict]]:
    """Preserve exact values; concatenate distinct notes with source attribution."""
    by_label = defaultdict(list)
    for row in members:
        for annotation in row["annotations"]:
            by_label[annotation["label"]].append(dict(annotation, source_series=row["seriesinstanceuid"]))
    changes, conflicts = [], []
    for label, sources in sorted(by_label.items()):
        if len({audit.canonical(s["value"]) for s in sources}) != 1:
            conflicts.append({"label": label, "sources": sources})
            continue
        target = next((s for s in sources if s["source_series"] == keeper), None)
        notes = []
        for source in sorted(sources, key=lambda s: (s["source_series"] != keeper, s["source_series"])):
            note = source.get("notes")
            if note and note not in [s["notes"] for s in notes]:
                notes.append(source)
        if len(notes) > 1:
            proposed_notes = "\n\n".join(
                f"[From series {s['source_series']}; last editor {s['created_by']}]\n{s['notes']}" for s in notes)
        elif notes:
            proposed_notes = notes[0]["notes"]
        else:
            proposed_notes = target.get("notes") if target else sources[0].get("notes")
        if target is None or target.get("notes") != proposed_notes:
            changes.append({"label": label, "operation": "copy_label" if target is None else "merge_notes",
                            "value": sources[0]["value"], "notes": proposed_notes,
                            "target_series": keeper, "sources": sources})
    return changes, conflicts


def review_set(group: dict, uids: list[str], state: dict[str, dict]) -> dict:
    result = {"decision_id": hashlib.sha256(audit.canonical(sorted(uids)).encode()).hexdigest()[:16],
              "group_id": group["group_id"], "series_uids": sorted(uids),
              "status": "blocked_unverified", "keep_series_uid": None,
              "remove_series_uids": [], "label_changes": [], "label_conflicts": [],
              "approval_required": True, "seriesdescription": group["seriesdescription"]}
    results = group.get("verification", {}).get("series", {})
    originals = {r["seriesinstanceuid"]: r for r in group["series"]}
    if len(set(uids)) < 2 or any(uid not in originals or uid not in results for uid in uids):
        return result
    if any(uid not in state for uid in uids):
        return dict(result, status="blocked_missing_series")
    members = [state[uid] for uid in sorted(uids)]
    result["live_state"] = members
    result["state_digest"] = hashlib.sha256(audit.canonical(members).encode()).hexdigest()
    # Check *all* enrollments that see a study, not just the current owner.
    patient_ids = {r.get(key) for r in members for key in ("patient_id", "study_patient_id")}
    patient_ids.update(e.get("patient_id") for r in members for e in r["enrollments"])
    patient_ids.update(pid for uid in uids for pid in results[uid].get("dicom_patient_ids", []))
    known_ids = {pid for pid in patient_ids if pid and str(pid).strip()}
    result["patient_ids"] = sorted(known_ids)
    if len(known_ids) > 1:
        return dict(result, status="protected_different_patient_ids")
    if len(known_ids) != 1 or any(not pid or not str(pid).strip() for pid in patient_ids):
        return dict(result, status="blocked_missing_identity")
    if any(not r["enrollments"] or not r.get("patient_key")
           or r["patient_key"] != r.get("study_patient_key") for r in members):
        return dict(result, status="blocked_missing_identity")
    if any(r.get(key) != originals[r["seriesinstanceuid"]].get(key)
           for r in members for key in ("studyinstanceuid", "patient_key", "patient_id")):
        return dict(result, status="blocked_context_changed")
    if any(results[uid].get("status") != "ok"
           or results[uid].get("version") != audit.VERIFICATION_VERSION
           or not results[uid].get("dicom_patient_ids") for uid in uids):
        return result
    hashes = {results[uid].get("sha256") for uid in uids}
    if len(hashes) != 1 or not next(iter(hashes)):
        return result
    try:
        if any(audit.source_signature(r) != results[r["seriesinstanceuid"]].get("source_signature") for r in members):
            return dict(result, status="blocked_source_changed")
    except (OSError, ValueError):
        return dict(result, status="blocked_source_changed")
    if len({r["studyinstanceuid"] for r in members}) != 1:
        # Full study duplication and inherited labels require a separate review;
        # identical individual series are not proof of identical whole studies.
        return dict(result, status="cross_study_review_required")
    counts = {r["seriesinstanceuid"]: len(r["annotations"]) for r in members}
    most = max(counts.values())
    tied = sorted(uid for uid, count in counts.items() if count == most)
    # Repeatable random tie-break: no UID-order preference and no rerun churn.
    keeper = random.Random(audit.canonical(sorted(uids))).choice(tied)
    changes, conflicts = label_plan(members, keeper)
    result["manual_label_counts"] = counts
    result["label_conflicts"] = conflicts
    if conflicts:
        return dict(result, status="blocked_conflicting_labels")
    result.update(status="awaiting_approval_with_label_merge" if changes else "awaiting_approval",
                  keep_series_uid=keeper, remove_series_uids=sorted(uid for uid in uids if uid != keeper),
                  label_changes=changes,
                  selection_reason="most_manual_labels" if len(tied) == 1 else "repeatable_random_tie")
    return result


def build_review(report: dict, state: dict[str, dict]) -> dict:
    decisions = []
    unverified = 0
    for group in report["groups"]:
        matching = group.get("verification", {}).get("matching_sets", [])
        if not matching:
            unverified += 1
        decisions.extend(review_set(group, uids, state) for uids in matching)
    return {"schema_version": 1, "timestamp": datetime.now(UTC).isoformat(),
            "source_report_timestamp": report["timestamp"],
            "approval_required": True, "applied": False,
            "summary": {"matching_sets": len(decisions), "statuses": dict(Counter(d["status"] for d in decisions)),
                        "proposed_removals": sum(len(d["remove_series_uids"]) for d in decisions),
                        "proposed_label_changes": sum(len(d["label_changes"]) for d in decisions),
                        "groups_without_verified_matches": unverified},
            "decisions": decisions}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--report", required=True, type=Path, help="Completed pixel verification report.json")
    parser.add_argument("--output", required=True, type=Path, help="Local review directory")
    args = parser.parse_args()
    report = json.loads(args.report.read_text())
    if report.get("verification_version") != audit.VERIFICATION_VERSION:
        parser.error("re-run pixel verification with the current audit tool before preparing removal proposals")
    uids = sorted({uid for g in report["groups"]
                   for matches in g.get("verification", {}).get("matching_sets", []) for uid in matches})
    state = fetch_state(uids)
    review = build_review(report, state)
    args.output.mkdir(parents=True, exist_ok=True)
    audit.write_json(args.output / "review.json", review)
    with (args.output / "review.csv").open("w", newline="") as stream:
        writer = csv.writer(stream)
        writer.writerow(["decision_id", "group_id", "status", "action", "series_uid", "keep_series_uid",
                         "patient_ids", "seriesdescription", "manual_labels", "label_changes", "conflicting_labels"])
        for decision in review["decisions"]:
            for uid in decision["series_uids"]:
                action = "retain_pending_review"
                if uid == decision["keep_series_uid"]:
                    action = "keep"
                elif uid in decision["remove_series_uids"]:
                    action = "remove_only_after_specific_approval"
                writer.writerow([decision["decision_id"], decision["group_id"], decision["status"], action, uid,
                                 decision["keep_series_uid"], ";".join(decision.get("patient_ids", [])),
                                 decision["seriesdescription"], decision.get("manual_label_counts", {}).get(uid, ""),
                                 len(decision["label_changes"]), ";".join(c["label"] for c in decision["label_conflicts"])])
    with (args.output / "removals.csv").open("w", newline="") as stream:
        writer = csv.writer(stream)
        writer.writerow(["decision_id", "series_uid_to_remove", "series_uid_to_keep", "patient_id", "study_uid",
                         "seriesdescription", "approval_required"])
        for decision in review["decisions"]:
            members = {r["seriesinstanceuid"]: r for r in decision.get("live_state", [])}
            for uid in decision["remove_series_uids"]:
                writer.writerow([decision["decision_id"], uid, decision["keep_series_uid"],
                                 members[uid]["patient_id"], members[uid]["studyinstanceuid"],
                                 decision["seriesdescription"], True])
    with (args.output / "conflicts.csv").open("w", newline="") as stream:
        writer = csv.writer(stream)
        writer.writerow(["decision_id", "label", "series_uid", "value", "notes", "last_editor", "annotation_id"])
        for decision in review["decisions"]:
            for conflict in decision["label_conflicts"]:
                for source in conflict["sources"]:
                    writer.writerow([decision["decision_id"], conflict["label"], source["source_series"],
                                     source["value"], source.get("notes"), source["created_by"], source["id"]])
    print(json.dumps(review["summary"], indent=2))
    print(f"Review: {args.output / 'review.csv'}\nDetails: {args.output / 'review.json'}")
    print(f"Removal proposals: {args.output / 'removals.csv'}\nLabel conflicts: {args.output / 'conflicts.csv'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
