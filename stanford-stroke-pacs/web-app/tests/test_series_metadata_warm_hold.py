"""Tests for the DICOMweb proxy's series-metadata warm hold.

OHIF requests metadata for every series in a study when it opens; Orthanc
answers from the instance files on disk, so a series mid-extraction from cold
storage 500s and OHIF pops an error toast. The proxy instead holds such a
request while the series is 'queued'/'warming' and forwards once hot.

DB-free by construction: cache_manager.get_batch_series_status is stubbed, so
no Postgres is required.
"""

from __future__ import annotations

import sys
from pathlib import Path

import httpx
from starlette.requests import Request

# Ensure web-app/ is importable.
_WEB_APP_DIR = Path(__file__).resolve().parent.parent
if str(_WEB_APP_DIR) not in sys.path:
    sys.path.insert(0, str(_WEB_APP_DIR))

from routes import proxy  # noqa: E402

SERIES_UID = "1.2.826.0.1.3680043.8.498.1"
METADATA_PATH = f"/dicom-web/studies/9.9.9/series/{SERIES_UID}/metadata"
FRAME_PATH = f"/dicom-web/studies/9.9.9/series/{SERIES_UID}/instances/1/frames/1"


def _status_sequence(monkeypatch, statuses):
    """Stub get_batch_series_status to pop from `statuses` (last one sticks).

    Returns the call-count list so tests can assert how often it polled.
    """
    calls = []

    def fake(series_uids):
        calls.append(list(series_uids))
        status = statuses.pop(0) if len(statuses) > 1 else statuses[0]
        return {uid: status for uid in series_uids}

    monkeypatch.setattr(proxy.cache_manager, "get_batch_series_status", fake)
    monkeypatch.setattr(proxy, "_WARM_WAIT_POLL_SECONDS", 0.001)
    return calls


class TestWaitForSeriesWarm:
    async def test_cold_returns_immediately(self, monkeypatch):
        calls = _status_sequence(monkeypatch, ["cold"])
        await proxy.wait_for_series_warm(SERIES_UID)
        assert calls == [[SERIES_UID]]

    async def test_hot_returns_immediately(self, monkeypatch):
        calls = _status_sequence(monkeypatch, ["hot"])
        await proxy.wait_for_series_warm(SERIES_UID)
        assert calls == [[SERIES_UID]]

    async def test_holds_through_warming_until_hot(self, monkeypatch):
        calls = _status_sequence(monkeypatch, ["queued", "warming", "warming", "hot"])
        await proxy.wait_for_series_warm(SERIES_UID)
        assert len(calls) == 4

    async def test_gives_up_at_deadline(self, monkeypatch):
        _status_sequence(monkeypatch, ["warming"])
        monkeypatch.setattr(proxy, "_WARM_WAIT_MAX_SECONDS", 0.01)
        # Returns rather than raising or hanging — the request then proceeds
        # and worst case reproduces the pre-hold behavior.
        await proxy.wait_for_series_warm(SERIES_UID)


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


async def _run_proxy(monkeypatch, path: str) -> None:
    async def _body():
        yield b"[]"

    def handler(request: httpx.Request) -> httpx.Response:
        # Realistic content-types: metadata/QIDO are dicom+json (buffered by
        # the URL-rewrite branch), frames are multipart (streamed).
        content_type = (
            'multipart/related; type="application/octet-stream"'
            if "/frames/" in path
            else "application/dicom+json"
        )
        return httpx.Response(
            200, content=_body(), headers={"content-type": content_type}
        )

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    monkeypatch.setattr(proxy, "_CLIENT", client)
    try:
        resp = await proxy._proxy(_make_request(path))
        if hasattr(resp, "body_iterator"):
            async for _ in resp.body_iterator:
                pass
    finally:
        await client.aclose()


class TestProxyWarmHold:
    async def test_series_metadata_waits_for_hot(self, monkeypatch):
        calls = _status_sequence(monkeypatch, ["warming", "hot"])
        await _run_proxy(monkeypatch, METADATA_PATH)
        assert len(calls) == 2

    async def test_frame_requests_never_check_cache_state(self, monkeypatch):
        calls = _status_sequence(monkeypatch, ["warming"])
        await _run_proxy(monkeypatch, FRAME_PATH)
        assert calls == []

    async def test_qido_series_list_never_checks_cache_state(self, monkeypatch):
        # The series *list* is index-served and must stay unfiltered/unheld —
        # it is what makes every series visible in OHIF's side panel.
        calls = _status_sequence(monkeypatch, ["warming"])
        await _run_proxy(monkeypatch, "/dicom-web/studies/9.9.9/series")
        assert calls == []
