"""Tests for the /healthz endpoint (WS 06)."""

import json
import string
from dataclasses import replace
from unittest.mock import patch

import pytest

import archive_volumes
import config
from tests.volume_helpers import archive_volumes_at


@pytest.fixture(autouse=True)
def _scratch_volumes(tmp_path):
    """Healthz reads the configured archive volumes: use scratch ones (disk1
    online) rather than whatever this host's config.toml points at."""
    with archive_volumes_at(tmp_path / "disk1", tmp_path / "disk2") as vols:
        yield vols


def _with(vols, **changes):
    """Patch the configured volume set with per-volume field changes."""
    new = {n: replace(v, **changes.get(n, {})) for n, v in vols.items()}
    archive_volumes.invalidate_status_cache()
    return patch.object(config, "ARCHIVE_VOLUMES", new)


def test_healthz_returns_200_when_healthy(client):
    """When the DB is reachable and Orthanc is stubbed, /healthz returns 200."""
    with patch("routes.admin.orthanc_system_check", return_value=("ok", None)):
        resp = client.get("/healthz")
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "ok"
    assert body["db_stanford_stroke"] == "ok"


def test_healthz_reports_archive_volumes(client, _scratch_volumes):
    with patch("routes.admin.orthanc_system_check", return_value=("ok", None)):
        body = client.get("/healthz").json()
    assert body["archive_volumes"]["disk1"]["online"] is True
    assert body["archive_volumes"]["disk1"]["free_bytes"] > 0
    assert "warnings" not in body


def test_healthz_optional_volume_offline_warns_but_stays_up(client, _scratch_volumes, monkeypatch):
    (_scratch_volumes["disk2"].root / archive_volumes.SENTINEL_NAME).unlink()
    monkeypatch.setattr(config, "STORAGE_MODE", "cold_path_cache")
    import routes.admin as admin_mod
    monkeypatch.setattr(admin_mod, "STORAGE_MODE", "cold_path_cache")
    archive_volumes.invalidate_status_cache()
    with patch("routes.admin.orthanc_system_check", return_value=("ok", None)):
        resp = client.get("/healthz")
    assert resp.status_code == 200
    body = resp.json()
    assert body["archive_volumes"]["disk2"]["online"] is False
    assert any("disk2" in w for w in body["warnings"])


def test_healthz_required_volume_offline_is_degraded(client, _scratch_volumes, monkeypatch):
    (_scratch_volumes["disk2"].root / archive_volumes.SENTINEL_NAME).unlink()
    import routes.admin as admin_mod
    monkeypatch.setattr(admin_mod, "STORAGE_MODE", "cold_path_cache")
    with _with(_scratch_volumes, disk2={"required": True}), \
         patch("routes.admin.orthanc_system_check", return_value=("ok", None)):
        resp = client.get("/healthz")
    assert resp.status_code == 503
    assert resp.json()["status"] == "degraded"


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
