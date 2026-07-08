"""SPA fallback contract: unknown /api/* paths 404; SPA routes get index.html."""

from __future__ import annotations


def test_unknown_api_path_returns_404_json(client):
    resp = client.get("/api/definitely-not-a-route")
    assert resp.status_code == 404
    assert resp.json() == {"detail": "Not found"}


def test_bare_api_path_returns_404(client):
    resp = client.get("/api")
    assert resp.status_code == 404


def test_spa_route_serves_index(client, tmp_path, monkeypatch):
    import routes.static as static_mod

    (tmp_path / "index.html").write_text("<html><body>spa</body></html>")
    monkeypatch.setattr(static_mod, "DIST_DIR", tmp_path)

    resp = client.get("/app/some/client/route")
    assert resp.status_code == 200
    assert "spa" in resp.text
