"""Tests for DICOM zip download authorization (admin-only).

Bulk DICOM export is a privilege, not a public read like the browsing
endpoints — `GET /api/series/{uid}/dicom-zip` is gated by `require_admin`.
"""

import bcrypt

from db import get_conn

# The seeded series from conftest.seeded_db (has no DICOM path/archive, so
# an authorized request 404s — which still proves the auth guard passed).
SEEDED_SERIES_UID = "1.2.3.4.5.6"
DICOM_ZIP_URL = f"/api/series/{SEEDED_SERIES_UID}/dicom-zip"


def test_dicom_download_requires_login(client):
    """Anonymous users must not be able to pull DICOMs."""
    resp = client.get(DICOM_ZIP_URL)
    assert resp.status_code == 401


def test_dicom_download_forbidden_for_non_admin(client):
    """A logged-in but non-admin user is rejected with 403."""
    user, pw = "downloader_nonadmin", "nonadminpass789"
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

    resp = client.get(DICOM_ZIP_URL)
    assert resp.status_code == 403


def test_dicom_download_allowed_for_admin(logged_in_client):
    """An admin clears the guard (the seeded series has no files on disk,
    so a 404 here is expected and confirms auth was *not* the blocker)."""
    resp = logged_in_client.get(DICOM_ZIP_URL)
    assert resp.status_code not in (401, 403)
    assert resp.status_code == 404
