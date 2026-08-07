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
import json
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
        # 'm'/'n' step a MIP slab up/down (mm steps) across the volume
        # viewports; from a stack layout the active pane is first converted
        # in place (OHIF's stack->orthographic orientation-menu move), and
        # the requested level is polled onto the volume once it is renderable
        # instead of blocking on the download.
        for token in (
            b"MAXIMUM_INTENSITY_BLEND",
            b"setDisplaySetsForViewports",
            b"'acquisition'",
            b"isReconstructable",
            b"[1.25, 2.5, 5, 10, 20, 30]",
            b"e.key === 'n'",
            b"(loading...)",
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


APP_CONFIG_JS = b"window.config = {\n  hotkeys: [\n    " \
    b"{ commandName: 'invertViewport', label: 'Invert', keys: ['i'] },\n  ],\n};\n"


class TestInjectExtraHotkeys:
    def test_appends_after_the_config_assignment(self):
        out = proxy.inject_extra_hotkeys(APP_CONFIG_JS)
        assert out.startswith(APP_CONFIG_JS)
        assert proxy._OHIF_HOTKEYS_MARKER in out

    def test_idempotent(self):
        once = proxy.inject_extra_hotkeys(APP_CONFIG_JS)
        assert proxy.inject_extra_hotkeys(once) == once

    def test_binds_d_to_the_mpr_hanging_protocol(self):
        # Same command the toolbar's MPR button runs, so OHIF's Preferences
        # dialog lists it and users can rebind the key.
        for token in (
            b"'toggleHangingProtocol'",
            b"protocolId: 'mpr'",
            b"keys: ['d']",
            b"isEditable: true",
            b"'ohif.hotkeyBindings'",
            b"sscExtraHotkeysOff",
        ):
            assert token in proxy._OHIF_EXTRA_HOTKEYS

    def test_javascript_parses(self, tmp_path):
        node = shutil.which("node")
        if node is None:
            pytest.skip("node not on PATH")
        js = tmp_path / "hotkeys.js"
        js.write_text(proxy._OHIF_EXTRA_HOTKEYS.decode())
        result = subprocess.run(
            [node, "--check", str(js)], capture_output=True, text=True
        )
        assert result.returncode == 0, result.stderr

    # OHIF 3.11 rebuilds the customization scopes from the extension modules
    # on every mode entry and reads 'ohif.hotkeyBindings' in the same
    # synchronous block, so the injected JS wraps getCustomization itself
    # (installed via a window.services property hook — the OHIF cornerstone
    # extension assigns that global during init, before the first mode
    # entry). These tests execute the payload under node against a stand-in
    # services object assigned *after* the hook installs, mirroring the real
    # ordering.

    _NODE_STUB = (
        "var __ls = {};\n"
        "var localStorage = { getItem: k => __ls[k] ?? null };\n"
        "var window = {};\n"
    )

    _NODE_SERVICES = (
        "window.services = { customizationService: {\n"
        "  getCustomization: function (id) {\n"
        "    if (id !== 'ohif.hotkeyBindings') return { other: true };\n"
        "    return [\n"
        "      { commandName: 'invertViewport', label: 'Invert',"
        " keys: ['i'] },\n"
        "      { commandName: 'toggleHangingProtocol',"
        " commandOptions: { protocolId: 'mpr' }, label: 'Old MPR',"
        " keys: ['q'] },\n"
        "    ];\n"
        "  },\n"
        "} };\n"
    )

    def _run_node(self, tmp_path, script: str) -> str:
        node = shutil.which("node")
        if node is None:
            pytest.skip("node not on PATH")
        js = tmp_path / "harness.js"
        js.write_text(script)
        return subprocess.run(
            [node, str(js)], capture_output=True, text=True, check=True
        ).stdout

    def test_wrapper_appends_the_binding_on_read(self, tmp_path):
        out = self._run_node(
            tmp_path,
            self._NODE_STUB
            + proxy._OHIF_EXTRA_HOTKEYS.decode()
            + self._NODE_SERVICES
            + "console.log(JSON.stringify("
            "window.services.customizationService"
            ".getCustomization('ohif.hotkeyBindings')));\n"
            "process.exit(0);\n"
        )
        hotkeys = json.loads(out)
        mpr = [h for h in hotkeys if h["commandName"] == "toggleHangingProtocol"]
        assert mpr == [{
            "commandName": "toggleHangingProtocol",
            "commandOptions": {"protocolId": "mpr"},
            "label": "MPR",
            "keys": ["d"],
            "isEditable": True,
        }]  # exactly one: the stale same-command entry was replaced
        assert hotkeys[-1] == mpr[0]  # appended last, so its key binding wins
        assert {h["commandName"] for h in hotkeys} >= {"invertViewport"}

    def test_wrapper_leaves_other_customizations_alone(self, tmp_path):
        out = self._run_node(
            tmp_path,
            self._NODE_STUB
            + proxy._OHIF_EXTRA_HOTKEYS.decode()
            + self._NODE_SERVICES
            + "console.log(JSON.stringify("
            "window.services.customizationService"
            ".getCustomization('ohif.anythingElse')));\n"
            "process.exit(0);\n"
        )
        assert json.loads(out) == {"other": True}

    def test_wrapper_honours_the_kill_switch(self, tmp_path):
        out = self._run_node(
            tmp_path,
            self._NODE_STUB
            + proxy._OHIF_EXTRA_HOTKEYS.decode()
            + self._NODE_SERVICES
            + "__ls.sscExtraHotkeysOff = '1';\n"
            "console.log(JSON.stringify("
            "window.services.customizationService"
            ".getCustomization('ohif.hotkeyBindings')"
            ".map(h => h.keys)));\n"
            "process.exit(0);\n"
        )
        assert json.loads(out) == [["i"], ["q"]]  # untouched stock list


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

    async def test_app_config_gets_extra_hotkeys(self, monkeypatch):
        resp, out = await _run_proxy(
            monkeypatch,
            "/ohif/app-config.js",
            APP_CONFIG_JS,
            "application/javascript",
        )
        assert isinstance(resp, Response)
        assert not isinstance(resp, StreamingResponse)
        assert out.startswith(APP_CONFIG_JS)
        assert proxy._OHIF_HOTKEYS_MARKER in out
        assert resp.headers["content-length"] == str(len(out))
        # A stale cached copy would keep serving a pre-injection config.
        assert resp.headers["cache-control"] == "no-store"

    async def test_gzipped_app_config_is_decoded_then_injected(
        self, monkeypatch
    ):
        resp, out = await _run_proxy(
            monkeypatch,
            "/ohif/app-config.js",
            gzip.compress(APP_CONFIG_JS),
            "application/javascript",
            extra_headers={"content-encoding": "gzip"},
        )
        assert out.startswith(APP_CONFIG_JS)  # decoded, not raw gzip bytes
        assert proxy._OHIF_HOTKEYS_MARKER in out
        assert "content-encoding" not in resp.headers

    async def test_other_ohif_javascript_streams_untouched(self, monkeypatch):
        # Only app-config.js is rewritten — the 15 MiB bundle must not be
        # buffered, and its content-hashed name keeps it immutably cached.
        resp, out = await _run_proxy(
            monkeypatch,
            "/ohif/app.bundle.b34f32c50e70ee27ad26.js",
            b"console.log(1)",
            "application/javascript",
        )
        assert isinstance(resp, StreamingResponse)
        assert out == b"console.log(1)"

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

    @pytest.mark.parametrize("path", ["/ohif/viewer", "/ohif/app.bundle.css"])
    async def test_coop_header_is_stripped(self, monkeypatch, path):
        # Orthanc serves OHIF with COOP: same-origin; forwarding it severs the
        # second-screen popup's opener handle (window.closed reads true), so
        # the proxy must drop it on both the buffered-HTML and streaming paths.
        body = ENTRY_HTML if path.endswith("viewer") else b"body{}"
        ctype = "text/html" if path.endswith("viewer") else "text/css"
        resp, _ = await _run_proxy(
            monkeypatch, path, body, ctype,
            extra_headers={
                "cross-origin-opener-policy": "same-origin",
                "cross-origin-embedder-policy": "require-corp",
            },
        )
        assert "cross-origin-opener-policy" not in resp.headers
        # COEP is deliberately kept — it doesn't affect the opener handle.
        assert resp.headers["cross-origin-embedder-policy"] == "require-corp"
