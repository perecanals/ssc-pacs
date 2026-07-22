"""Reverse-proxy /ohif/* and /dicom-web/* to Orthanc.

End users authenticate to the web app via JWT cookie. The web app forwards their
requests to Orthanc, attaching the service-account Basic auth from .env. Users
no longer need entries in orthanc_users.json.
"""

from __future__ import annotations

import asyncio
import re
import time
from urllib.parse import parse_qsl, urlencode

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request
from starlette.background import BackgroundTask
from starlette.concurrency import run_in_threadpool
from starlette.responses import Response, StreamingResponse

import cache_manager
import dataset_access
from auth import get_current_user
from config import OHIF_TRACKPAD_PX_PER_SLICE
from orthanc_client import ORTHANC_PASS, ORTHANC_URL, ORTHANC_USER

router = APIRouter()

# WADO-RS/QIDO-RS path form: /dicom-web/studies/{StudyInstanceUID}[/...]
_STUDY_PATH_RE = re.compile(r"^/dicom-web/studies/([^/]+)")

# Webpack emits OHIF's assets with a 20-hex [contenthash] as a whole
# dot-delimited segment: app.bundle.<hash>.js, <hash>.woff2, <hash>.wasm. The
# name derives from the bytes, so a rebuild always produces a *new* URL and a
# cached old URL can never go stale — that is what makes `immutable` safe. No
# extension allowlist: the hash is the proof, and an allowlist would silently
# drop future asset types (.svg, .map) out of caching.
_CONTENTHASH_ASSET_RE = re.compile(r"(?:^|[.-])[0-9a-f]{20}\.[A-Za-z0-9]+$")

# Orthanc serves the OHIF build with no Cache-Control/ETag/Last-Modified, so
# every viewer open re-downloads ~21 MiB. `private`, not `public`: these sit
# behind get_current_user, and the browser is the only cache on this path
# anyway. No Vary: Cookie needed — the 200 bytes are user-independent.
_IMMUTABLE_CACHE_CONTROL = "private, max-age=31536000, immutable"


def is_immutable_ohif_asset(path: str) -> bool:
    """True for content-hashed OHIF build artefacts under /ohif/.

    Unhashed siblings (app.bundle.css, app-config.js, manifest.json, and the
    /ohif/ and /ohif/viewer entry documents) deliberately return False: they
    keep their names across rebuilds, so caching them would go stale, and
    Orthanc sends no ETag/Last-Modified for a revalidating policy to use.
    """
    if not path.startswith("/ohif/"):
        return False
    # Match the basename — `(?:^|[.-])` excludes `/`, so searching the full
    # path would miss /ohif/<hash>.woff2.
    return bool(_CONTENTHASH_ASSET_RE.search(path.rpartition("/")[2]))


# ---------------------------------------------------------------------------
# OHIF trackpad scroll damping
# ---------------------------------------------------------------------------
# Cornerstone3D scrolls one slice per wheel *event*, ignoring delta magnitude
# — right for mouse detents, but a trackpad swipe fires dozens of small
# events. No OHIF/plugin knob exists, so this capture-phase shim is injected
# into the entry documents: trackpad-like events (pixel-mode, wheelDeltaY not
# a multiple of the 120 detent quantum) accumulate, and one event per
# `ohif_trackpad_px_per_slice` pixels reaches Cornerstone. Mouse wheels
# bypass the accumulator — and a single detent clears the default threshold
# regardless. Per-browser live tuning: localStorage.sscTrackpadPxPerSlice
# (threshold) / sscTrackpadShimOff = '1' (kill switch).
_OHIF_SHIM_MARKER = b"ssc-trackpad-shim"

_OHIF_WHEEL_SHIM = """\
<script id="ssc-trackpad-shim">/* injected by web-app routes/proxy.py */
(function () {
  'use strict';
  var acc = 0, last = 0;
  window.addEventListener('wheel', function (e) {
    if (localStorage.getItem('sscTrackpadShimOff') === '1') return;
    var t = e.target;
    if (!t || !t.closest ||
        !t.closest('[data-viewport-uid], .viewport-element')) return;
    if (e.deltaMode !== 0) return;  // line/page deltas: a real wheel
    var wdy = e.wheelDeltaY;        // detent-quantized on real wheels
    if (typeof wdy === 'number' && wdy !== 0 && wdy % 120 === 0) return;
    var px = parseFloat(localStorage.getItem('sscTrackpadPxPerSlice'));
    if (!(px > 0)) px = __PX_PER_SLICE__;
    // New gesture (idle > 300 ms) or direction flip: restart the tally.
    if (e.timeStamp - last > 300 || acc * e.deltaY < 0) acc = 0;
    last = e.timeStamp;
    acc += e.deltaY;
    if (Math.abs(acc) >= px) { acc %= px; return; }  // pass: one slice
    e.preventDefault();  // swallowed: Cornerstone never sees it
    e.stopPropagation();
  }, { capture: true, passive: false });
})();
</script>""".replace(
    "__PX_PER_SLICE__", str(OHIF_TRACKPAD_PX_PER_SLICE)
).encode()


