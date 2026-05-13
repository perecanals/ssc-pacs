"""Tests for the authentication flow: login, logout, /api/me, JWT sliding refresh."""

import bcrypt

from db import get_conn
from tests.conftest import TEST_PASSWORD, TEST_USER


def test_login_success(client):
    resp = client.post("/api/login", json={"username": TEST_USER, "password": TEST_PASSWORD})
    assert resp.status_code == 200
    assert resp.json()["username"] == TEST_USER
    assert "auth_token" in resp.cookies


def test_login_failure_wrong_password(client):
    resp = client.post("/api/login", json={"username": TEST_USER, "password": "wrong"})
    assert resp.status_code == 401


def test_login_failure_unknown_user(client):
    resp = client.post("/api/login", json={"username": "nobody", "password": "x"})
    assert resp.status_code == 401


def test_me_authenticated(logged_in_client):
    resp = logged_in_client.get("/api/me")
    assert resp.status_code == 200
    assert resp.json()["username"] == TEST_USER


def test_me_unauthenticated(client):
    resp = client.get("/api/me")
    assert resp.status_code == 200
    assert resp.json()["username"] is None


def test_logout(logged_in_client):
    resp = logged_in_client.post("/api/logout")
    assert resp.status_code == 200
    assert resp.json()["ok"] is True


def test_session_refresh_returns_new_cookie(logged_in_client):
    """Any non-/api/me request should slide the JWT expiry (new cookie)."""
    resp = logged_in_client.get("/api/labels")
    assert resp.status_code == 200
    # The sliding_jwt middleware sets a fresh cookie on every meaningful request.
    assert "auth_token" in resp.cookies


def test_protected_endpoint_requires_auth(client):
    """POST /api/annotations requires a logged-in user."""
    resp = client.post(
        "/api/annotations",
        json={
            "level": "patient",
            "patient_id": "P-0001",
            "label": "test_label",
            "value": "yes",
        },
    )
    assert resp.status_code == 401


def test_login_overrides_existing_session(logged_in_client):
    """Logging in as user B while already logged in as user A must switch identity.

    Regression: previously the sliding_jwt middleware re-issued a cookie based
    on the inbound (user-A) auth_token *after* the login route had set a new
    cookie for user B, leaving the browser stuck on user A.
    """
    # Seed a second user directly in the DB.
    other_user = "testuser_b"
    other_pw = "otherpass456"
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            pw_hash = bcrypt.hashpw(other_pw.encode(), bcrypt.gensalt()).decode()
            cur.execute(
                "INSERT INTO users (username, password_hash, is_admin) "
                "VALUES (%s, %s, false) ON CONFLICT DO NOTHING",
                (other_user, pw_hash),
            )
        conn.commit()
    finally:
        conn.close()

    # Login as user B over the existing user-A session.
    resp = logged_in_client.post(
        "/api/login", json={"username": other_user, "password": other_pw}
    )
    assert resp.status_code == 200
    assert resp.json()["username"] == other_user

    # /api/me should now report user B, not user A.
    me = logged_in_client.get("/api/me")
    assert me.status_code == 200
    assert me.json()["username"] == other_user


def test_logout_actually_clears_session(logged_in_client):
    """After /api/logout, subsequent /api/me must report no user.

    Regression: previously the sliding_jwt middleware re-issued an auth_token
    cookie based on the still-valid inbound token *after* the logout route
    had deleted it.
    """
    resp = logged_in_client.post("/api/logout")
    assert resp.status_code == 200

    me = logged_in_client.get("/api/me")
    assert me.status_code == 200
    assert me.json()["username"] is None
