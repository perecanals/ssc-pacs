"""Reverse-proxy /ohif/* and /dicom-web/* to Orthanc.

End users authenticate to the web app via JWT cookie. The web app forwards their
requests to Orthanc, attaching the service-account Basic auth from .env. Users
no longer need entries in orthanc_users.json.
"""

from __future__ import annotations

import re

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request
from starlette.background import BackgroundTask
from starlette.concurrency import run_in_threadpool
from starlette.responses import StreamingResponse

import dataset_access
from auth import get_current_user
from orthanc_client import ORTHANC_PASS, ORTHANC_URL, ORTHANC_USER

router = APIRouter()

# WADO-RS/QIDO-RS path form: /dicom-web/studies/{StudyInstanceUID}[/...]
_STUDY_PATH_RE = re.compile(r"^/dicom-web/studies/([^/]+)")


async def dicomweb_dataset_guard(
    request: Request,
    user: str = Depends(get_current_user),
) -> None:
    """Per-request dataset scoping for the DICOMweb proxy.

    Extracts the StudyInstanceUID from the WADO-RS path or the QIDO-RS query
    string and rejects studies outside the caller's dataset scope. Admins
    bypass. Requests with no resolvable study UID (unscoped QIDO searches)
    are denied for non-admins — OHIF is always opened with explicit
    StudyInstanceUIDs, so the viewer never needs an unscoped search.

    DB lookups are cached in-process (dataset_access TTL caches) and run in
    the threadpool, so per-frame requests cost no DB round-trips and never
    block the event loop.
    """
    scope = await run_in_threadpool(dataset_access.get_user_scope_cached, user)
    if scope is None:
        return
    m = _STUDY_PATH_RE.match(request.url.path)
    uid = m.group(1) if m else (
        request.query_params.get("StudyInstanceUID")
        or request.query_params.get("0020000D")
    )
    if not uid:
        raise HTTPException(status_code=403, detail="Dataset access denied")
    datasets = await run_in_threadpool(
        dataset_access.get_study_datasets_cached, uid
    )
    if not dataset_access.scope_allows(scope, datasets):
        raise HTTPException(status_code=403, detail="Dataset access denied")

# Hop-by-hop headers per RFC 7230 §6.1 — must not be forwarded in either direction.
_HOP_BY_HOP = frozenset({
    "connection",
    "keep-alive",
    "proxy-authenticate",
    "proxy-authorization",
    "te",
    "trailers",
    "transfer-encoding",
    "upgrade",
})

# Inbound request headers to drop in addition to hop-by-hop:
#   host:            httpx sets from target URL
#   cookie:          Orthanc doesn't use the web app's session cookie
#   authorization:   replaced by the client's BasicAuth (service account)
#   content-length:  httpx recomputes from the forwarded body
_DROP_REQUEST_HEADERS = _HOP_BY_HOP | {
    "host",
    "cookie",
    "authorization",
    "content-length",
}

_CLIENT: httpx.AsyncClient | None = None


def init_client() -> None:
    """Initialize the shared httpx client. Called from the app lifespan."""
    global _CLIENT
    _CLIENT = httpx.AsyncClient(
        timeout=httpx.Timeout(30.0, connect=5.0, read=300.0, write=60.0),
        limits=httpx.Limits(max_connections=64, max_keepalive_connections=32),
        auth=httpx.BasicAuth(ORTHANC_USER, ORTHANC_PASS),
        follow_redirects=False,
    )


async def shutdown_client() -> None:
    """Close the shared httpx client. Called from the app lifespan teardown."""
    global _CLIENT
    if _CLIENT is not None:
        await _CLIENT.aclose()
        _CLIENT = None


def _get_client() -> httpx.AsyncClient:
    if _CLIENT is None:
        raise RuntimeError("Proxy httpx client not initialized")
    return _CLIENT


def _filtered_request_headers(request: Request) -> dict[str, str]:
    return {
        k: v
        for k, v in request.headers.items()
        if k.lower() not in _DROP_REQUEST_HEADERS
    }


def _filtered_response_headers(upstream: httpx.Response) -> dict[str, str]:
    return {
        k: v
        for k, v in upstream.headers.items()
        if k.lower() not in _HOP_BY_HOP
    }


async def _proxy(request: Request) -> StreamingResponse:
    client = _get_client()
    upstream_url = f"{ORTHANC_URL}{request.url.path}"
    if request.url.query:
        upstream_url = f"{upstream_url}?{request.url.query}"

    headers = _filtered_request_headers(request)
    body = await request.body() if request.method not in ("GET", "HEAD") else None

    upstream_req = client.build_request(
        request.method,
        upstream_url,
        headers=headers,
        content=body,
    )
    upstream = await client.send(upstream_req, stream=True)
    return StreamingResponse(
        upstream.aiter_raw(),
        status_code=upstream.status_code,
        headers=_filtered_response_headers(upstream),
        background=BackgroundTask(upstream.aclose),
    )


_PROXY_METHODS = ["GET", "HEAD", "POST", "OPTIONS"]


@router.api_route(
    "/ohif",
    methods=_PROXY_METHODS,
    dependencies=[Depends(get_current_user)],
)
async def proxy_ohif_root(request: Request):
    return await _proxy(request)


@router.api_route(
    "/ohif/{path:path}",
    methods=_PROXY_METHODS,
    dependencies=[Depends(get_current_user)],
)
async def proxy_ohif(request: Request, path: str):
    return await _proxy(request)


@router.api_route(
    "/dicom-web",
    methods=_PROXY_METHODS,
    dependencies=[Depends(dicomweb_dataset_guard)],
)
async def proxy_dicom_web_root(request: Request):
    return await _proxy(request)


@router.api_route(
    "/dicom-web/{path:path}",
    methods=_PROXY_METHODS,
    dependencies=[Depends(dicomweb_dataset_guard)],
)
async def proxy_dicom_web(request: Request, path: str):
    return await _proxy(request)