def inject_wheel_shim(body: bytes) -> bytes:
    """Insert the trackpad shim into an OHIF entry document.

    Before </head>, so the capture listener is registered before OHIF's
    deferred bundle runs (capture-phase ordering would save us regardless;
    this keeps the intent obvious). No-op when damping is disabled or the
    shim is already present.
    """
    if OHIF_TRACKPAD_PX_PER_SLICE <= 0 or _OHIF_SHIM_MARKER in body:
        return body
    for anchor in (b"</head>", b"</body>"):
        idx = body.find(anchor)
        if idx != -1:
            return body[:idx] + _OHIF_WHEEL_SHIM + body[idx:]
    return body + _OHIF_WHEEL_SHIM


async def dicomweb_dataset_guard(
    request: Request,
    user: str = Depends(get_current_user),
) -> None:
    """Per-request dataset scoping for the DICOMweb proxy.

    Resolves the request to a dataset-taggable entity and rejects anything
    outside the caller's dataset scope. Admins bypass. Two resolution paths:

     - StudyInstanceUID from the WADO-RS path or QIDO-RS query string
       (OHIF's viewer requests);
     - PatientID (0010,0020) from the QIDO-RS query string — OHIF's study
       browser panel searches by PatientID, not StudyInstanceUID.

    Requests with neither identifier (unscoped QIDO searches) are denied for
    non-admins: deny-by-default.

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
    if uid:
        datasets = await run_in_threadpool(
            dataset_access.get_study_datasets_cached, uid
        )
    else:
        patient_id = (
            request.query_params.get("PatientID")
            or request.query_params.get("00100020")
        )
        if not patient_id:
            raise HTTPException(status_code=403, detail="Dataset access denied")
        datasets = await run_in_threadpool(
            dataset_access.get_patient_datasets_cached, patient_id
        )
    if not dataset_access.scope_allows(scope, datasets):
        raise HTTPException(status_code=403, detail="Dataset access denied")


# QIDO study-level search endpoint (exact path — study sub-resources like
# /studies/{uid}/series are series-level, where Modality is answerable).
_STUDY_SEARCH_PATH = "/dicom-web/studies"

# includefield tokens Orthanc cannot answer from its index at study level.
# Modality (0008,0060) is a series-level tag: requesting it in a study-level
# QIDO search makes Orthanc open one DICOM file from storage per matching
# study (its own log flags this, W001/W005) — a disk read per study that turns
# into a 500 for the whole search whenever any referenced file is absent
# (evicted cold series under RemoveMissingFiles:false, or a stale index path).
# Stripping it is lossless: Orthanc always returns the index-computed
# ModalitiesInStudy (0008,0061) in study-level responses, and OHIF's
# getModalities() falls back to it when Modality is absent.
_STUDY_LEVEL_UNANSWERABLE = frozenset({"00080060", "modality"})


def sanitize_study_search_query(query: str) -> str:
    """Drop storage-forcing tokens from includefield in a QIDO study search."""
    if "includefield" not in query.lower():
        return query
    pairs = []
    for key, value in parse_qsl(query, keep_blank_values=True):
        if key.lower() == "includefield":
            kept = [
                tok for tok in value.split(",")
                if tok.strip().lower() not in _STUDY_LEVEL_UNANSWERABLE
            ]
            if not kept:
                continue
            value = ",".join(kept)
        pairs.append((key, value))
    return urlencode(pairs)

# WADO-RS series-level metadata: the request OHIF issues once per series when
# it opens a study, to build the side panel. Orthanc answers it by reading the
# instance files from storage, so for a series whose files are still being
# extracted from cold storage it 500s ("series metadata json does not contain
# an array") and OHIF pops a persistent error toast. Instead of forwarding
# into that race, the proxy holds the request while the series is
# 'queued'/'warming' and forwards once it turns hot — the panel entry then
# simply appears a few seconds later. Frame/instance requests never need this:
# OHIF only issues them after the metadata resolved, i.e. after the files are
# back on disk.
_SERIES_METADATA_RE = re.compile(
    r"^/dicom-web/studies/[^/]+/series/([^/]+)/metadata$"
)

# Cap on how long one metadata request is held. Normal per-study warms finish
# in seconds; on expiry the request is forwarded as-is (worst case: one error
# toast, i.e. the pre-hold behavior). Stuck 'warming'/'queued' rows are not a
# concern here — cache_manager's effective status reports them as cold after
# WARMING_TIMEOUT_MINUTES, which also ends the hold.
_WARM_WAIT_MAX_SECONDS = 120.0
_WARM_WAIT_POLL_SECONDS = 0.5


async def wait_for_series_warm(seriesinstanceuid: str) -> None:
    """Hold until the series is no longer queued/warming (bounded).

    Costs one indexed DB read per poll, off the event loop. Series without a
    cache row (legacy mode, or never archived) report 'cold' and fall straight
    through — the hold only ever engages while a warm is actually in flight.
    """
    deadline = time.monotonic() + _WARM_WAIT_MAX_SECONDS
    while True:
        status = (
            await run_in_threadpool(
                cache_manager.get_batch_series_status, [seriesinstanceuid]
            )
        ).get(seriesinstanceuid, "cold")
        if status not in ("queued", "warming") or time.monotonic() >= deadline:
            return
        await asyncio.sleep(_WARM_WAIT_POLL_SECONDS)


# Orthanc's DICOMweb plugin emits *absolute* URLs in its JSON responses —
# BulkDataURI (overlay data (6000,3000), bulk pixel data (7fe0,0010), ...) and
# RetrieveURL — built from the upstream request, i.e. pointing at
# ORTHANC_URL itself. OHIF follows BulkDataURI verbatim, so from a page served
# on the web app's origin the fetch goes cross-origin straight at Orthanc:
# blocked by CORS, and end users hold no Orthanc credentials anyway (that is
# the point of this proxy). Rewriting the base to a relative /dicom-web makes
# the browser resolve those URLs against the web app origin, sending bulkdata
# through the authenticated proxy like every other DICOMweb request.
_ORTHANC_DICOMWEB_BASE = f"{ORTHANC_URL.rstrip('/')}/dicom-web".encode()


def rewrite_dicomweb_urls(body: bytes) -> bytes:
    """Relativize absolute Orthanc DICOMweb URLs in a JSON response body."""
    return body.replace(_ORTHANC_DICOMWEB_BASE, b"/dicom-web")


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


async def _proxy(request: Request) -> Response:
    client = _get_client()
    series_metadata = _SERIES_METADATA_RE.match(request.url.path)
    if series_metadata:
        await wait_for_series_warm(series_metadata.group(1))
    upstream_url = f"{ORTHANC_URL}{request.url.path}"
    query = request.url.query
    if query and request.url.path == _STUDY_SEARCH_PATH:
        query = sanitize_study_search_query(query)
    if query:
        upstream_url = f"{upstream_url}?{query}"

    headers = _filtered_request_headers(request)
    body = await request.body() if request.method not in ("GET", "HEAD") else None

    upstream_req = client.build_request(
        request.method,
        upstream_url,
        headers=headers,
        content=body,
    )
    upstream = await client.send(upstream_req, stream=True)
    headers = _filtered_response_headers(upstream)
    if upstream.status_code == 200 and is_immutable_ohif_asset(request.url.path):
        headers["cache-control"] = _IMMUTABLE_CACHE_CONTROL
    if (
        upstream.status_code == 200
        and request.url.path.startswith("/ohif")
        and upstream.headers.get("content-type", "").lower().startswith("text/html")
    ):
        # Entry documents only (/ohif/, /ohif/viewer — a few KB, deliberately
        # uncached): buffer and inject the trackpad shim. aread() decodes any
        # content-encoding, so that header and the stale content-length must
        # go; Response recomputes the length. Assets stream below untouched.
        try:
            html = await upstream.aread()
        finally:
            await upstream.aclose()
        headers.pop("content-encoding", None)
        headers.pop("content-length", None)
        return Response(
            content=inject_wheel_shim(html),
            status_code=upstream.status_code,
            headers=headers,
        )
    if (
        upstream.status_code == 200
        and request.url.path.startswith("/dicom-web")
        and "json" in upstream.headers.get("content-type", "").lower()
    ):
        # QIDO / metadata responses only — frames and bulkdata are
        # multipart/related or application/octet-stream and stream untouched.
        # aread() decodes any content-encoding, so that header and the stale
        # content-length must go; Response recomputes the length.
        try:
            body = await upstream.aread()
        finally:
            await upstream.aclose()
        headers.pop("content-encoding", None)
        headers.pop("content-length", None)
        return Response(
            content=rewrite_dicomweb_urls(body),
            status_code=upstream.status_code,
            headers=headers,
        )
    return StreamingResponse(
        upstream.aiter_raw(),
        status_code=upstream.status_code,
        headers=headers,
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
