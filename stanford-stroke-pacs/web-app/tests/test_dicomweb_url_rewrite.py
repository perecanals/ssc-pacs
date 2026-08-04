"""Tests for relativizing absolute Orthanc URLs in DICOMweb JSON responses.

Orthanc's DICOMweb plugin emits absolute BulkDataURI/RetrieveURL values
pointing at itself (http://localhost:8042/...). OHIF follows BulkDataURI
verbatim, so overlay/bulkdata fetches went cross-origin at Orthanc directly:
CORS-blocked, credential-less, and each failure raised the viewer's
"Something went wrong" toast. The proxy rewrites the base to a relative
/dicom-web so those fetches come back through the authenticated proxy.

DB-free by construction: nothing here uses the `client` fixture.
"""

from __future__ import annotations

import sys
from pathlib import Path

import httpx
from starlette.requests import Request
from starlette.responses import Response, StreamingResponse

# Ensure web-app/ is importable.
_WEB_APP_DIR = Path(__file__).resolve().parent.parent
if str(_WEB_APP_DIR) not in sys.path:
    sys.path.insert(0, str(_WEB_APP_DIR))

from routes import proxy  # noqa: E402

METADATA_PATH = "/dicom-web/studies/9.9.9/series/1.2.3/metadata"

# Body shaped like Orthanc's dicom+json metadata output, with the absolute
# base actually configured for this process (ORTHANC_URL), so the test holds
# regardless of the env's host/port.
ABSOLUTE_BASE = proxy._ORTHANC_DICOMWEB_BASE.decode()
METADATA_BODY = (
    '[{"60003000": {"vr": "OW", "BulkDataURI": '
    f'"{ABSOLUTE_BASE}/studies/9.9.9/series/1.2.3/instances/4.5/bulk/60003000"'
    '}, "00081190": {"vr": "UR", "Value": '
    f'["{ABSOLUTE_BASE}/studies/9.9.9"]'
    "}}]"
).encode()


class TestRewriteDicomwebUrls:
    def test_absolute_base_becomes_relative(self):
        out = proxy.rewrite_dicomweb_urls(METADATA_BODY)
        assert proxy._ORTHANC_DICOMWEB_BASE not in out
        assert b'"/dicom-web/studies/9.9.9/series/1.2.3/instances/4.5/bulk/60003000"' in out
        assert b'["/dicom-web/studies/9.9.9"]' in out

    def test_body_without_absolute_urls_is_unchanged(self):
        body = b'[{"0020000D": {"vr": "UI", "Value": ["9.9.9"]}}]'
        assert proxy.rewrite_dicomweb_urls(body) == body


def _make_request(path: str) -> Request:
    return Request({
        "type": "http",
        "method": "GET",
        "scheme": "http",
        "server": ("testserver", 80),
        "path": path,
        "query_string": b"",
        "headers": [],
    })


async def _run_proxy(monkeypatch, path: str, body: bytes, content_type: str):
    async def _body():
        yield body

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, content=_body(), headers={"content-type": content_type}
        )

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    monkeypatch.setattr(proxy, "_CLIENT", client)
    # No cache-state lookups in these tests — the series is never warming.
    monkeypatch.setattr(
        proxy.cache_manager, "get_batch_series_status", lambda uids: {}
    )
    try:
        resp = await proxy._proxy(_make_request(path))
        if isinstance(resp, StreamingResponse):
            chunks = b""
            async for chunk in resp.body_iterator:
                chunks += chunk
            return resp, chunks
        return resp, resp.body
    finally:
        await client.aclose()


class TestProxyJsonRewrite:
    async def test_dicom_json_response_is_rewritten(self, monkeypatch):
        resp, body = await _run_proxy(
            monkeypatch, METADATA_PATH, METADATA_BODY, "application/dicom+json"
        )
        assert isinstance(resp, Response) and not isinstance(resp, StreamingResponse)
        assert proxy._ORTHANC_DICOMWEB_BASE not in body
        assert resp.headers["content-length"] == str(len(body))

    async def test_multipart_frames_stream_untouched(self, monkeypatch):
        frame_path = "/dicom-web/studies/9.9.9/series/1.2.3/instances/4.5/frames/1"
        payload = b"\x00\x01binary"
        resp, body = await _run_proxy(
            monkeypatch, frame_path, payload,
            'multipart/related; type="application/octet-stream"',
        )
        assert isinstance(resp, StreamingResponse)
        assert body == payload

    async def test_non_dicomweb_json_is_untouched(self, monkeypatch):
        payload = b'{"routerBasename": "/ohif"}'
        resp, body = await _run_proxy(
            monkeypatch, "/ohif/app-config.js", payload, "application/json"
        )
        assert isinstance(resp, StreamingResponse)
        assert body == payload
