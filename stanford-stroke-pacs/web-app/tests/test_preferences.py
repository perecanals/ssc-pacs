"""Tests for the user preferences API, including the `_global` session-state bucket."""


def test_global_prefs_roundtrip(logged_in_client):
    """The `_global` level stores the Navigator session state (level + sidebar filters)."""
    payload = {
        "prefs": {
            "session": {
                "level": "study",
                "filters": {"modality": "CT", "label": None},
            }
        }
    }
    resp = logged_in_client.put("/api/preferences/_global", json=payload)
    assert resp.status_code == 200
    assert resp.json()["ok"] is True

    resp = logged_in_client.get("/api/preferences/_global")
    assert resp.status_code == 200
    assert resp.json()["prefs"] == payload["prefs"]


def test_prefs_invalid_level_rejected(logged_in_client):
    resp = logged_in_client.get("/api/preferences/bogus")
    assert resp.status_code == 400
    resp = logged_in_client.put("/api/preferences/bogus", json={"prefs": {}})
    assert resp.status_code == 400


def test_get_prefs_anonymous_returns_empty(client):
    resp = client.get("/api/preferences/_global")
    assert resp.status_code == 200
    assert resp.json() == {"prefs": {}}
