"""Tests for the /healthz endpoint (WS 06)."""

import json
import string
from unittest.mock import patch


def test_healthz_returns_200_when_healthy(client):
    """When the DB is reachable and Orthanc is stubbed, /healthz returns 200."""
    with patch("routes.admin.orthanc_system_check", return_value=("ok", None)):
        resp = client.get("/healthz")
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "ok"
    assert body["db_stanford_stroke"] == "ok"


def test_healthz_returns_503_when_db_unreachable(client):
    """When the DB is unreachable, /healthz returns 503."""
    import db as db_mod

    original = db_mod.DB_CONFIG.copy()
    db_mod.DB_CONFIG.update({"host": "unreachable-host", "port": "59999"})
    try:
        with patch("routes.admin.orthanc_system_check", return_value=("ok", None)):
            resp = client.get("/healthz")
    finally:
        db_mod.DB_CONFIG.update(original)
    assert resp.status_code == 503
    assert resp.json()["status"] == "degraded"


# ---------------------------------------------------------------------------
# Regression: _GIT_ROOT was computed one level short (stanford-stroke-pacs/
# instead of the outer git root), so /healthz reported version "unknown" and
# reconciliation/latest always 404'd despite reports existing on disk.
# ---------------------------------------------------------------------------


def test_git_root_is_outer_repo_root():
    import routes.admin as admin_mod

    assert (admin_mod._GIT_ROOT / ".git").exists()
    assert admin_mod._REPORTS_DIR == (
        admin_mod._GIT_ROOT / "maintenance" / "reconciliation-reports"
    )


def test_healthz_version_is_git_sha(client):
    """/healthz version must be a 12-char commit SHA, not 'unknown'."""
    import routes.admin as admin_mod

    original = admin_mod._GIT_SHA
    admin_mod._GIT_SHA = None  # drop any cached value from earlier tests
    try:
        with patch("routes.admin.orthanc_system_check", return_value=("ok", None)):
            resp = client.get("/healthz")
    finally:
        admin_mod._GIT_SHA = original
    assert resp.status_code == 200
    version = resp.json()["version"]
    assert len(version) == 12
    assert all(c in string.hexdigits for c in version)


def test_reconciliation_latest_returns_newest_report(logged_in_client, tmp_path, monkeypatch):
    import routes.admin as admin_mod

    (tmp_path / "2026-01-01T000000.json").write_text(json.dumps({"run": "old"}))
    (tmp_path / "2026-07-07T120000.json").write_text(json.dumps({"run": "new"}))
    monkeypatch.setattr(admin_mod, "_REPORTS_DIR", tmp_path)

    resp = logged_in_client.get("/api/admin/reconciliation/latest")
    assert resp.status_code == 200
    assert resp.json() == {"run": "new"}
