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


# ---------------------------------------------------------------------------
# must_change_password flow
# ---------------------------------------------------------------------------

def _seed_user_must_change(username: str, password: str) -> None:
    """Insert a user with must_change_password=TRUE."""
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            pw_hash = bcrypt.hashpw(password.encode(), bcrypt.gensalt()).decode()
            cur.execute(
                "INSERT INTO users "
                "(username, password_hash, is_admin, must_change_password) "
                "VALUES (%s, %s, false, TRUE) "
                "ON CONFLICT (username) DO UPDATE "
                "SET password_hash = EXCLUDED.password_hash, "
                "    must_change_password = TRUE, "
                "    password_changed_at = NULL",
                (username, pw_hash),
            )
        conn.commit()
    finally:
        conn.close()


def test_login_response_includes_must_change_flag(client):
    user, pw = "must_change_user", "tempPass123"
    _seed_user_must_change(user, pw)

    resp = client.post("/api/login", json={"username": user, "password": pw})
    assert resp.status_code == 200
    body = resp.json()
    assert body["username"] == user
    assert body["must_change_password"] is True


def test_me_includes_must_change_flag_for_existing_user(logged_in_client):
    """testuser is seeded with must_change_password=FALSE in conftest."""
    resp = logged_in_client.get("/api/me")
    assert resp.status_code == 200
    assert resp.json()["must_change_password"] is False


def test_change_password_happy_path(client):
    user, pw = "change_pw_user", "tempPass123"
    new_pw = "brandNewPass456"
    _seed_user_must_change(user, pw)

    login = client.post("/api/login", json={"username": user, "password": pw})
    assert login.status_code == 200
    assert login.json()["must_change_password"] is True

    resp = client.post(
        "/api/auth/change-password",
        json={"current_password": pw, "new_password": new_pw},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["ok"] is True

    # /api/me now reports the flag cleared.
    me = client.get("/api/me")
    assert me.json()["must_change_password"] is False

    # Password actually changed: old fails, new succeeds.
    client.post("/api/logout")
    fail = client.post("/api/login", json={"username": user, "password": pw})
    assert fail.status_code == 401
    ok = client.post("/api/login", json={"username": user, "password": new_pw})
    assert ok.status_code == 200
    assert ok.json()["must_change_password"] is False


def test_change_password_wrong_current(client):
    user, pw = "change_pw_wrong_current", "tempPass123"
    _seed_user_must_change(user, pw)
    client.post("/api/login", json={"username": user, "password": pw})

    resp = client.post(
        "/api/auth/change-password",
        json={"current_password": "not-it", "new_password": "anotherPass456"},
    )
    assert resp.status_code == 401


def test_change_password_too_short(client):
    user, pw = "change_pw_short", "tempPass123"
    _seed_user_must_change(user, pw)
    client.post("/api/login", json={"username": user, "password": pw})

    resp = client.post(
        "/api/auth/change-password",
        json={"current_password": pw, "new_password": "short"},
    )
    assert resp.status_code == 422


def test_change_password_same_as_current(client):
    user, pw = "change_pw_same", "tempPass123"
    _seed_user_must_change(user, pw)
    client.post("/api/login", json={"username": user, "password": pw})

    resp = client.post(
        "/api/auth/change-password",
        json={"current_password": pw, "new_password": pw},
    )
    assert resp.status_code == 422


def test_must_change_gate_blocks_protected_endpoints(client):
    """While must_change_password=TRUE, non-allowlisted endpoints 403."""
    user, pw = "gated_user", "tempPass123"
    _seed_user_must_change(user, pw)
    client.post("/api/login", json={"username": user, "password": pw})

    # Allowlisted endpoints still work.
    assert client.get("/api/me").status_code == 200

    # Other API endpoints are blocked.
    blocked = client.get("/api/labels")
    assert blocked.status_code == 403
    assert blocked.json()["detail"] == "password_change_required"


def test_must_change_gate_lifts_after_password_change(client):
    """After change-password, the user can hit protected endpoints again."""
    user, pw = "gate_lift_user", "tempPass123"
    new_pw = "brandNewPass456"
    _seed_user_must_change(user, pw)
    client.post("/api/login", json={"username": user, "password": pw})

    resp = client.post(
        "/api/auth/change-password",
        json={"current_password": pw, "new_password": new_pw},
    )
    assert resp.status_code == 200

    after = client.get("/api/labels")
    assert after.status_code == 200


# ---------------------------------------------------------------------------
# Login rate limit (wired via rate_limit.limiter decorating the endpoint)
# ---------------------------------------------------------------------------


def test_login_rate_limit_returns_429(client):
    """The N+1th login attempt within the window must 429 with Retry-After."""
    import rate_limit as rate_limit_mod
    from config import LOGIN_RATE_LIMIT_PER_5MIN

    rate_limit_mod.limiter.enabled = True
    try:
        last = None
        for _ in range(LOGIN_RATE_LIMIT_PER_5MIN + 1):
            last = client.post(
                "/api/login", json={"username": "nobody", "password": "wrong"}
            )
        assert last.status_code == 429, last.text
        assert "Retry-After" in last.headers
        assert last.json() == {"detail": "Too many attempts; please wait and retry."}
    finally:
        rate_limit_mod.limiter.enabled = False
        rate_limit_mod.limiter.reset()
