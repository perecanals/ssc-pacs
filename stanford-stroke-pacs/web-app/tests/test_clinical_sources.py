"""Per-dataset clinical tables (Alembic 0027): episode date, registry, import.

Seeded (conftest.py): lvo's clinical table `lvo_clinical_data` (study_id ->
stroke_date '2025-01-01' for P-0001), registered on the lvo dataset row. P-0001
is also enrolled (linked) in crisp2, which has no clinical table.
"""

from __future__ import annotations

import argparse
import logging
import runpy
from pathlib import Path

import psycopg2
import pytest

import clinical_sources
from db import get_conn
from schema_scope import include_object

_SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "admin" / "manage_datasets.py"


def _q(sql, params=()):
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(sql, params)
            rows = cur.fetchall() if cur.description else None
        conn.commit()
        return rows
    finally:
        conn.close()


def _dates(client):
    items = client.get("/api/patients", params={"patient_id": "P-0001"}).json()["items"]
    return {r["patient_key"]: str(r["stroke_date"])[:10] for r in items}


@pytest.fixture()
def crisp2_table(client):
    """A second dataset's clinical table, with a *date* column (Excel-style)."""
    _q("CREATE TABLE crisp2_clinical_data (rid text PRIMARY KEY, onset date)")
    _q("INSERT INTO crisp2_clinical_data VALUES ('P-0001', '2019-05-05')")
    _q("UPDATE dataset SET clinical_table = 'crisp2_clinical_data', clinical_id_column = 'rid', "
       "clinical_date_column = 'onset' WHERE name = 'crisp2'")
    yield
    _q("UPDATE dataset SET clinical_table = NULL, clinical_id_column = NULL, "
       "clinical_date_column = NULL WHERE name = 'crisp2'")
    _q("DROP TABLE crisp2_clinical_data")


class TestEpisodeDate:
    def test_each_enrollment_uses_its_own_dataset_table(self, logged_in_client, crisp2_table):
        assert _dates(logged_in_client) == {
            "lvo__P-0001": "2025-01-01",     # lvo's table (text column)
            "crisp2__P-0001": "2019-05-05",  # crisp2's table (date column)
        }

    def test_sort_and_filter_follow_the_episode_date(self, logged_in_client, crisp2_table):
        rows = logged_in_client.get(
            "/api/patients", params={"sort_by": "stroke_date", "sort_dir": "asc"}
        ).json()["items"]
        dates = [str(r["stroke_date"]) for r in rows if r["stroke_date"]]
        assert dates == sorted(dates)
        hit = logged_in_client.get("/api/patients", params={"stroke_date": "2019-05"}).json()
        assert [r["patient_key"] for r in hit["items"]] == ["crisp2__P-0001"]

    def test_registered_date_column_is_used(self, logged_in_client):
        _q("UPDATE lvo_clinical_data SET enroll_date = '2020-09-09' WHERE study_id = 'P-0001'")
        _q("UPDATE dataset SET clinical_date_column = 'enroll_date' WHERE name = 'lvo'")
        try:
            assert _dates(logged_in_client)["lvo__P-0001"] == "2020-09-09"
        finally:
            _q("UPDATE dataset SET clinical_date_column = 'stroke_date' WHERE name = 'lvo'")
            _q("UPDATE lvo_clinical_data SET enroll_date = NULL WHERE study_id = 'P-0001'")

    def test_misregistered_column_is_ignored_with_a_warning(
        self, logged_in_client, monkeypatch, caplog
    ):
        monkeypatch.setattr(clinical_sources, "_warned", set())
        monkeypatch.setattr(clinical_sources.logger, "disabled", False)
        _q("UPDATE dataset SET clinical_date_column = 'no_such_column' WHERE name = 'lvo'")
        try:
            with caplog.at_level(logging.WARNING, logger="clinical_sources"):
                assert _dates(logged_in_client)["lvo__P-0001"] == "2025-02-02"  # imaging
        finally:
            _q("UPDATE dataset SET clinical_date_column = 'stroke_date' WHERE name = 'lvo'")
        assert any("no_such_column" in r.getMessage() for r in caplog.records)

    def test_registry_constraints(self, client):
        for update in (
            "clinical_table = 'not_suffixed'",
            "clinical_table = 'x_clinical_data'",  # others NULL: all-or-none
            "timepoint_strategy = 'made_up'",
        ):
            with pytest.raises(psycopg2.errors.CheckViolation):
                _q(f"UPDATE dataset SET {update} WHERE name = 'crisp2'")


class TestAutogenerateScope:
    @pytest.mark.parametrize("name", [
        "patient", "clinical_data", "precise_clinical_data", "crisp2_lvo_clinical_data",
        "patient_labelled",
    ])
    def test_upstream_and_clinical_tables_skipped(self, name):
        assert include_object(None, name, "table", True, None) is False

    def test_owned_tables_kept(self):
        assert include_object(None, "annotations", "table", True, None) is True
        assert include_object(None, "dataset", "table", True, None) is True


