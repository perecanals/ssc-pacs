"""Shared pytest fixtures for the companion backend test suite.

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
# Bootstrap: ensure companion/ and its parent (repo root with config.py)
# are importable, and load .env for DB creds.
# ---------------------------------------------------------------------------
_COMPANION_DIR = Path(__file__).resolve().parent.parent
_REPO_ROOT = _COMPANION_DIR.parent
for p in (_COMPANION_DIR, _REPO_ROOT):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

from dotenv import load_dotenv  # noqa: E402

load_dotenv(_REPO_ROOT / ".env")

# ---------------------------------------------------------------------------
# Test DB name — isolated from the real database.
# ---------------------------------------------------------------------------
TEST_DB_NAME = os.getenv("TEST_DB_NAME", "test_stanford_stroke")

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
    # (see companion/alembic/env.py — it checks this var first).
    from urllib.parse import quote_plus

    db_url = (
        f"postgresql+psycopg2://{quote_plus(_test_dsn['user'])}:{quote_plus(_test_dsn['password'])}"
        f"@{_test_dsn['host']}:{_test_dsn['port']}/{quote_plus(TEST_DB_NAME)}"
    )
    old_url = os.environ.get("DATABASE_URL")
    os.environ["DATABASE_URL"] = db_url

    from alembic.config import Config

    from alembic import command

    cfg = Config(str(_COMPANION_DIR / "alembic.ini"))
    command.upgrade(cfg, "head")

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
            # Minimal reference rows so browsing endpoints don't 500 on empty tables.
            cur.execute(
                "INSERT INTO lvo_clinical_data (study_id, stroke_date) "
                "VALUES ('P-0001', '2025-01-01') ON CONFLICT DO NOTHING"
            )
            cur.execute(
                "INSERT INTO image_study (patient_id, studyinstanceuid, study_type) "
                "VALUES ('P-0001', '1.2.3.4.5', 'CTA') ON CONFLICT DO NOTHING"
            )
            cur.execute(
                "INSERT INTO image_series "
                "(patient_id, studyinstanceuid, seriesinstanceuid, modality, seriesdescription) "
                "VALUES ('P-0001', '1.2.3.4.5', '1.2.3.4.5.6', 'CT', 'Axial') "
                "ON CONFLICT DO NOTHING"
            )
        conn.commit()
    finally:
        conn.close()
    return test_db


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
        app_mod.limiter.enabled = False

        # TestClient uses http://testserver — Secure cookies won't be sent.
        original_cookie_secure = auth_mod.COOKIE_SECURE
        auth_mod.COOKIE_SECURE = False

        from fastapi.testclient import TestClient

        with TestClient(app_mod.app, raise_server_exceptions=False) as tc:
            yield tc

        # Restore originals so module-level state doesn't leak between tests.
        db_mod.DB_CONFIG.update(original_db_config)
        auth_mod.JWT_SECRET = original_jwt
        auth_mod.COOKIE_SECURE = original_cookie_secure
        app_mod.limiter.enabled = True


@pytest.fixture()
def logged_in_client(client):
    """A TestClient that has already logged in as the test user."""
    resp = client.post(
        "/api/login",
        json={"username": TEST_USER, "password": TEST_PASSWORD},
    )
    assert resp.status_code == 200, f"Login failed: {resp.text}"
    return client
