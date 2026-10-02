"""Shared pytest fixtures for the web-app backend test suite.

Creates a scratch PostgreSQL database per test session, runs Alembic
migrations to set up the schema, seeds a test user, and provides
`client` / `logged_in_client` fixtures that talk to the FastAPI app
with DB_CONFIG pointed at the scratch DB.

Requirements:
  - A local Postgres instance with a superuser that can CREATE DATABASE.
  - The connection params are read from the same .env the app uses
    (DB_HOST, DB_PORT, DB_USER, DB_PASSWORD).

In CI the Postgres service container satisfies these requirements
automatically (see .github/workflows/ci.yml).
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from unittest.mock import patch

import bcrypt
import psycopg2
import pytest

# ---------------------------------------------------------------------------
# Bootstrap: ensure web-app/ and its parent (repo root with config.py)
# are importable, and load .env for DB creds.
# ---------------------------------------------------------------------------
_WEB_APP_DIR = Path(__file__).resolve().parent.parent
_REPO_ROOT = _WEB_APP_DIR.parent
for p in (_WEB_APP_DIR, _REPO_ROOT):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

from dotenv import load_dotenv  # noqa: E402

load_dotenv(_REPO_ROOT / ".env")

# ---------------------------------------------------------------------------
# Test DB name — isolated from the real database.
# ---------------------------------------------------------------------------
TEST_DB_NAME = os.getenv("TEST_DB_NAME", "test_stanford_stroke")

# Pin DB_NAME to the scratch DB before any test module imports `db`:
# db.DB_CONFIG is built from os.environ at import time, so without this a
# direct `from db import get_conn` in a test file would lock in the CI/host
# DB_NAME and get_conn() outside the `client` fixture would hit the wrong DB.
os.environ["DB_NAME"] = TEST_DB_NAME

# Connection params for the *admin* connection (to create/drop the test DB).
_admin_dsn = dict(
    host=os.getenv("DB_HOST", "localhost"),
    port=os.getenv("DB_PORT", "5432"),
    user=os.getenv("DB_USER"),
    password=os.getenv("DB_PASSWORD"),
    dbname="postgres",  # connect to default DB for admin ops
)

# Connection params for the *test* database (used by the app under test).
_test_dsn = dict(
    host=os.getenv("DB_HOST", "localhost"),
    port=os.getenv("DB_PORT", "5432"),
    user=os.getenv("DB_USER"),
    password=os.getenv("DB_PASSWORD"),
    dbname=TEST_DB_NAME,
)


# Test-only convenience: many fixtures insert imaging rows the way ingestion did
# before Alembic 0026, naming only patient_id. Ingestion now stamps the owning
# enrollment (patient_key) and its subject; this trigger does the same for such
# fixture rows, from the patient's owner enrollment (the one whose key is its
# subject). Rows that set the columns explicitly are left alone. Production has
# no such trigger — ingestion and scripts/admin/link_patients.py maintain
# ownership, covered by the ingestion suite's scratch DB.
_OWNERSHIP_FILL_SQL = """
CREATE OR REPLACE FUNCTION test_fill_imaging_owner() RETURNS trigger AS $$
BEGIN
    IF NEW.patient_key IS NULL AND NEW.patient_id IS NOT NULL THEN
        SELECT p.patient_key, p.subject_id INTO NEW.patient_key, NEW.subject_id
        FROM patient p
        WHERE p.patient_id = NEW.patient_id AND p.patient_key = p.subject_id
        ORDER BY p.patient_key LIMIT 1;
    END IF;
    RETURN NEW;
END $$ LANGUAGE plpgsql;
CREATE TRIGGER test_fill_study_owner BEFORE INSERT ON image_study
    FOR EACH ROW EXECUTE FUNCTION test_fill_imaging_owner();
CREATE TRIGGER test_fill_series_owner BEFORE INSERT ON image_series
    FOR EACH ROW EXECUTE FUNCTION test_fill_imaging_owner();
