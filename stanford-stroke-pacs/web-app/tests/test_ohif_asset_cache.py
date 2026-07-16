"""Tests for long-lived caching of OHIF's content-hashed assets.

Orthanc serves the OHIF build with no Cache-Control/ETag/Last-Modified, so
without this the browser re-downloads ~21 MiB on every viewer open. The proxy
stamps `immutable` onto content-hashed artefacts only; unhashed siblings keep
their names across rebuilds and must stay uncached.

DB-free by construction: nothing here uses the `client` fixture, so no Postgres
is required. Importing routes.proxy / app only reads env via require_env().
"""

from __future__ import annotations

import sys
from pathlib import Path

import httpx
import pytest
from starlette.requests import Request
from starlette.responses import Response

# Ensure web-app/ is importable.
_WEB_APP_DIR = Path(__file__).resolve().parent.parent
if str(_WEB_APP_DIR) not in sys.path:
    sys.path.insert(0, str(_WEB_APP_DIR))

import app as app_mod  # noqa: E402
import auth as auth_mod  # noqa: E402
from routes import proxy  # noqa: E402

# ---------------------------------------------------------------------------
# Real asset names, measured from production journald logs. This table doubles
# as the record of what OHIF actually serves.
# ---------------------------------------------------------------------------

HASHED_PATHS = [
    "/ohif/app.bundle.b34f32c50e70ee27ad26.js",
    "/ohif/213.bundle.a60dd2d3c7182b17cb7b.js",
    "/ohif/31fb9346313fc3740d7b.woff2",
    "/ohif/031089e563a18ada8441.wasm",
]

UNHASHED_PATHS = [
    "/ohif/app.bundle.css",
    "/ohif/app-config.js",
    "/ohif/init-service-worker.js",
    "/ohif/manifest.js",
    "/ohif/manifest.json",
    "/ohif/assets/favicon.ico",
    "/ohif/147.css",
    "/ohif/414.css",
    "/ohif/",
    "/ohif/viewer",
]

# Hash-shaped but outside /ohif/ — the prefix gate must reject these.
OUT_OF_SCOPE_PATHS = [
    "/dicom-web/studies/1.2.3/series/4.5.6/instances/7/frames/1",
    "/assets/index-b34f32c50e70ee27ad26.js",
    "/api/studies",
]


class TestIsImmutableOhifAsset:
    @pytest.mark.parametrize("path", HASHED_PATHS)
    def test_contenthashed_assets_are_immutable(self, path):
        assert proxy.is_immutable_ohif_asset(path) is True

    @pytest.mark.parametrize("path", UNHASHED_PATHS)
    def test_unhashed_assets_are_not(self, path):
        assert proxy.is_immutable_ohif_asset(path) is False

    @pytest.mark.parametrize("path", OUT_OF_SCOPE_PATHS)
    def test_paths_outside_ohif_are_not(self, path):
        assert proxy.is_immutable_ohif_asset(path) is False


# ---------------------------------------------------------------------------
# _proxy header application, via httpx.MockTransport against the module-level
# client (_get_client reads _CLIENT at call time, so monkeypatching suffices).
# ---------------------------------------------------------------------------


def _make_request(path: str) -> Request:
    """Minimal ASGI scope — _proxy never reads the body on GET."""
    return Request({
        "type": "http",
        "method": "GET",
        "scheme": "http",
        "server": ("testserver", 80),
        "path": path,
        "query_string": b"",
        "headers": [],
    })


async def _proxy_response_headers(monkeypatch, path: str, status: int = 200):
    """Run _proxy against a stubbed upstream and return the response headers."""
    async def _body():
        yield b"x"

    def handler(request: httpx.Request) -> httpx.Response:
        # An async-generator body keeps the stream unconsumed, which is what
        # _proxy's aiter_raw() needs; content=b"..." would mark it read.
        return httpx.Response(
            status, content=_body(), headers={"content-type": "text/plain"}
        )

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    monkeypatch.setattr(proxy, "_CLIENT", client)
    try:
        resp = await proxy._proxy(_make_request(path))
        # Drain the body: BackgroundTask(upstream.aclose) never fires outside a
        # real ASGI cycle, and httpx warns on an unclosed response.
        async for _ in resp.body_iterator:
            pass
        return resp.headers
    finally:
        await client.aclose()


class TestProxyCacheControl:
    async def test_hashed_asset_gets_immutable_cache_control(self, monkeypatch):
        headers = await _proxy_response_headers(
            monkeypatch, "/ohif/app.bundle.b34f32c50e70ee27ad26.js"
        )
        assert headers["cache-control"] == "private, max-age=31536000, immutable"

    async def test_unhashed_asset_gets_no_cache_control(self, monkeypatch):
        headers = await _proxy_response_headers(monkeypatch, "/ohif/app.bundle.css")
        assert "cache-control" not in headers

    async def test_non_200_is_not_cached(self, monkeypatch):
        headers = await _proxy_response_headers(
            monkeypatch, "/ohif/app.bundle.b34f32c50e70ee27ad26.js", status=404
        )
        assert "cache-control" not in headers

    async def test_dicomweb_frame_is_untouched(self, monkeypatch):
        headers = await _proxy_response_headers(
            monkeypatch,
            "/dicom-web/studies/1.2.3/series/4.5.6/instances/7/frames/1",
        )
        assert "cache-control" not in headers


# ---------------------------------------------------------------------------
# sliding_jwt must not Set-Cookie on cacheable assets — but must keep sliding
# everywhere else.
# ---------------------------------------------------------------------------


async def _sliding_jwt_headers(path: str):
    """Call the sliding_jwt middleware directly with a valid auth cookie.

    @app.middleware("http") returns the function unchanged, so it is callable
    without a TestClient (which would need a live DB and a live Orthanc).
    """
    token = auth_mod.create_jwt("testuser")
    request = Request({
        "type": "http",
        "method": "GET",
        "scheme": "http",
        "server": ("testserver", 80),
        "path": path,
        "query_string": b"",
        "headers": [(b"cookie", f"auth_token={token}".encode())],
    })

    async def call_next(_request):
        return Response("ok")

    resp = await app_mod.sliding_jwt(request, call_next)
    return resp.headers


class TestSlidingJwtSkip:
    async def test_no_set_cookie_on_cacheable_asset(self):
        headers = await _sliding_jwt_headers("/ohif/app.bundle.b34f32c50e70ee27ad26.js")
        assert "set-cookie" not in headers

    async def test_still_slides_on_api_calls(self):
        # Positive control: catches an over-broad predicate silently killing
        # session sliding across the rest of the app.
        headers = await _sliding_jwt_headers("/api/studies")
        assert "set-cookie" in headers

    async def test_still_slides_on_dicomweb_frames(self):
        # Frame fetches are what keep a session alive during a long viewing
        # session, now that asset fetches no longer do.
        headers = await _sliding_jwt_headers(
            "/dicom-web/studies/1.2.3/series/4.5.6/instances/7/frames/1"
        )
        assert "set-cookie" in headers
