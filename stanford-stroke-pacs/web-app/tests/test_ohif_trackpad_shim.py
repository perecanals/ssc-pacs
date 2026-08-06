"""Tests for the OHIF trackpad-scroll shim injected by the reverse proxy.

Cornerstone3D scrolls one slice per wheel event regardless of delta size, so
trackpads (dozens of small-delta events per swipe) overshoot wildly. The proxy
injects a damping script into the OHIF entry documents in transit; these tests
cover the injection helper and the _proxy branch that applies it. Assets and
non-OHIF responses must keep streaming byte-identically.

DB-free by construction: nothing here uses the `client` fixture, so no
Postgres is required.
"""

from __future__ import annotations

import gzip
import shutil
import subprocess
import sys
from pathlib import Path

import httpx
import pytest
from starlette.requests import Request
from starlette.responses import Response, StreamingResponse

# Ensure web-app/ is importable.
_WEB_APP_DIR = Path(__file__).resolve().parent.parent
if str(_WEB_APP_DIR) not in sys.path:
    sys.path.insert(0, str(_WEB_APP_DIR))

from routes import proxy  # noqa: E402

ENTRY_HTML = (
    b'<!doctype html><html><head><title>OHIF Viewer</title></head>'
    b'<body><div id="root"></div></body></html>'
)


@pytest.fixture(autouse=True)
def _damping_enabled(monkeypatch):
    """Pin the threshold so tests don't depend on the host's config.toml."""
    monkeypatch.setattr(proxy, "OHIF_TRACKPAD_PX_PER_SLICE", 100)


class TestInjectWheelShim:
    def test_injects_before_head_close(self):
        out = proxy.inject_wheel_shim(ENTRY_HTML)
        assert proxy._OHIF_SHIM_MARKER in out
        assert out.index(proxy._OHIF_SHIM_MARKER) < out.index(b"</head>")

    def test_falls_back_to_body_close(self):
        out = proxy.inject_wheel_shim(b"<html><body>x</body></html>")
        assert out.index(proxy._OHIF_SHIM_MARKER) < out.index(b"</body>")

    def test_appends_without_anchors(self):
        out = proxy.inject_wheel_shim(b"stub")
        assert out.startswith(b"stub")
        assert proxy._OHIF_SHIM_MARKER in out

    def test_idempotent(self):
        once = proxy.inject_wheel_shim(ENTRY_HTML)
        assert proxy.inject_wheel_shim(once) == once

    def test_zero_threshold_disables_script_but_not_dialog_fit(
        self, monkeypatch
    ):
        # The threshold kill switch governs the input script only; the
        # dialog-fit CSS prevents a viewer crash and must stay injected.
        monkeypatch.setattr(proxy, "OHIF_TRACKPAD_PX_PER_SLICE", 0)
        out = proxy.inject_wheel_shim(ENTRY_HTML)
        assert proxy._OHIF_SHIM_MARKER not in out
        assert proxy._OHIF_DIALOG_FIT_MARKER in out

    def test_threshold_was_rendered_into_the_script(self):
        # The placeholder must be substituted at import; a leftover would make
        # the browser throw and disable damping silently.
        assert b"__PX_PER_SLICE__" not in proxy._OHIF_WHEEL_SHIM

    def test_shim_covers_arrow_key_navigation(self):
        # Arrow presses become synthetic wheel events, flagged so the damping
        # handler passes them through untouched.
        assert b"ArrowDown" in proxy._OHIF_WHEEL_SHIM
        assert proxy._OHIF_WHEEL_SHIM.count(b"sscSynthetic") >= 2

    def test_shim_covers_mip_cycling(self):
        # 'm' cycles a MIP slab (mm steps) across the volume viewports; from
        # a stack layout the active pane is first converted in place to a
        # volume viewport (OHIF's stack->orthographic orientation-menu move).
        for token in (
            b"MAXIMUM_INTENSITY_BLEND",
            b"setDisplaySetsForViewports",
            b"'acquisition'",
            b"isReconstructable",
            b"[1.25, 2.5, 5, 10, 20, 30]",
            b"sscMipShimOff",
            b"sscMipSlabSteps",
        ):
            assert token in proxy._OHIF_WHEEL_SHIM

    def test_dialog_fit_css_is_injected_with_the_shim(self):
        # OHIF 3.11's ManagedDialog crashes (and unmounts the viewer) when a
        # dialog without a defaultPosition mounts clipped — the ~500px
        # Rendering Presets dialog inside the ~400px preview-pane iframe.
        # The CSS keeps dialogs inside small viewports so the crashing
        # branch never runs; big windows are untouched via the media scope.
        out = proxy.inject_wheel_shim(ENTRY_HTML)
        assert proxy._OHIF_DIALOG_FIT_MARKER in out
        assert out.index(proxy._OHIF_DIALOG_FIT_MARKER) < out.index(b"</head>")
        for token in (
            b"@media (max-height: 659px)",
            b'div[role="dialog"].fixed',
            b"max-height: 94vh",
            b"h-\\[500px\\]",
            b"@media (max-width: 479px)",
        ):
            assert token in proxy._OHIF_DIALOG_FIT

    def test_shim_javascript_parses(self, tmp_path):
        # A syntax error would kill the whole inline script — damping, arrows,
        # and MIP all silently gone. Gate on node when available.
        node = shutil.which("node")
        if node is None:
            pytest.skip("node not on PATH")
        script = proxy._OHIF_WHEEL_SHIM.decode()
        body = script.split(">", 1)[1].rsplit("</script>", 1)[0]
        js = tmp_path / "shim.js"
        js.write_text(body)
        result = subprocess.run(
            [node, "--check", str(js)], capture_output=True, text=True
        )
        assert result.returncode == 0, result.stderr


