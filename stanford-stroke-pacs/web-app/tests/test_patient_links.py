"""scripts/admin/link_patients.py + scripts/migration/split_merged_patients.py
(subject operations from image_ingestion_protocols/patient_identity.py).

Seeded geometry (conftest.py): P-0001 enrolled in lvo (owns study 1.2.3.4.5,
series 1.2.3.4.5.6) and crisp2 (linked); P-0002 in lvo (owns 2.2.2.2.2).
Every test runs on the rolled-back ``db_conn`` transaction, except the split
dry-run, which rolls itself back.
"""

from __future__ import annotations

import runpy
import sys
from pathlib import Path

import psycopg2.extras
import pytest

from tests.conftest import insert_patient

_SCRIPTS = Path(__file__).resolve().parents[2] / "scripts"


@pytest.fixture(scope="module")
def link_module():
    return runpy.run_path(str(_SCRIPTS / "admin" / "link_patients.py"))


@pytest.fixture()
def cur(db_conn):
    with db_conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as c:
        yield c


def _row(**kw):
    return {"line": 2, **kw}


def _subject(cur, table, key_col, key):
    cur.execute(f"SELECT subject_id FROM {table} WHERE {key_col} = %s", (key,))
    return cur.fetchone()["subject_id"]


def test_link_creates_missing_enrollment_in_target_subject(link_module, cur):
    report, touched = link_module["_link_rows"](cur, [_row(
        dataset="crisp2", patient_id="OL-1", link_dataset="lvo", link_patient_id="P-0002",
    )], "t.csv")
    assert _subject(cur, "patient", "patient_key", "crisp2__OL-1") == "lvo__P-0002"
    assert touched == {"lvo__P-0002"}
    assert "new" in report[0]


def test_link_merges_existing_subject_keeping_owners(link_module, cur):
    key = insert_patient(cur, "P-0009", "crisp2")
    cur.execute(
        "INSERT INTO image_study (patient_id, patient_key, subject_id, studyinstanceuid) "
        "VALUES ('P-0009', %s, %s, '9.0.0.9')",
        (key, key),
    )
    link_module["_link_rows"](cur, [_row(
        dataset="crisp2", patient_id="P-0009", link_dataset="lvo", link_patient_id="P-0002",
    )], "t.csv")
    cur.execute("SELECT patient_key, subject_id FROM image_study WHERE studyinstanceuid = '9.0.0.9'")
    assert dict(cur.fetchone()) == {"patient_key": key, "subject_id": "lvo__P-0002"}


def test_unlink_root_moves_the_rest_to_their_own_subject(link_module, cur):
    link_module["_unlink_rows"](cur, [_row(dataset="lvo", patient_id="P-0001")])
    # lvo keeps its subject (and its imaging); the linked crisp2 enrollment
    # becomes a person of its own, owning nothing.
    assert _subject(cur, "patient", "patient_key", "lvo__P-0001") == "lvo__P-0001"
    assert _subject(cur, "image_series", "seriesinstanceuid", "1.2.3.4.5.6") == "lvo__P-0001"
    assert _subject(cur, "patient", "patient_key", "crisp2__P-0001") == "crisp2__P-0001"


def test_unlink_member_takes_its_owned_imaging(link_module, cur):
    cur.execute(
        "UPDATE image_study SET patient_key = 'crisp2__P-0001' WHERE studyinstanceuid = '1.2.3.4.5'"
    )
    cur.execute(
        "UPDATE image_series SET patient_key = 'crisp2__P-0001' WHERE studyinstanceuid = '1.2.3.4.5'"
    )
    link_module["_unlink_rows"](cur, [_row(dataset="crisp2", patient_id="P-0001")])
    assert _subject(cur, "image_study", "studyinstanceuid", "1.2.3.4.5") == "crisp2__P-0001"
    assert _subject(cur, "image_series", "seriesinstanceuid", "1.2.3.4.5.6") == "crisp2__P-0001"
    assert _subject(cur, "patient", "patient_key", "lvo__P-0001") == "lvo__P-0001"


@pytest.mark.parametrize("row,message", [
    (dict(dataset="lvo", patient_id="P-0002", link_dataset="lvo", link_patient_id="P-0002"),
     "itself"),
    (dict(dataset="crisp2", patient_id="X", link_dataset="lvo", link_patient_id="P-ghost"),
     "not enrolled"),
    (dict(dataset="nope", patient_id="X", link_dataset="lvo", link_patient_id="P-0002"),
     "not registered"),
])
def test_link_rejects_bad_rows(link_module, cur, row, message):
    with pytest.raises(ValueError, match=message):
        link_module["_link_rows"](cur, [_row(**row)], "t.csv")


def test_link_rejects_a_source_listed_twice(link_module, cur):
    row = dict(dataset="crisp2", patient_id="OL-2", link_dataset="lvo", link_patient_id="P-0002")
    with pytest.raises(ValueError, match="twice"):
        link_module["_link_rows"](cur, [_row(**row), {**_row(**row), "line": 3}], "t.csv")


def test_refresh_follows_the_subject(link_module, cur, db_conn):
    """After linking, the new enrollment takes the subject's imaging-derived
    stroke date and gets a patient mirror row."""
    refresh = runpy.run_path(
        str(Path(__file__).resolve().parents[2] / "image_ingestion_protocols" / "patient_identity.py")
    )["refresh_subjects"]
    link_module["_link_rows"](cur, [_row(
        dataset="crisp2", patient_id="OL-3", link_dataset="lvo", link_patient_id="P-0002",
    )], "t.csv")
    refresh(cur, db_conn, ["lvo__P-0002"])
    cur.execute("SELECT stroke_date::date::text AS d FROM patient WHERE patient_key = 'crisp2__OL-3'")
    assert cur.fetchone()["d"] == "2024-03-03"
    cur.execute("SELECT 1 FROM patient_labelled WHERE patient_key = 'crisp2__OL-3'")
    assert cur.fetchone() is not None


def test_split_dry_run_plans_and_rolls_back(client, db_conn, monkeypatch, capsys):
    """The seeded P-0001 is linked lvo+crisp2 with one study; labelled as a
    crisp2 import, the split would hand it to crisp2 and separate them."""
    with db_conn.cursor() as c:
        c.execute("UPDATE image_study SET import_label = 'crisp2_x' WHERE studyinstanceuid = '1.2.3.4.5'")
    db_conn.commit()
    try:
        script = _SCRIPTS / "migration" / "split_merged_patients.py"
        monkeypatch.setattr(sys, "argv", [str(script), "--dataset", "crisp2",
                                          "--import-label-like", "crisp2\\_%"])
        assert runpy.run_path(str(script))["main"]() == 0
        out = capsys.readouterr().out
        assert "crisp2__P-0001: takes 1 studie(s) [crisp2_x] from lvo__P-0001" in out
        assert "DRY RUN" in out
        with db_conn.cursor() as c:
            c.execute("SELECT subject_id FROM patient WHERE patient_key = 'crisp2__P-0001'")
            assert c.fetchone()[0] == "lvo__P-0001"  # unchanged
    finally:
        with db_conn.cursor() as c:
            c.execute("UPDATE image_study SET import_label = NULL WHERE studyinstanceuid = '1.2.3.4.5'")
        db_conn.commit()
