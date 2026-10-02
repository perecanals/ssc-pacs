"""Persisted modality rollups, migration backfill, and study API compatibility."""

from __future__ import annotations

import importlib.util
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Event
from types import SimpleNamespace

import psycopg2
import pytest
from sqlalchemy import URL, create_engine

from labelled_table_sync import sync_labelled_rows
from study_metadata import lock_study_rows, refresh_study_modalities
from tests.conftest import USER_CRISP, USER_NONE, insert_patient, login_as

PATIENT_ID = "P-MODALITY-025"


@pytest.fixture()
def modality_studies(db_conn):
    uids = ["9.25.1", "9.25.2", "9.25.3"]
    with db_conn.cursor() as cur:
        insert_patient(cur, PATIENT_ID, "crisp2")
        cur.executemany(
            "INSERT INTO image_study (patient_id, studyinstanceuid) VALUES (%s, %s)",
            [(PATIENT_ID, uid) for uid in uids],
        )
        cur.executemany(
            "INSERT INTO image_series (patient_id, studyinstanceuid, seriesinstanceuid, modality) "
            "VALUES (%s, %s, %s, %s)",
            [(PATIENT_ID, study, series, modality) for study, series, modality in
             [(uids[0], "9.25.1.1", " sr "), (uids[0], "9.25.1.2", "ct"),
              (uids[0], "9.25.1.3", "CT"), (uids[0], "9.25.1.4", " "),
              (uids[0], "9.25.1.5", None), (uids[1], "9.25.2.1", "MR")]],
        )
        refresh_study_modalities(cur, uids)
        sync_labelled_rows(db_conn, "study", uids)
    db_conn.commit()
    yield uids
    with db_conn.cursor() as cur:
        cur.execute("DELETE FROM image_series WHERE studyinstanceuid = ANY(%s)", (uids,))
        cur.execute("DELETE FROM image_study_labelled WHERE studyinstanceuid = ANY(%s)", (uids,))
        cur.execute("DELETE FROM image_study WHERE studyinstanceuid = ANY(%s)", (uids,))
        cur.execute("DELETE FROM patient WHERE patient_id = %s", (PATIENT_ID,))
    db_conn.commit()


@pytest.mark.parametrize("endpoint", ["/api/studies", f"/api/patients/crisp2__{PATIENT_ID}/studies"])
def test_modality_display_and_normalization(logged_in_client, modality_studies, endpoint):
    response = logged_in_client.get(endpoint)
    assert response.status_code == 200
    data = response.json()
    rows = data["items"] if isinstance(data, dict) else data
    values = {row["studyinstanceuid"]: row["modality"] for row in rows}
    assert [values[uid] for uid in modality_studies] == ["CT, SR", "MR", ""]


@pytest.mark.parametrize("direction,indices", [("asc", [0, 1, 2]), ("desc", [1, 0, 2])])
def test_modality_sort(logged_in_client, modality_studies, direction, indices):
    response = logged_in_client.get("/api/studies", params={
        "patient_id": PATIENT_ID, "sort_by": "modality", "sort_dir": direction,
    })
    assert response.status_code == 200
    assert [r["studyinstanceuid"] for r in response.json()["items"]] == [
        modality_studies[index] for index in indices
    ]


def test_modality_substring_filter_and_permissions(client, modality_studies):
    login_as(client, USER_CRISP)
    response = client.get("/api/studies", params={"patient_id": PATIENT_ID, "modality": "c"})
    assert response.status_code == 200
    assert [r["studyinstanceuid"] for r in response.json()["items"]] == [modality_studies[0]]
    login_as(client, USER_NONE)
    assert client.get("/api/studies", params={"modality": "CT"}).json()["items"] == []


def test_rollup_clears_unknown_and_rolls_back(db_conn, modality_studies):
    with db_conn.cursor() as cur:
        lock_study_rows(cur, modality_studies)
        cur.execute("UPDATE image_series SET modality = NULL WHERE studyinstanceuid = %s",
                    (modality_studies[0],))
        refresh_study_modalities(cur, modality_studies)
        cur.execute("SELECT modalities FROM image_study WHERE studyinstanceuid = %s",
                    (modality_studies[0],))
        assert cur.fetchone()[0] is None
    db_conn.rollback()
    with db_conn.cursor() as cur:
        cur.execute("SELECT modalities FROM image_study WHERE studyinstanceuid = %s",
                    (modality_studies[0],))
        assert cur.fetchone()[0] == ["CT", "SR"]


