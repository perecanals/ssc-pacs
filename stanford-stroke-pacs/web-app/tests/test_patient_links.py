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
    report, touched = link_module["apply_links"](cur, [_row(
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
    link_module["apply_links"](cur, [_row(
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
        link_module["apply_links"](cur, [_row(**row)], "t.csv")


def test_link_rejects_a_source_listed_twice(link_module, cur):
    row = dict(dataset="crisp2", patient_id="OL-2", link_dataset="lvo", link_patient_id="P-0002")
    with pytest.raises(ValueError, match="twice"):
        link_module["apply_links"](cur, [_row(**row), {**_row(**row), "line": 3}], "t.csv")


def _identity():
    return runpy.run_path(
        str(Path(__file__).resolve().parents[2] / "image_ingestion_protocols" / "patient_identity.py")
    )


def test_refresh_follows_what_the_enrollment_sees(link_module, cur, db_conn):
    """Linking is identity only (Alembic 0028): a linked enrollment with no
    imaging of its own gets no stroke date from the other's studies — only from
    a study it is a member of. It gets a patient mirror row either way."""
    refresh = _identity()["refresh_subjects"]
    link_module["apply_links"](cur, [_row(
        dataset="crisp2", patient_id="OL-3", link_dataset="lvo", link_patient_id="P-0002",
    )], "t.csv")
    refresh(cur, db_conn, ["lvo__P-0002"])
    cur.execute("SELECT stroke_date FROM patient WHERE patient_key = 'crisp2__OL-3'")
    assert cur.fetchone()["stroke_date"] is None
    cur.execute("SELECT 1 FROM patient_labelled WHERE patient_key = 'crisp2__OL-3'")
    assert cur.fetchone() is not None
    cur.execute("INSERT INTO study_member VALUES ('2.2.2.2.2', 'crisp2__OL-3')")
    refresh(cur, db_conn, ["lvo__P-0002"])
    cur.execute("SELECT stroke_date::date::text AS d FROM patient WHERE patient_key = 'crisp2__OL-3'")
    assert cur.fetchone()["d"] == "2024-03-03"


def _members(cur):
    cur.execute("SELECT studyinstanceuid, patient_key FROM study_member ORDER BY 1, 2")
    return [(r["studyinstanceuid"], r["patient_key"]) for r in cur.fetchall()]


def test_unlink_drops_memberships_across_the_new_boundary(link_module, cur):
    """crisp2__P-0001 is a member of lvo's study (conftest); unlinked, they are
    two people, so the membership goes."""
    assert ("1.2.3.4.5", "crisp2__P-0001") in _members(cur)
    _identity()["unlink"](cur, "crisp2__P-0001")
    assert ("1.2.3.4.5", "crisp2__P-0001") not in _members(cur)


def test_move_drops_the_new_owners_membership(cur):
    _identity()["move_studies"](cur, ["1.2.3.4.5"], "crisp2__P-0001")
    assert ("1.2.3.4.5", "crisp2__P-0001") not in _members(cur)
    cur.execute("SELECT patient_key FROM image_study WHERE studyinstanceuid = '1.2.3.4.5'")
    assert cur.fetchone()["patient_key"] == "crisp2__P-0001"


def test_refresh_with_a_plain_cursor(link_module, db_conn):
    """link_patients.py --apply drives refresh_subjects with a plain (tuple)
    cursor; the timepoint recompute must not assume dict rows."""
    refresh = runpy.run_path(
        str(Path(__file__).resolve().parents[2] / "image_ingestion_protocols" / "patient_identity.py")
    )["refresh_subjects"]
    with db_conn.cursor() as plain:
        link_module["apply_links"](plain, [_row(
            dataset="crisp2", patient_id="OL-4", link_dataset="lvo", link_patient_id="P-0002",
        )], "t.csv")
        refresh(plain, db_conn, ["lvo__P-0002"])
        plain.execute("SELECT subject_id FROM patient WHERE patient_key = 'crisp2__OL-4'")
        assert plain.fetchone()[0] == "lvo__P-0002"


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


def test_recompute_timepoints_same_with_either_cursor(link_module, db_conn):
    """recompute_subject_timepoints writes the same values whether its caller
    passes a plain or a RealDictCursor."""
    import subject_timepoints  # on sys.path via link_module

    ns = vars(subject_timepoints)
    written = []
    for factory in (None, psycopg2.extras.RealDictCursor):
        with db_conn.cursor(cursor_factory=factory) as c:
            assert ns["recompute_subject_timepoints"](c, ["lvo__P-0002"]) == 1
            c.execute("SELECT episode, timepoint, timepoint_anchor_source, hours_to_event, "
                      "timepoint_version FROM image_study WHERE studyinstanceuid = '2.2.2.2.2'")
            row = c.fetchone()
            written.append(tuple(row.values()) if isinstance(row, dict) else row)
    assert written[0] == written[1]
    assert written[0][-1] == ns["RULES_VERSION"]


def test_link_from_json_file(link_module, cur, tmp_path):
    """The JSON link file (also ingestion's patient_links) links like the CSV."""
    path = tmp_path / "links.json"
    path.write_text('{"dataset": "crisp2", "link_dataset": "lvo", '
                    '"links": {"OL-5": "P-0002"}}', encoding="utf-8")
    rows = link_module["_load"](path, link_module["LINK_COLUMNS"])
    report, touched = link_module["apply_links"](cur, rows, path.name)
    assert _subject(cur, "patient", "patient_key", "crisp2__OL-5") == "lvo__P-0002"
    assert touched == {"lvo__P-0002"}
    # Applying the same file again (a resumed ingestion run) changes nothing.
    report, _ = link_module["apply_links"](cur, rows, path.name)
    assert "already linked" in report[0]


def test_ranks_are_per_owner(link_module, cur):
    """Two enrollments of one person each owning a copy of the same NCCT: both
    are NCCT_1 (Alembic 0028), not NCCT_1 / NCCT_2 across the person."""
    from series_classification import ASSIGN_RANKS_SQL  # on sys.path via link_module

    link_module["apply_links"](cur, [_row(
        dataset="crisp2", patient_id="OL-6", link_dataset="lvo", link_patient_id="P-0002",
    )], "t.csv")
    cur.execute("INSERT INTO image_study (patient_id, patient_key, subject_id, studyinstanceuid) "
                "VALUES ('OL-6', 'crisp2__OL-6', 'lvo__P-0002', '6.6.6.6.6')")
    for key, study, series in (("lvo__P-0002", "2.2.2.2.2", "2.2.2.2.2.1"),
                               ("crisp2__OL-6", "6.6.6.6.6", "6.6.6.6.6.1")):
        cur.execute(
            "INSERT INTO image_series (patient_id, patient_key, subject_id, studyinstanceuid, "
            "seriesinstanceuid, series_type) VALUES ('x', %s, 'lvo__P-0002', %s, %s, 'NCCT')",
            (key, study, series),
        )
        cur.execute("INSERT INTO series_dicom_tags (seriesinstanceuid) VALUES (%s)", (series,))
    cur.execute(ASSIGN_RANKS_SQL)
    cur.execute("SELECT series_label FROM image_series WHERE seriesinstanceuid IN "
                "('2.2.2.2.2.1', '6.6.6.6.6.1')")
    assert [r["series_label"] for r in cur.fetchall()] == ["NCCT_1", "NCCT_1"]
