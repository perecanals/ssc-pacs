"""Removal proposals must preserve labels and never cross patient identity."""

from __future__ import annotations

import importlib.util
import sys
from copy import deepcopy
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parents[2] / "scripts/data_integrity"
sys.path.insert(0, str(SCRIPTS))
spec = importlib.util.spec_from_file_location("duplicate_series_review", SCRIPTS / "duplicate_series_review.py")
review = importlib.util.module_from_spec(spec)
spec.loader.exec_module(review)


def annotation(label="type", value="CTA", notes=None, created_by="rater"):
    return {"label": label, "value": value, "notes": notes, "created_by": created_by, "id": 1}


@pytest.fixture
def example(monkeypatch):
    state = {uid: {"seriesinstanceuid": uid, "studyinstanceuid": "study-a", "patient_id": "person-a",
                   "patient_key": "a__person-a", "study_patient_id": "person-a", "study_patient_key": "a__person-a",
                   "annotations": [], "study_annotations": [],
                   "enrollments": [{"patient_id": "person-a", "patient_key": "a__person-a"}]}
             for uid in ("series-a", "series-b")}
    results = {uid: {"status": "ok", "sha256": "same-content", "source_signature": "same-source",
                     "version": review.audit.VERIFICATION_VERSION, "dicom_patient_ids": ["person-a"]}
               for uid in state}
    group = {"group_id": "group-a", "seriesdescription": "CTA", "series": deepcopy(list(state.values())),
             "verification": {"matching_sets": [list(state)], "series": results}}
    monkeypatch.setattr(review.audit, "source_signature", lambda row: "same-source")
    return group, state


def decide(example):
    group, state = example
    return review.review_set(group, list(state), state)


def test_keep_labeled_series(example):
    example[1]["series-b"]["annotations"] = [annotation()]
    plan = decide(example)
    assert plan["status"] == "awaiting_approval"
    assert plan["keep_series_uid"] == "series-b"
    assert plan["remove_series_uids"] == ["series-a"]
    assert plan["approval_required"] is True


def test_complementary_labels_are_planned_without_modifying_sources(example):
    example[1]["series-a"]["annotations"] = [annotation("type"), annotation("quality", "good")]
    example[1]["series-b"]["annotations"] = [annotation("reviewed", "false", notes="check this")]
    plan = decide(example)
    assert plan["keep_series_uid"] == "series-a"
    assert plan["status"] == "awaiting_approval_with_label_merge"
    assert plan["label_changes"][0]["value"] == "false"
    assert plan["label_changes"][0]["notes"] == "check this"
    assert len(example[1]["series-a"]["annotations"]) == 2


def test_identical_labels_get_repeatable_random_keeper(example):
    for row in example[1].values():
        row["annotations"] = [annotation()]
    a, b = decide(example), decide(example)
    assert a["keep_series_uid"] == b["keep_series_uid"]
    assert a["decision_id"] == b["decision_id"]
    assert a["label_changes"] == []
    assert a["selection_reason"] == "repeatable_random_tie"


def test_complementary_notes_preserve_sources(example):
    example[1]["series-a"]["annotations"] = [annotation(notes="motion", created_by="alice")]
    example[1]["series-b"]["annotations"] = [annotation(notes="acceptable", created_by="bob")]
    changes = decide(example)["label_changes"]
    assert len(changes) == 1
    assert changes[0]["operation"] == "merge_notes"
    assert all(text in changes[0]["notes"] for text in ("motion", "acceptable", "alice", "bob"))


@pytest.mark.parametrize("other_value", ["NCCT", None, ""])
def test_conflicting_label_values_block_removal(example, other_value):
    example[1]["series-a"]["annotations"] = [annotation()]
    example[1]["series-b"]["annotations"] = [annotation(value=other_value)]
    result = decide(example)
    assert result["status"] == "blocked_conflicting_labels"
    assert result["remove_series_uids"] == []
    assert result["keep_series_uid"] is None
    assert result["label_conflicts"][0]["label"] == "type"


@pytest.mark.parametrize("location", ["series", "study", "member", "dicom"])
def test_different_patient_ids_are_always_protected(example, location):
    group, state = example
    if location == "series":
        state["series-b"]["patient_id"] = "person-b"
    elif location == "study":
        state["series-b"]["study_patient_id"] = "person-b"
    elif location == "member":
        state["series-a"]["enrollments"].append({"patient_id": "person-b"})
    else:
        group["verification"]["series"]["series-b"]["dicom_patient_ids"] = ["person-b"]
    plan = decide(example)
    assert plan["status"] == "protected_different_patient_ids"
    assert plan["remove_series_uids"] == []
    assert plan["label_changes"] == []


def test_missing_identity_is_not_a_match(example):
    example[1]["series-a"]["patient_id"] = None
    assert decide(example)["status"] == "blocked_missing_identity"


def test_changed_files_block_proposals(example, monkeypatch):
    monkeypatch.setattr(review.audit, "source_signature", lambda row: "new-source")
    assert decide(example)["status"] == "blocked_source_changed"


