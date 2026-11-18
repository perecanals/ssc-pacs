"""Expanding an OHIF study queues warming through authorized QIDO traffic."""

import asyncio
from unittest.mock import AsyncMock, Mock

import httpx
import pytest
from fastapi import FastAPI, HTTPException
from starlette.requests import Request
from starlette.responses import Response

from routes import proxy

UID = "1.2.3"
PATH = f"/dicom-web/studies/{UID}/series"


def request(path=PATH, method="GET"):
    return Request(
        {
            "type": "http",
            "method": method,
            "path": path,
            "scheme": "http",
            "server": ("testserver", 80),
            "query_string": b"",
            "headers": [],
        }
    )


@pytest.fixture
def warming(monkeypatch):
    monkeypatch.setattr(proxy, "STORAGE_MODE", "cold_path_cache")
    status = Mock(return_value={UID: "cold"})
    warm = AsyncMock()
    monkeypatch.setattr(proxy.cache_manager, "get_batch_cache_status", status)
    monkeypatch.setattr(proxy, "api_warm_study", warm)
    return status, warm


@pytest.mark.parametrize("state", ["cold", "error"])
async def test_cold_or_failed_study_queues_without_waiting_for_extraction(warming, state):
    status, warm = warming
    status.return_value = {UID: state}
    req = request()
    await proxy.queue_sidebar_study_warm(req)
    warm.assert_awaited_once_with(UID, req, scope=None)


@pytest.mark.parametrize("state", ["hot", "queued", "warming"])
async def test_ready_or_in_progress_study_is_not_resubmitted(warming, state):
    status, warm = warming
    status.return_value = {UID: state}
    await proxy.queue_sidebar_study_warm(request())
    warm.assert_not_awaited()


@pytest.mark.parametrize(
    "path,method",
    [
        (PATH, "HEAD"),
        (PATH, "OPTIONS"),
        ("/dicom-web/studies", "GET"),
        (f"{PATH}/4.5/metadata", "GET"),
        (f"{PATH}/4.5/instances/6/frames/1", "GET"),
    ],
)
async def test_other_requests_do_not_trigger_warming(warming, path, method):
    status, warm = warming
    await proxy.queue_sidebar_study_warm(request(path, method))
    status.assert_not_called()
    warm.assert_not_awaited()


async def test_legacy_mode_does_not_check_cache(warming, monkeypatch):
    status, warm = warming
    monkeypatch.setattr(proxy, "STORAGE_MODE", "legacy")
    await proxy.queue_sidebar_study_warm(request())
    status.assert_not_called()
    warm.assert_not_awaited()


async def test_concurrent_listings_submit_once_but_later_eviction_can_warm_again(warming):
    status, warm = warming
    await asyncio.gather(*(proxy.queue_sidebar_study_warm(request()) for _ in range(10)))
    status.assert_called_once_with([UID])
    assert warm.await_count == 1
    # No permanent browser/session dedup: the same study may be evicted later.
    await proxy.queue_sidebar_study_warm(request(PATH + "/"))
    assert warm.await_count == 2


async def test_concurrent_listing_waits_for_queue_markers(warming):
    _, warm = warming
    started, submitted = asyncio.Event(), asyncio.Event()

    async def submit(*args, **kwargs):
        started.set()
        await submitted.wait()

    warm.side_effect = submit
    first = asyncio.create_task(proxy.queue_sidebar_study_warm(request()))
    await started.wait()
    second = asyncio.create_task(proxy.queue_sidebar_study_warm(request()))
    try:
        await asyncio.sleep(0)
        assert not second.done()
    finally:
        submitted.set()
        await asyncio.gather(first, second)
    assert warm.await_count == 1


@pytest.mark.parametrize("error", [HTTPException(503), HTTPException(507), RuntimeError()])
async def test_failed_submission_allows_retry(warming, error):
    _, warm = warming
    warm.side_effect = error
    await proxy.queue_sidebar_study_warm(request())
    warm.side_effect = None
    await proxy.queue_sidebar_study_warm(request())
    assert warm.await_count == 2


@pytest.fixture
def sidebar_app(monkeypatch, warming):
    app = FastAPI()
    app.include_router(proxy.router)
    app.dependency_overrides[proxy.get_current_user] = lambda: "reader"
    monkeypatch.setattr(proxy.dataset_access, "get_user_scope_cached", lambda user: frozenset({"allowed"}))
    monkeypatch.setattr(proxy.dataset_access, "get_study_datasets_cached", lambda uid: frozenset({"allowed"}))
    forwarded = AsyncMock(return_value=Response(b"[]", media_type="application/dicom+json"))
    monkeypatch.setattr(proxy, "_proxy", forwarded)
    return app, forwarded


async def test_authorized_listing_queues_before_forwarding_and_preserves_response(sidebar_app, warming):
    app, forwarded = sidebar_app
    _, warm = warming

    async def forward(req):
        warm.assert_awaited_once_with(UID, req, scope=None)
        return Response(b"[]", media_type="application/dicom+json")

    forwarded.side_effect = forward
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="http://test") as client:
        response = await client.get(PATH + "?includefield=00080060")
    assert response.status_code == 200
    assert response.content == b"[]"
    assert response.headers["content-type"] == "application/dicom+json"


@pytest.mark.parametrize("denial", [401, 403])
async def test_unauthenticated_or_out_of_scope_requests_never_warm(
    sidebar_app,
    warming,
    monkeypatch,
    denial,
):
    app, forwarded = sidebar_app
    status, warm = warming
    if denial == 401:

        def logged_out():
            raise HTTPException(401)

        app.dependency_overrides[proxy.get_current_user] = logged_out
    else:
        monkeypatch.setattr(proxy.dataset_access, "get_study_datasets_cached", lambda uid: frozenset({"other"}))
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="http://test") as client:
        response = await client.get(PATH)
    assert response.status_code == denial
    status.assert_not_called()
    warm.assert_not_awaited()
    forwarded.assert_not_awaited()


async def test_storage_rejection_does_not_replace_qido_response(sidebar_app, warming):
    app, _ = sidebar_app
    _, warm = warming
    warm.side_effect = HTTPException(507)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="http://test") as client:
        response = await client.get(PATH)
    assert response.status_code == 200
    assert response.content == b"[]"
