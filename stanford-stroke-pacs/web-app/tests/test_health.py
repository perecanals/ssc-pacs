"""Tests for the /healthz endpoint (WS 06)."""

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