def test_stale_verification_or_different_pixels_cannot_propose_removal(example):
    result = example[0]["verification"]["series"]["series-b"]
    result["version"] = 0
    assert decide(example)["remove_series_uids"] == []
    result["version"] = review.audit.VERIFICATION_VERSION
    result["sha256"] = "different"
    assert decide(example)["remove_series_uids"] == []


def test_cross_study_series_do_not_prove_whole_study_duplication(example):
    example[1]["series-b"]["studyinstanceuid"] = "study-b"
    example[0]["series"][1]["studyinstanceuid"] = "study-b"
    assert decide(example)["status"] == "cross_study_review_required"


def test_changed_study_or_ownership_blocks_existing_proposal(example):
    example[1]["series-b"]["studyinstanceuid"] = "study-b"
    assert decide(example)["status"] == "blocked_context_changed"


def test_unverified_groups_only_add_coverage_count(example):
    group, state = example
    group["verification"] = {"status": "pixel_difference"}
    report = review.build_review({"timestamp": "now", "groups": [group]}, state)
    assert report["summary"]["groups_without_verified_matches"] == 1
    assert report["summary"]["proposed_removals"] == 0
    assert report["applied"] is False


def test_live_state_includes_member_patient_ids_and_parent_manual_labels(test_db, monkeypatch):
    import psycopg2

    import db

    monkeypatch.setattr(db, "DB_CONFIG", test_db)
    with psycopg2.connect(**test_db) as conn, conn.cursor() as cur:
        cur.execute("INSERT INTO dataset(slug,name) VALUES ('rev-a','REVIEW A'),('rev-b','REVIEW B')")
        cur.execute("""INSERT INTO patient(patient_id,patient_key,subject_id,dataset) VALUES
            ('person-a','rev-a__a','rev-a__a','REVIEW A'),('person-b','rev-b__b','rev-a__a','REVIEW B')""")
        cur.execute("""INSERT INTO image_study(studyinstanceuid,patient_id,patient_key,subject_id)
            VALUES ('review-study','person-a','rev-a__a','rev-a__a')""")
        cur.execute("INSERT INTO study_member(studyinstanceuid,patient_key) VALUES ('review-study','rev-b__b')")
        cur.execute("""INSERT INTO image_series(seriesinstanceuid,studyinstanceuid,patient_id,patient_key,subject_id)
            VALUES ('review-series','review-study','person-a','rev-a__a','rev-a__a')""")
        cur.execute("""INSERT INTO annotations(level,seriesinstanceuid,label,value,notes,created_by)
            VALUES ('series','review-series','type','CTA','motion','rater')""")
        cur.execute("""INSERT INTO annotations(level,studyinstanceuid,label,value,created_by)
            VALUES ('study','review-study','reviewed','true','rater')""")
    try:
        state = review.fetch_state(["review-series"])
        row = state["review-series"]
        assert {e["patient_id"] for e in row["enrollments"]} == {"person-a", "person-b"}
        assert row["annotations"][0]["notes"] == "motion"
        assert row["annotations"][0]["created_by"] == "rater"
        assert row["study_annotations"][0]["label"] == "reviewed"
        assert review.fetch_state([]) == {}
    finally:
        with psycopg2.connect(**test_db) as conn, conn.cursor() as cur:
            cur.execute("DELETE FROM annotations WHERE seriesinstanceuid='review-series' OR studyinstanceuid='review-study'")
            cur.execute("DELETE FROM image_series WHERE seriesinstanceuid='review-series'")
            cur.execute("DELETE FROM image_study WHERE studyinstanceuid='review-study'")
            cur.execute("DELETE FROM patient WHERE patient_key IN ('rev-a__a','rev-b__b')")
            cur.execute("DELETE FROM dataset WHERE slug IN ('rev-a','rev-b')")


def test_review_csv_separates_removal_proposals_from_label_conflicts(example, tmp_path, monkeypatch):
    import csv

    group, state = example
    report = {"timestamp": "now", "verification_version": review.audit.VERIFICATION_VERSION, "groups": [group]}
    source = tmp_path / "report.json"
    output = tmp_path / "review"
    review.audit.write_json(source, report)
    monkeypatch.setattr(review, "fetch_state", lambda uids: state)
    monkeypatch.setattr("sys.argv", ["review", "--report", str(source), "--output", str(output)])
    assert review.main() == 0
    with (output / "removals.csv").open() as stream:
        assert len(list(csv.DictReader(stream))) == 1
    state["series-a"]["annotations"] = [annotation(value="CTA")]
    state["series-b"]["annotations"] = [annotation(value="NCCT")]
    assert review.main() == 0
    with (output / "removals.csv").open() as stream:
        assert list(csv.DictReader(stream)) == []
    with (output / "conflicts.csv").open() as stream:
        assert {r["value"] for r in csv.DictReader(stream)} == {"CTA", "NCCT"}