def test_concurrent_appends_keep_both_modalities(db_conn, seeded_db, modality_studies):
    uid = modality_studies[0]
    started = Event()

    def append_mr():
        with psycopg2.connect(**seeded_db) as conn:
            with conn.cursor() as cur:
                cur.execute("SET LOCAL statement_timeout = '10s'")
                started.set()
                lock_study_rows(cur, [uid])
                cur.execute(
                    "INSERT INTO image_series (studyinstanceuid, seriesinstanceuid, modality) "
                    "VALUES (%s, '9.25.1.6', 'MR')", (uid,),
                )
                refresh_study_modalities(cur, [uid])

    with db_conn.cursor() as cur:
        lock_study_rows(cur, [uid])
        cur.execute(
            "INSERT INTO image_series (studyinstanceuid, seriesinstanceuid, modality) "
            "VALUES (%s, '9.25.1.7', 'XA')", (uid,),
        )
        with ThreadPoolExecutor(max_workers=1) as executor:
            future = executor.submit(append_mr)
            try:
                assert started.wait(5)
                refresh_study_modalities(cur, [uid])
            finally:
                # Release the parent even on failure so the worker can finish.
                db_conn.commit()
            future.result(timeout=15)
        cur.execute("SELECT modalities FROM image_study WHERE studyinstanceuid = %s", (uid,))
        assert cur.fetchone()[0] == ["CT", "MR", "SR", "XA"]


@pytest.mark.parametrize("mirror_exists", [True, False])
def test_migration_backfill_and_downgrade(seeded_db, mirror_exists):
    path = Path(__file__).resolve().parents[2] / "alembic/versions/0025_study_modalities.py"
    spec = importlib.util.spec_from_file_location("study_modalities_revision", path)
    revision = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(revision)
    engine = create_engine(URL.create(
        "postgresql+psycopg2", username=seeded_db["user"], password=seeded_db["password"],
        host=seeded_db["host"], port=int(seeded_db["port"]), database=seeded_db["dbname"],
    ))
    try:
        with engine.connect() as conn:
            # All DDL and seed rows are rolled back, restoring the session's
            # scratch schema regardless of assertions or migration failures.
            with conn.begin() as transaction:
                revision.op = SimpleNamespace(execute=conn.exec_driver_sql)
                revision.downgrade()
                if not mirror_exists:
                    conn.exec_driver_sql("ALTER TABLE image_study_labelled RENAME TO hidden_study_mirror")
                conn.exec_driver_sql("INSERT INTO image_study (studyinstanceuid) VALUES ('9.25.8'), ('9.25.9')")
                conn.exec_driver_sql(
                    "INSERT INTO image_series (studyinstanceuid, seriesinstanceuid, modality) VALUES "
                    "('9.25.8', '9.25.8.1', ' sr '), ('9.25.8', '9.25.8.2', 'ct'), "
                    "('9.25.8', '9.25.8.3', 'CT'), ('9.25.8', '9.25.8.4', ' '), "
                    "('9.25.8', '9.25.8.5', NULL)"
                )
                if mirror_exists:
                    conn.exec_driver_sql(
                        "INSERT INTO image_study_labelled (studyinstanceuid) VALUES ('9.25.8'), ('9.25.9')"
                    )
                revision.upgrade()
                assert conn.exec_driver_sql(
                    "SELECT modalities FROM image_study WHERE studyinstanceuid IN ('9.25.8', '9.25.9') "
                    "ORDER BY studyinstanceuid"
                ).scalars().all() == [["CT", "SR"], None]
                if mirror_exists:
                    assert conn.exec_driver_sql(
                        "SELECT modalities FROM image_study_labelled WHERE studyinstanceuid = '9.25.8'"
                    ).scalar() == ["CT", "SR"]
                revision.downgrade()
                revision.upgrade()
                assert conn.exec_driver_sql(
                    "SELECT modalities FROM image_study WHERE studyinstanceuid = '9.25.8'"
                ).scalar() == ["CT", "SR"]
                transaction.rollback()
    finally:
        engine.dispose()
