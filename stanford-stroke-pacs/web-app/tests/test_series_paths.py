"""Tests for the series filesystem-paths endpoint (admin-only).

Server-side paths are operational detail like the zip download —
`GET /api/series/{uid}/paths` is gated by `require_admin` and backs the
copy-path quick actions in the table.
"""

import bcrypt

from db import get_conn

# The seeded series from conftest.seeded_db (has no DICOM path/archive, so
# an authorized request returns null paths — proving the guard passed).
SEEDED_SERIES_UID = "1.2.3.4.5.6"
PATHS_URL = f"/api/series/{SEEDED_SERIES_UID}/paths"


def test_series_paths_requires_login(client):
    """Anonymous users must not see server filesystem paths."""
    resp = client.get(PATHS_URL)
    assert resp.status_code == 401


def test_series_paths_forbidden_for_non_admin(client):
    """A logged-in but non-admin user is rejected with 403."""
    user, pw = "paths_nonadmin", "nonadminpass789"
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            pw_hash = bcrypt.hashpw(pw.encode(), bcrypt.gensalt()).decode()
            cur.execute(
                "INSERT INTO users (username, password_hash, is_admin) "
                "VALUES (%s, %s, false) ON CONFLICT DO NOTHING",
                (user, pw_hash),
            )
        conn.commit()
    finally:
        conn.close()

    login = client.post("/api/login", json={"username": user, "password": pw})
    assert login.status_code == 200

    resp = client.get(PATHS_URL)
    assert resp.status_code == 403


def test_series_paths_allowed_for_admin(logged_in_client):
    """An admin clears the guard; the seeded series has no recorded paths,
    so both fields come back null."""
    resp = logged_in_client.get(PATHS_URL)
    assert resp.status_code == 200
    data = resp.json()
    assert data == {"dicom_dir_path": None, "dicom_archive_path": None}


def test_series_paths_unknown_series_404s(logged_in_client):
    resp = logged_in_client.get("/api/series/9.9.9.9/paths")
    assert resp.status_code == 404