@pytest.fixture()
def mod(client):
    return runpy.run_path(str(_SCRIPT))


def _args(**kw):
    defaults = dict(dataset="crisp2", file=None, from_table=None, sheet=0, id_column="Patient ID",
                    date_column="Onset Date", timepoint_strategy=None, replace=False,
                    execute=False)
    return argparse.Namespace(**{**defaults, **kw})


def _run(mod, **kw):
    conn = get_conn()
    try:
        return mod["_import_clinical"](conn, _args(**kw))
    finally:
        conn.close()


@pytest.fixture()
def csv_file(tmp_path):
    path = tmp_path / "crisp2.csv"
    path.write_text("Patient ID,Onset Date,Score\nP-0001,2018-03-03,4\nX-404,2018-04-04,2\n")
    return path


@pytest.fixture()
def cleanup_crisp2():
    yield
    _q("UPDATE dataset SET clinical_table = NULL, clinical_id_column = NULL, "
       "clinical_date_column = NULL, timepoint_strategy = NULL WHERE name = 'crisp2'")
    _q("DROP TABLE IF EXISTS crisp2_clinical_data")


@pytest.mark.usefixtures("cleanup_crisp2")
class TestImportClinical:
    def test_dry_run_reports_and_writes_nothing(self, mod, csv_file, capsys):
        assert _run(mod, file=csv_file) == 0
        out = capsys.readouterr().out
        assert "table:                   crisp2_clinical_data" in out
        assert "id / date columns:       patient_id / onset_date" in out
        assert "1 of 1 enrolled patient(s) have a row" in out
        assert "ids not enrolled:        1 (e.g. X-404)" in out
        assert _q("SELECT to_regclass('crisp2_clinical_data')") == [(None,)]

    def test_execute_uploads_registers_and_feeds_the_patient_tab(
        self, mod, csv_file, logged_in_client
    ):
        assert _run(mod, file=csv_file, execute=True) == 0
        assert _q("SELECT clinical_table, clinical_id_column, clinical_date_column "
                  "FROM dataset WHERE name = 'crisp2'") == [
            ("crisp2_clinical_data", "patient_id", "onset_date")]
        assert _q("SELECT count(*) FROM crisp2_clinical_data") == [(2,)]
        assert _dates(logged_in_client)["crisp2__P-0001"] == "2018-03-03"
        # An existing table needs --replace.
        with pytest.raises(SystemExit, match="--replace"):
            _run(mod, file=csv_file, execute=True)
        assert _run(mod, file=csv_file, execute=True, replace=True) == 0

    def test_blocking_problems_write_nothing(self, mod, tmp_path, capsys):
        bad = tmp_path / "dupes.csv"
        bad.write_text("Patient ID,Onset Date\nP-0001,2018-03-03\nP-0001,2018-03-04\n")
        assert _run(mod, file=bad, execute=True) == 1
        assert "appear more than once: P-0001" in capsys.readouterr().out
        assert _q("SELECT to_regclass('crisp2_clinical_data')") == [(None,)]

    def test_unknown_columns_and_strategy_requirements(self, mod, csv_file):
        with pytest.raises(SystemExit, match="no column"):
            _run(mod, file=csv_file, date_column="Missing")
        with pytest.raises(SystemExit, match="crisp2_puncture needs"):
            _run(mod, file=csv_file, timepoint_strategy="crisp2_puncture")

    def test_adopt_leaves_a_compatibility_view(self, mod):
        """Adopting a table named clinical_data renames it and leaves a view
        under the old name, so existing queries keep working."""
        _q("ALTER TABLE clinical_data RENAME TO clinical_data_parked")
        _q("CREATE TABLE clinical_data (study_id text, stroke_date text)")
        _q("INSERT INTO clinical_data VALUES ('P-0001', '2017-07-07')")
        try:
            assert _run(mod, from_table="clinical_data", id_column="study_id",
                        date_column="stroke_date", execute=True) == 0
            assert _q("SELECT relkind FROM pg_class WHERE relname = 'clinical_data'") == [("v",)]
            assert _q("SELECT stroke_date FROM clinical_data") == [("2017-07-07",)]
        finally:
            kind = _q("SELECT relkind FROM pg_class WHERE relname = 'clinical_data'")
            _q("DROP VIEW clinical_data" if kind == [("v",)] else "DROP TABLE IF EXISTS clinical_data")
            _q("ALTER TABLE clinical_data_parked RENAME TO clinical_data")

    def test_clear_unregisters_but_keeps_the_table(self, mod, csv_file):
        _run(mod, file=csv_file, execute=True)
        conn = get_conn()
        try:
            mod["_clear_clinical"](conn, "crisp2", True)
        finally:
            conn.close()
        assert _q("SELECT clinical_table FROM dataset WHERE name = 'crisp2'") == [(None,)]
        assert _q("SELECT count(*) FROM crisp2_clinical_data") == [(2,)]
