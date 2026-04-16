"""Tests for the /healthz endpoint (WS 06)."""

from unittest.mock import patch


def test_healthz_returns_200_when_healthy(client):
    """When the DB is reachable and Orthanc is stubbed, /healthz returns 200."""
    # Mock the Orthanc HTTP check to avoid needing a real Orthanc instance.
    with patch("app.http_requests.get") as mock_get:
        mock_get.return_value.status_code = 200
        resp = client.get("/healthz")
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "ok"
    assert body["db_stanford_stroke"] == "ok"


def test_healthz_returns_503_when_db_unreachable(client):
    """When the DB is unreachable, /healthz returns 503."""
    import app as app_mod

    # Temporarily break DB_CONFIG to simulate a connection failure.
    original = app_mod.DB_CONFIG.copy()
    app_mod.DB_CONFIG.update({"host": "unreachable-host", "port": "59999"})
    try:
        with patch("app.http_requests.get") as mock_get:
            mock_get.return_value.status_code = 200
            resp = client.get("/healthz")
    finally:
        app_mod.DB_CONFIG.update(original)
    assert resp.status_code == 503
    assert resp.json()["status"] == "degraded"