# ---------------------------------------------------------------------------
# _proxy branch, via httpx.MockTransport against the module-level client
# (_get_client reads _CLIENT at call time, so monkeypatching suffices).
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


async def _run_proxy(
    monkeypatch,
    path: str,
    body: bytes,
    content_type: str,
    status: int = 200,
    extra_headers: dict | None = None,
):
    """Run _proxy against a stubbed upstream; return (response, raw bytes)."""
    async def _stream():
        # An async-generator body keeps the stream unconsumed, which is what
        # _proxy's aiter_raw()/aread() need; content=b"..." would mark it read.
        yield body

    def handler(request: httpx.Request) -> httpx.Response:
        headers = {"content-type": content_type, "content-length": str(len(body))}
        headers.update(extra_headers or {})
        return httpx.Response(status, content=_stream(), headers=headers)

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    monkeypatch.setattr(proxy, "_CLIENT", client)
    try:
        resp = await proxy._proxy(_make_request(path))
        if isinstance(resp, StreamingResponse):
            # Drain: BackgroundTask(upstream.aclose) never fires outside a
            # real ASGI cycle, and httpx warns on an unclosed response.
            chunks = b"".join([chunk async for chunk in resp.body_iterator])
        else:
            chunks = resp.body
        return resp, chunks
    finally:
        await client.aclose()


class TestProxyInjection:
    @pytest.mark.parametrize("path", ["/ohif/", "/ohif/viewer"])
    async def test_entry_document_gets_shim(self, monkeypatch, path):
        resp, out = await _run_proxy(
            monkeypatch, path, ENTRY_HTML, "text/html"
        )
        assert isinstance(resp, Response)
        assert not isinstance(resp, StreamingResponse)
        assert proxy._OHIF_SHIM_MARKER in out
        assert proxy._OHIF_DIALOG_FIT_MARKER in out
        assert resp.headers["content-length"] == str(len(out))

    async def test_gzipped_entry_document_is_decoded_then_injected(
        self, monkeypatch
    ):
        resp, out = await _run_proxy(
            monkeypatch,
            "/ohif/",
            gzip.compress(ENTRY_HTML),
            "text/html",
            extra_headers={"content-encoding": "gzip"},
        )
        assert proxy._OHIF_SHIM_MARKER in out
        assert b"</head>" in out  # decoded, not raw gzip bytes
        assert "content-encoding" not in resp.headers
        assert resp.headers["content-length"] == str(len(out))

    async def test_asset_streams_untouched(self, monkeypatch):
        resp, out = await _run_proxy(
            monkeypatch, "/ohif/app.bundle.css", b"body{}", "text/css"
        )
        assert isinstance(resp, StreamingResponse)
        assert out == b"body{}"

    async def test_html_outside_ohif_is_not_injected(self, monkeypatch):
        resp, out = await _run_proxy(
            monkeypatch, "/dicom-web/studies", ENTRY_HTML, "text/html"
        )
        assert isinstance(resp, StreamingResponse)
        assert proxy._OHIF_SHIM_MARKER not in out

    async def test_non_200_is_not_injected(self, monkeypatch):
        resp, out = await _run_proxy(
            monkeypatch, "/ohif/", b"<html>not found</html>", "text/html",
            status=404,
        )
        assert isinstance(resp, StreamingResponse)
        assert proxy._OHIF_SHIM_MARKER not in out
