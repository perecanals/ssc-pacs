"""Tests for the configurable clinical episode-date column.

`config.toml [web-app] clinical_episode_date_column` picks which clinical_data
column feeds the patient tab's episode date. The value is interpolated into
SQL, so config.py gates it behind a strict-identifier check at import, and
routes/studies.py re-validates it against the live schema once at startup
(falling back to stroke_date with a WARN if the column is missing).
"""

import logging

import psycopg2
import pytest

import config as config_mod
from routes import studies as studies_mod

from .test_patients import _find, _table_hidden


class TestIdentifierValidation:
    """config._require_sql_identifier is the SQL-injection gate."""

    @pytest.mark.parametrize("value,expected", [
        ("stroke_date", "stroke_date"),
        ("enroll_date", "enroll_date"),
        ("_x1", "_x1"),
        ("  Enroll_Date  ", "enroll_date"),  # trimmed + PG-folded to lower
    ])
    def test_valid_identifiers_pass(self, value, expected):
        assert config_mod._require_sql_identifier(value, "k") == expected

    @pytest.mark.parametrize("value", [
        "a-b", "a b", "1col", "", "c; DROP TABLE users", 'a"b',
        "col::text", "c.stroke_date",
    ])
    def test_invalid_identifiers_refuse_startup(self, value):
        with pytest.raises(RuntimeError, match="not a valid SQL identifier"):
            config_mod._require_sql_identifier(value, "k")


class TestResolveClinicalDateColumn:
    """Startup resolution against the live schema."""

    def test_missing_column_falls_back_with_warn(
        self, seeded_db, monkeypatch, caplog
    ):
        monkeypatch.setattr(
            studies_mod, "CLINICAL_EPISODE_DATE_COLUMN", "no_such_column"
        )
        # Patch the module global too so monkeypatch restores it afterwards.
        monkeypatch.setattr(
            studies_mod, "_effective_clinical_date_column", "stroke_date"
        )
        # The session fixture's Alembic run calls fileConfig(), which disables
        # pre-existing loggers; the app re-enables them in configure_logging(),
        # but no app starts in this test.
        monkeypatch.setattr(studies_mod.logger, "disabled", False)
        conn = psycopg2.connect(**seeded_db)
        try:
            with conn.cursor() as cur, caplog.at_level(
                logging.WARNING, logger="routes.studies"
            ):
                assert studies_mod.resolve_clinical_date_column(cur) == "stroke_date"
        finally:
            conn.close()
        assert any("no_such_column" in r.message for r in caplog.records)

    def test_absent_table_keeps_configured_column(
        self, seeded_db, monkeypatch, caplog
    ):
        """With no clinical table there is nothing to validate against: the
        configured (already injection-safe) value is kept, without warning —
        the per-request table_exists guard keeps it out of SQL anyway."""
        monkeypatch.setattr(
            studies_mod, "CLINICAL_EPISODE_DATE_COLUMN", "enroll_date"
        )
        monkeypatch.setattr(
            studies_mod, "_effective_clinical_date_column", "stroke_date"
        )
        conn = psycopg2.connect(**seeded_db)
        try:
            with _table_hidden(seeded_db, "clinical_data"):
                with conn.cursor() as cur, caplog.at_level(
                    logging.WARNING, logger="routes.studies"
                ):
                    assert (
                        studies_mod.resolve_clinical_date_column(cur)
                        == "enroll_date"
                    )
        finally:
            conn.close()
        assert not caplog.records


class TestNonDefaultColumnEndToEnd:
    def test_configured_column_feeds_episode_date(
        self, logged_in_client, seeded_db, monkeypatch
    ):
        """enroll_date (TEXT in the baseline schema) supplies P-0001's date;
        P-0002 (no clinical row) still falls back to its imaging date, and
        sorting exercises the ::text cast path."""
        conn = psycopg2.connect(**seeded_db)
        conn.autocommit = True
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "UPDATE clinical_data SET enroll_date = '2020-09-09' "
                    "WHERE study_id = 'P-0001'"
                )
            monkeypatch.setattr(
                studies_mod, "_effective_clinical_date_column", "enroll_date"
            )

            resp = logged_in_client.get("/api/patients")
            assert resp.status_code == 200
            items = resp.json()["items"]
            assert str(_find(items, "P-0001")["stroke_date"]).startswith("2020-09-09")
            assert str(_find(items, "P-0002")["stroke_date"]).startswith("2024-03-03")

            resp = logged_in_client.get(
                "/api/patients", params={"sort_by": "stroke_date", "sort_dir": "asc"}
            )
            assert resp.status_code == 200
            dates = [
                str(it["stroke_date"]) for it in resp.json()["items"]
                if it["stroke_date"] is not None
            ]
            assert dates == sorted(dates)
        finally:
            # Session-scoped shared scratch DB — undo the committed UPDATE.
            with conn.cursor() as cur:
                cur.execute(
                    "UPDATE clinical_data SET enroll_date = NULL "
                    "WHERE study_id = 'P-0001'"
                )
            conn.close()
