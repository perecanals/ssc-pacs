"""Tests for the authentication flow: login, logout, /api/me, JWT sliding refresh."""

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