"""


def _install_ownership_fill(dsn):
    conn = psycopg2.connect(**dsn)
    try:
        with conn.cursor() as cur:
            cur.execute(_OWNERSHIP_FILL_SQL)
        conn.commit()
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Session-scoped: create the scratch DB, run Alembic migrations, seed data.
# ---------------------------------------------------------------------------
@pytest.fixture(scope="session")
def test_db():
    """Create the test database and apply all Alembic migrations."""
    # Create the DB (autocommit required for CREATE DATABASE).
    admin = psycopg2.connect(**_admin_dsn)
    admin.autocommit = True
    with admin.cursor() as cur:
        cur.execute(f"DROP DATABASE IF EXISTS {TEST_DB_NAME}")
        cur.execute(f"CREATE DATABASE {TEST_DB_NAME}")
    admin.close()

    # Point the Alembic env at the test DB via DATABASE_URL override
    # (see alembic/env.py — it checks this var first).
    from urllib.parse import quote_plus

    db_url = (
        f"postgresql+psycopg2://{quote_plus(_test_dsn['user'])}:{quote_plus(_test_dsn['password'])}"
        f"@{_test_dsn['host']}:{_test_dsn['port']}/{quote_plus(TEST_DB_NAME)}"
    )
    old_url = os.environ.get("DATABASE_URL")
    os.environ["DATABASE_URL"] = db_url

    from alembic import command
    from alembic.config import Config

    cfg = Config(str(_REPO_ROOT / "alembic.ini"))
    command.upgrade(cfg, "head")
    _install_ownership_fill(_test_dsn)

    if old_url is None:
        os.environ.pop("DATABASE_URL", None)
    else:
        os.environ["DATABASE_URL"] = old_url

    yield _test_dsn

    # Teardown: drop the scratch DB.
    admin = psycopg2.connect(**_admin_dsn)
    admin.autocommit = True
    with admin.cursor() as cur:
        # Terminate any lingering connections before dropping.
        cur.execute(
            "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
            f"WHERE datname = '{TEST_DB_NAME}' AND pid <> pg_backend_pid()"
        )
        cur.execute(f"DROP DATABASE IF EXISTS {TEST_DB_NAME}")
    admin.close()


TEST_USER = "testuser"
TEST_PASSWORD = "testpass123"

# Non-admin users with dataset scopes (deny-by-default access control).
# Seeded patients: P-0001 is enrolled in lvo (owning its imaging) and in crisp2
# (a linked enrollment of the same subject); P-0002 only in lvo — so:
#   USER_LVO sees both lvo patients, USER_CRISP sees only crisp2's P-0001 (and
#   P-0001's imaging through the link), USER_NONE (no grants) sees nothing.
USER_LVO = "user_lvo"
USER_CRISP = "user_crisp"
USER_NONE = "user_none"
SCOPED_USERS = {
    USER_LVO: ["lvo"],
    USER_CRISP: ["crisp2"],
    USER_NONE: [],
}


def dataset_slug(name: str) -> str:
    """The registry slug the 0026 migration and manage_datasets.py derive."""
    import re

    return re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")


def insert_patient(cur, patient_id: str, dataset: str, *, subject_id: str | None = None,
                   stroke_date: str | None = None) -> str:
    """Register ``dataset`` if needed and enroll ``patient_id`` in it.

    Returns the patient_key. ``subject_id`` links the enrollment to another
    one's subject (default: its own key — an unlinked person).
    """
    slug = dataset_slug(dataset)
    cur.execute(
        "INSERT INTO dataset (slug, name) VALUES (%s, %s) ON CONFLICT DO NOTHING",
        (slug, dataset),
    )
    key = f"{slug}__{patient_id}"
    cur.execute(
        "INSERT INTO patient (patient_key, subject_id, patient_id, dataset, stroke_date) "
        "VALUES (%s, %s, %s, %s, %s) ON CONFLICT DO NOTHING",
        (key, subject_id or key, patient_id, dataset, stroke_date),
    )
    return key


@pytest.fixture(scope="session")
def seeded_db(test_db):
    """Seed the test DB with a user and minimal reference data."""
    conn = psycopg2.connect(**test_db)
    try:
        with conn.cursor() as cur:
            pw_hash = bcrypt.hashpw(TEST_PASSWORD.encode(), bcrypt.gensalt()).decode()
            cur.execute(
                "INSERT INTO users (username, password_hash, is_admin) "
                "VALUES (%s, %s, true) ON CONFLICT DO NOTHING",
                (TEST_USER, pw_hash),
            )
            for username, datasets in SCOPED_USERS.items():
                cur.execute(
                    "INSERT INTO users "
                    "(username, password_hash, is_admin, allowed_datasets) "
                    "VALUES (%s, %s, false, %s::text[]) ON CONFLICT DO NOTHING",
                    (username, pw_hash, datasets),
                )
            # Minimal reference rows so browsing endpoints don't 500 on empty tables.
            # P-0001: clinically matched. Its clinical stroke_date (2025-01-01)
            # differs from its imaging date (2025-02-02) so we can assert the tab
            # prefers the clinical value via COALESCE.
            cur.execute(
                "INSERT INTO clinical_data (study_id, stroke_date) "
                "VALUES ('P-0001', '2025-01-01') ON CONFLICT DO NOTHING"
            )
            # The machine-derived columns (series_type / timepoint and their
            # provenance) are what the "Auto ..." API fields expose. P-0001's
            # timepoint is anchored on a recorded puncture time; P-0002's below
            # is anchored on an offset, i.e. ESTIMATED.
            cur.execute(
                "INSERT INTO image_study "
                "(patient_id, patient_key, subject_id, studyinstanceuid, study_type, "
                " acquisitiondatetime, "
                " timepoint, timepoint_anchor_source, hours_to_event, timepoint_version) "
                "VALUES ('P-0001', 'lvo__P-0001', 'lvo__P-0001', '1.2.3.4.5', 'CTA', "
                " '2025-02-02', "
                " 'BL', 'femoral_sheath_time', -3.5, 'rules-v1') "
                "ON CONFLICT DO NOTHING"
            )
            cur.execute(
                "INSERT INTO image_series "
                "(patient_id, patient_key, subject_id, studyinstanceuid, seriesinstanceuid, "
                " modality, seriesdescription, "
                " series_type, series_type_rank, series_label, series_type_rule, series_type_version) "
                "VALUES ('P-0001', 'lvo__P-0001', 'lvo__P-0001', '1.2.3.4.5', '1.2.3.4.5.6', "
                " 'CT', 'Axial', "
                " 'NCCT', 1, 'NCCT_1', 'kernel-soft', 'rules-v1') "
                "ON CONFLICT DO NOTHING"
            )
            # P-0002: imaging ingested but NO clinical_data row — the
            # regression fixture. Must still appear in /api/patients, with
            # stroke_date falling back to the earliest study date.
            cur.execute(
                "INSERT INTO image_study "
                "(patient_id, patient_key, subject_id, studyinstanceuid, study_type, "
                " acquisitiondatetime, "
                " timepoint, timepoint_anchor_source, hours_to_event, timepoint_version) "
                "VALUES ('P-0002', 'lvo__P-0002', 'lvo__P-0002', '2.2.2.2.2', 'CTA', "
                " '2024-03-03', "
                " 'FU', 'time_recognized', 26.0, 'rules-v1') "
                "ON CONFLICT DO NOTHING"
            )
            # Patient registry (the patient-level spine): one row per
            # enrollment. Mirrors what the ingest pipeline produces: stroke_date
            # is the imaging-derived MIN. P-0001's crisp2 enrollment is linked to
            # its lvo one (same subject), so the 'crisp2' dataset filter isolates
            # P-0001 and USER_CRISP still reaches P-0001's imaging.
            insert_patient(cur, "P-0001", "lvo", stroke_date="2025-02-02")
            insert_patient(cur, "P-0001", "crisp2", subject_id="lvo__P-0001",
                           stroke_date="2025-02-02")
            insert_patient(cur, "P-0002", "lvo", stroke_date="2024-03-03")
            from study_metadata import refresh_study_modalities

            refresh_study_modalities(cur, ["1.2.3.4.5", "2.2.2.2.2"])
        conn.commit()
    finally:
        conn.close()
    return test_db


@pytest.fixture(autouse=True)
def _unrestricted_clinical_join(monkeypatch):
    """The host config.toml may restrict the clinical join to one dataset
    ([web-app] clinical_data_dataset); tests must not depend on it. Tests of
    the restriction patch it explicitly."""
    import routes.studies as studies_mod

    monkeypatch.setattr(studies_mod, "CLINICAL_DATA_DATASET", None)


# ---------------------------------------------------------------------------
# Per-function fixtures: TestClient with patched DB.
# ---------------------------------------------------------------------------
@pytest.fixture()
def db_conn(seeded_db):
    """Raw psycopg2 connection to the test DB, rolled back after each test."""
    conn = psycopg2.connect(**seeded_db)
    yield conn
    conn.rollback()
    conn.close()


@pytest.fixture()
def client(seeded_db):
    """FastAPI TestClient wired to the scratch test DB.

    Patches DB_CONFIG in db.py (single source of truth) so every
    get_conn() call hits the test DB.  Also stubs JWT_SECRET and
    disables the rate limiter and Secure cookies for TestClient compat.
    """
    env_patch = {
        "DB_USER": seeded_db["user"],
        "DB_PASSWORD": seeded_db["password"],
        "DB_HOST": seeded_db["host"],
        "DB_PORT": seeded_db["port"],
        "DB_NAME": seeded_db["dbname"],
        "JWT_SECRET": "test-jwt-secret-for-ci",
        "ORTHANC_ADMIN_USER": "test",
        "ORTHANC_ADMIN_PASSWORD": "test",
    }
    with patch.dict(os.environ, env_patch):
        import app as app_mod
        import auth as auth_mod
        import db as db_mod

        # Redirect DB connections to the test DB.
        original_db_config = db_mod.DB_CONFIG.copy()
        db_mod.DB_CONFIG.update(seeded_db)

        # Override JWT_SECRET used by the already-imported module.
        original_jwt = auth_mod.JWT_SECRET
        auth_mod.JWT_SECRET = "test-jwt-secret-for-ci"

        # Disable slowapi rate limiting so login-heavy test runs don't 429.
        import rate_limit as rate_limit_mod

        rate_limit_mod.limiter.enabled = False

        # TestClient uses http://testserver — Secure cookies won't be sent.
        original_cookie_secure = auth_mod.COOKIE_SECURE
        auth_mod.COOKIE_SECURE = False

        from fastapi.testclient import TestClient

        # The optional exporter must not start a production-configured worker
        # against the scratch DB. Data Exports tests explicitly enable their fixture.
        from data_exports.settings import Settings
        with patch("data_exports.api.load_settings", return_value=Settings()), TestClient(app_mod.app, raise_server_exceptions=False) as tc:
            yield tc

        # Restore originals so module-level state doesn't leak between tests.
        db_mod.DB_CONFIG.update(original_db_config)
        auth_mod.JWT_SECRET = original_jwt
        auth_mod.COOKIE_SECURE = original_cookie_secure
        rate_limit_mod.limiter.enabled = True


@pytest.fixture()
def logged_in_client(client):
    """A TestClient that has already logged in as the test user."""
    resp = client.post(
        "/api/login",
        json={"username": TEST_USER, "password": TEST_PASSWORD},
    )
    assert resp.status_code == 200, f"Login failed: {resp.text}"
    return client


def login_as(client, username: str):
    """Log the TestClient in as one of the seeded users (shared password)."""
    resp = client.post(
        "/api/login",
        json={"username": username, "password": TEST_PASSWORD},
    )
    assert resp.status_code == 200, f"Login as {username} failed: {resp.text}"
    return client
