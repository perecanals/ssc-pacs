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
# OHIF viewport input shim (trackpad damping + arrow-key slice navigation)
# ---------------------------------------------------------------------------
# Trackpad: Cornerstone3D scrolls one slice per wheel *event*, ignoring delta
# magnitude — right for mouse detents, but a trackpad swipe fires dozens of
# small events. No OHIF/plugin knob exists, so this capture-phase shim is
# injected into the entry documents: trackpad-like events (pixel-mode,
# wheelDeltaY not a multiple of the 120 detent quantum) accumulate, and one
# event per `ohif_trackpad_px_per_slice` pixels reaches Cornerstone. Mouse
# wheels bypass the accumulator — and a single detent clears the default
# threshold regardless. Live tuning: localStorage.sscTrackpadPxPerSlice
# (threshold) / sscTrackpadShimOff = '1' (kill switch for the damping).
#
# Arrows: OHIF binds up/down to previous/nextImage via its hotkeys manager,
# which drops keys depending on focus — flaky. The shim owns ArrowUp/Down in
# capture phase (unless typing in a field) and turns each press into a
# synthetic one-detent wheel on the last-clicked viewport, so "click the
# image, then arrows" always works and OHIF's own binding can't double-step.
#
# MIP ('m' up / 'n' down): the keys step a maximum-intensity-projection
# slab through preset thicknesses (mm) on every volume viewport; below the
# first step (and past the last) is normal composite. From the plain stack
# view the first 'm' converts the active pane in place to a volume viewport
# in acquisition orientation (same image, same layout — the
# stack->orthographic move OHIF's own orientation menu makes); in MPR all
# three panes slab together. The keys record the *requested* level
# immediately — while a large volume is still streaming the HUD shows
# "(loading...)" and the level lands the moment the volume is renderable,
# so nobody has to wait out the download to press keys. Drives the globals
# the OHIF cornerstone extension exposes (window.cornerstone /
# window.commandsManager / window.services) — nothing in the plugin is
# patched. 'm' is unbound in OHIF's stock hotkeys; 'n' shadows the
# labelmap-interpolate binding (segmentation tooling, unused here). Live
# tuning: localStorage.sscMipSlabSteps = '1.25,2.5,5,10,20,30' /
# sscMipShimOff = '1' (kill switch for the MIP handler).
#
# Dialog-fit (the <style> injected alongside): OHIF 3.11's ManagedDialog
# crashes when a dialog carrying no defaultPosition mounts clipped by the
# viewport — its reposition helper dereferences defaultPosition.y/.x
# (upstream bug, still on OHIF master). The embedded preview pane hits
# exactly that: the ~500px-tall "Rendering Presets" dialog opens inside a
# ~400px-tall iframe, the uncaught TypeError unmounts OHIF's whole React
# tree, and the pane stays black until the series is reselected. The
# media-scoped rules keep centered dialogs inside small viewports (unclipped
# at mount ⇒ the crashing branch never runs — that is the actual fix, and
# the `div[role="dialog"].fixed` selector is structural, matching every
# Radix dialog); the `h-\\[500px\\]` rule is ergonomics on top, shrinking the
# preset grid so title, search and Cancel stay visible and only the grid
# scrolls. Viewports ≥660px tall and ≥480px wide are untouched.
_OHIF_SHIM_MARKER = b"ssc-trackpad-shim"
_OHIF_DIALOG_FIT_MARKER = b"ssc-dialog-fit"

_OHIF_WHEEL_SHIM = """\
<script id="ssc-trackpad-shim">/* injected by web-app routes/proxy.py */
(function () {
  'use strict';
  var VP = '[data-viewport-uid], .viewport-element';
  var acc = 0, last = 0, lastVp = null;
  function vpOf(node) {
    return node && node.closest ? node.closest(VP) : null;
  }
  window.addEventListener('wheel', function (e) {
    if (e.sscSynthetic) return;  // our own arrow-key events pass untouched
    if (localStorage.getItem('sscTrackpadShimOff') === '1') return;
    if (!vpOf(e.target)) return;
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
  window.addEventListener('pointerdown', function (e) {
    var vp = vpOf(e.target);
    if (vp) lastVp = vp;
  }, true);
  window.addEventListener('keydown', function (e) {
    if (e.key !== 'ArrowUp' && e.key !== 'ArrowDown') return;
    var a = document.activeElement, tag = a && a.tagName;
    if (tag === 'INPUT' || tag === 'TEXTAREA' || tag === 'SELECT' ||
        (a && a.isContentEditable)) return;
    var vp = lastVp && document.contains(lastVp)
      ? lastVp : document.querySelector(VP);
    if (!vp) return;
    e.preventDefault();   // the page must not scroll
    e.stopPropagation();  // OHIF's own hotkey must not double-step
    var evt = new WheelEvent('wheel', {
      deltaY: e.key === 'ArrowDown' ? 120 : -120,
      deltaMode: 0, bubbles: true, cancelable: true
    });
    evt.sscSynthetic = true;
    (vp.querySelector('canvas') || vp).dispatchEvent(evt);
  }, true);
  /* ---- 'm'/'n': step a MIP slab up/down through preset thicknesses ---- */
  var MIP_STEPS = [1.25, 2.5, 5, 10, 20, 30];
  var mipIdx = -1, mipConverting = false, mipPoll = 0;
  var hudEl = null, hudTimer = 0;
  function hud(text) {
    if (!document.body) return;
    if (!hudEl) {
      hudEl = document.createElement('div');
      hudEl.style.cssText = 'position:fixed;top:12px;left:50%;' +
        'transform:translateX(-50%);background:rgba(0,0,0,.75);color:#fff;' +
        'padding:4px 12px;border-radius:4px;font:13px sans-serif;' +
        'z-index:99999;pointer-events:none;transition:opacity .3s';
      document.body.appendChild(hudEl);
    }
    hudEl.textContent = text;
    hudEl.style.opacity = '1';
    clearTimeout(hudTimer);
    hudTimer = setTimeout(function () { hudEl.style.opacity = '0'; }, 1200);
  }
  function mipSteps() {
    var raw = localStorage.getItem('sscMipSlabSteps'), out = [];
    if (raw) {
      raw.split(',').forEach(function (s) {
        var v = parseFloat(s);
        if (v > 0) out.push(v);
      });
    }
    return out.length ? out : MIP_STEPS;
  }
  function orthoViewports(needActors) {
    var cs = window.cornerstone, out = [];
    if (!cs || !cs.getRenderingEngines) return out;
    (cs.getRenderingEngines() || []).forEach(function (eng) {
      (eng.getViewports() || []).forEach(function (vp) {
        // Orthographic = volume viewport (3D and stack viewports don't
        // slab). Actors only exist once the volume is mounted and
        // renderable; callers that just need the pane's type pass false.
        if (vp.type === 'orthographic' &&
            typeof vp.setSlabThickness === 'function' &&
            (!needActors || (vp.getActors && vp.getActors().length)))
          out.push(vp);
      });
    });
    return out;
  }
  function mipLabel() {
    var steps = mipSteps();
    return mipIdx < 0 ? 'MIP off'
      : 'MIP ' + steps[Math.min(mipIdx, steps.length - 1)] + ' mm';
  }
  function applyMipState() {
    var vps = orthoViewports(true);
    if (!vps.length) return false;
    var bm = window.cornerstone.Enums.BlendModes;
    var steps = mipSteps();
    vps.forEach(function (vp) {
      if (mipIdx < 0) {
        vp.setBlendMode(bm.COMPOSITE);
        if (vp.resetSlabThickness) vp.resetSlabThickness();
      } else {
        vp.setBlendMode(bm.MAXIMUM_INTENSITY_BLEND);
        vp.setSlabThickness(steps[Math.min(mipIdx, steps.length - 1)]);
      }
      vp.render();
    });
    mipConverting = false;
    return true;
  }
  function scheduleMipApply() {
    // Apply now if a volume is renderable; otherwise poll — large volumes
    // stream for a while, and the requested level (still adjustable with
    // further m/n presses meanwhile) lands the moment rendering works.
    clearTimeout(mipPoll);
    if (applyMipState()) { hud(mipLabel()); return; }
    hud(mipLabel() + ' (loading...)');
    var tries = 600;  // x 500 ms = 5 min
    (function tick() {
      if (applyMipState()) { hud(mipLabel()); return; }
      if (--tries <= 0) { mipConverting = false; return; }
      mipPoll = setTimeout(tick, 500);
    })();
  }
  window.addEventListener('keydown', function (e) {
    var isUp = e.key === 'm' || e.key === 'M';
    var isDown = e.key === 'n' || e.key === 'N';
    if (!isUp && !isDown) return;
    if (e.ctrlKey || e.metaKey || e.altKey) return;
    var a = document.activeElement, tag = a && a.tagName;
    if (tag === 'INPUT' || tag === 'TEXTAREA' || tag === 'SELECT' ||
        (a && a.isContentEditable)) return;
    if (localStorage.getItem('sscMipShimOff') === '1') return;
    if (!window.cornerstone || !window.cornerstone.Enums) return;
    e.preventDefault();
    e.stopPropagation();  // 'n' shadows OHIF's labelmap-interpolate binding
    var steps = mipSteps();
    if (isDown) {
      // Step down from wherever we are; below the first step means off.
      // From off, stay off — never convert the pane for a step-down.
      if (mipIdx < 0) { hud('MIP off'); return; }
      mipIdx -= 1;
      scheduleMipApply();
      return;
    }
    if (!orthoViewports(false).length && !mipConverting) {
      // Stack layout: convert the active pane in place to a volume viewport
      // in acquisition orientation — same image, same single-pane layout —
      // exactly what OHIF's own per-viewport orientation menu does.
      var cm = window.commandsManager, svc = window.services;
      var grid = svc && svc.viewportGridService;
      var dss = svc && svc.displaySetService;
      if (!cm || !grid || !dss) return;
      var vpId = grid.getState().activeViewportId;
      var uids = vpId ? (grid.getDisplaySetsUIDsForViewport(vpId) || []) : [];
      var recon = uids.some(function (u) {
        var ds = dss.getDisplaySetByUID(u);
        return !!(ds && ds.isReconstructable);
      });
      if (!recon) { hud('MIP unavailable'); return; }
      mipConverting = true;
      mipIdx = 0;
      try {
        cm.run('setDisplaySetsForViewports', {viewportsToUpdate: [{
          viewportId: vpId,
          displaySetInstanceUIDs: uids,
          viewportOptions: {
            viewportType: 'orthographic',
            orientation: 'acquisition'
          },
          displaySetOptions: uids.map(function () { return {}; })
        }]});
      } catch (err) {
        mipConverting = false; mipIdx = -1;
        hud('MIP unavailable');
        return;
      }
      scheduleMipApply();
      return;
    }
    // Volume pane exists (or conversion is in flight): step up, wrapping
    // past the last step back to off.
    mipIdx += 1;
    if (mipIdx >= steps.length) mipIdx = -1;
    scheduleMipApply();
  }, true);
})();
</script>""".replace(
    "__PX_PER_SLICE__", str(OHIF_TRACKPAD_PX_PER_SLICE)
).encode()

# ---------------------------------------------------------------------------
# Extra OHIF hotkey defaults (app-config.js)
# ---------------------------------------------------------------------------
# OHIF ships a per-user hotkey editor (Preferences in the top-right menu;
# bindings persist in localStorage `user-preferred-keys`, hashed by
# commandName + commandOptions), but it only lists the *default* bindings.
# In OHIF 3.11 those no longer come from `window.config.hotkeys` (dead
# legacy — the list in the plugin's app-config.js is ignored): on every mode
# entry the CustomizationService rebuilds its 'default' and 'mode' scopes
# from the extension modules and the mode route immediately reads
# `getCustomization('ohif.hotkeyBindings')` into
# `hotkeysManager.setDefaultHotKeys` — one synchronous block, so nothing
# appended to any scope beforehand survives to the read.
#
# The one durable seam is the read itself: wrap
# `customizationService.getCustomization` and append our definitions to the
# 'ohif.hotkeyBindings' result. The wrapper is installed via a property hook
# on `window.services`, which the OHIF cornerstone extension assigns during
# extension init — strictly before the first mode entry — so this is
# race-free, with a short poll as a belt-and-braces fallback. The payload is
# appended to app-config.js (served from inside libOrthancOHIF.so, so not
# editable on disk; deliberately excluded from the immutable cache policy;
# runs before the app bundle, so the hook precedes the assignment).
#
# 'd' -> MPR: the same command the toolbar's MPR button runs
# (`toggleHangingProtocol` with protocolId 'mpr' — a toggle, so a second
# press returns to the previous layout). A hotkey has no enablement
# evaluator, so on a non-reconstructable series OHIF shows its own "The
# hanging protocol could not be applied" toast. 'd' is unbound in OHIF's
# stock bindings; if an upgrade ever claims it, our entry is appended last,
# so its binding wins.
#
# These are *defaults*: the entry shows up in the Preferences hotkey editor,
# and a user who rebinds it there keeps their key (stored preferences are
# looked up by command, not by key). Kill switch:
# localStorage.sscExtraHotkeysOff = '1'.
_OHIF_APP_CONFIG_PATH = "/ohif/app-config.js"
_OHIF_HOTKEYS_MARKER = b"ssc-extra-hotkeys"

_OHIF_EXTRA_HOTKEYS = b"""
/* ssc-extra-hotkeys: injected by web-app routes/proxy.py */
(function () {
  'use strict';
  var EXTRAS = [
    {
      commandName: 'toggleHangingProtocol',
      commandOptions: { protocolId: 'mpr' },
      label: 'MPR',
      keys: ['d'],
      isEditable: true
    }
  ];
  function sameCommand(a, b) {
    return a.commandName === b.commandName &&
      JSON.stringify(a.commandOptions || {}) ===
      JSON.stringify(b.commandOptions || {});
  }
  function withExtras(bindings) {
    if (!Array.isArray(bindings)) return bindings;
    var out = bindings.filter(function (h) {
      return !EXTRAS.some(function (def) { return sameCommand(h, def); });
    });
    return out.concat(EXTRAS);
  }
  var patched = false;
  function patch(services) {
    if (patched || !services || !services.customizationService) return;
    var svc = services.customizationService;
    if (typeof svc.getCustomization !== 'function') return;
    var orig = svc.getCustomization.bind(svc);
    svc.getCustomization = function (id) {
      var value = orig.apply(null, arguments);
      if (id !== 'ohif.hotkeyBindings') return value;
      if (localStorage.getItem('sscExtraHotkeysOff') === '1') return value;
      return withExtras(value);
    };
    patched = true;
  }
  // window.services is assigned once, during OHIF's cornerstone extension
  // init - before the first mode entry reads the hotkey bindings. Hook the
  // assignment so the wrapper is in place the moment the service exists.
  var current = window.services;
  try {
    Object.defineProperty(window, 'services', {
      configurable: true,
      get: function () { return current; },
      set: function (v) { current = v; patch(v); }
    });
  } catch (err) { /* fall through to the poll */ }
  patch(current);
  var tries = 600;  // x 500 ms = 5 min, same budget as the MIP poll
  var poll = setInterval(function () {
    patch(current || window.services);
    if (patched || --tries <= 0) clearInterval(poll);
  }, 500);
})();
"""


def inject_extra_hotkeys(body: bytes) -> bytes:
    """Append the extra hotkey defaults to OHIF's app-config.js.

    Appended, not spliced: app-config.js assigns `window.config` at top level,
    so running last is what guarantees the object exists. No-op when already
    present.
    """
    if _OHIF_HOTKEYS_MARKER in body:
        return body
    return body + _OHIF_EXTRA_HOTKEYS


_OHIF_DIALOG_FIT = b"""\
<style id="ssc-dialog-fit">/* injected by web-app routes/proxy.py */
@media (max-height: 659px) {
  div[role="dialog"].fixed { max-height: 94vh; overflow-y: auto; }
  div.h-\\[500px\\] { height: clamp(180px, calc(100vh - 130px), 500px) !important; }
}
@media (max-width: 479px) {
  div[role="dialog"].fixed { max-width: 96vw; }
}
</style>"""


def inject_wheel_shim(body: bytes) -> bytes:
    """Insert the input shim + dialog-fit CSS into an OHIF entry document.

    Before </head>, so the capture listener is registered before OHIF's
    deferred bundle runs (capture-phase ordering would save us regardless;
    this keeps the intent obvious). The script honours the damping kill
    switch (threshold <= 0); the dialog-fit CSS is unconditional — it
    prevents a viewer crash, not an input preference. Each part is a no-op
    when already present.
    """
    blob = b""
    if OHIF_TRACKPAD_PX_PER_SLICE > 0 and _OHIF_SHIM_MARKER not in body:
        blob += _OHIF_WHEEL_SHIM
    if _OHIF_DIALOG_FIT_MARKER not in body:
        blob += _OHIF_DIALOG_FIT
    if not blob:
        return body
    for anchor in (b"</head>", b"</body>"):
        idx = body.find(anchor)
        if idx != -1:
            return body[:idx] + blob + body[idx:]
    return body + blob


# ---------------------------------------------------------------------------
# OHIF worklist-return replacement (entry documents)
# ---------------------------------------------------------------------------
# OHIF's header has a single clickable block (back arrow + logo, one div with
# data-cy="return-to-work-list") whose onClick navigates to OHIF's study-list
# route — which this deployment does not serve, leaving the viewer wedged on a
# broken page. The CSS below hides and disables that control in every mode
# (visibility, not display, so the 48px header keeps its layout). The script
# then puts a "Close" button in the freed top-left spot, but only when the
# document is top-level (new tab, second-screen popup, direct URL): in the
# embedded preview pane the React overlay in PreviewPane.jsx owns the
# replacement control instead. Closing works wherever the window was opened by
# script (the footer New Tab click, the row OHIF action, the second-screen
# popup — COOP is stripped above precisely so the opener survives); windows
# that cannot self-close (direct URL, middle-clicked link) fall back to the
# app landing page.
_OHIF_CLOSE_MARKER = b"ssc-viewer-close"

_OHIF_VIEWER_CLOSE = b"""\
<style id="ssc-viewer-close-style">/* injected by web-app routes/proxy.py */
[data-cy="return-to-work-list"] { visibility: hidden; pointer-events: none; }
#ssc-viewer-close-btn {
  position: fixed; top: 8px; left: 8px; z-index: 99999;
  background: #1a2256; color: #fff; border: 0; border-radius: 4px;
  padding: 6px 12px; font: 600 12px sans-serif; cursor: pointer;
}
#ssc-viewer-close-btn:hover { background: #090C29; }
</style>
<script id="ssc-viewer-close">/* injected by web-app routes/proxy.py */
(function () {
  'use strict';
  if (window.parent !== window) return;
  function mount() {
    var btn = document.createElement('button');
    btn.id = 'ssc-viewer-close-btn';
    btn.type = 'button';
    btn.title = 'Close this viewer window';
    btn.textContent = '\\u2190 Close';
    btn.addEventListener('click', function () {
      window.close();
      setTimeout(function () {
        if (!window.closed) window.location.assign('/app/');
      }, 200);
    });
    document.body.appendChild(btn);
  }
  if (document.body) mount();
  else document.addEventListener('DOMContentLoaded', mount);
})();
</script>"""


def inject_viewer_close(body: bytes) -> bytes:
    """Insert the worklist-return replacement into an OHIF entry document.

    The CSS neutralizes OHIF's broken return-to-work-list control in every
    mode; the script adds the top-level-only Close button (see the block
    comment above). Unconditional — unlike the trackpad shim there is no
    preference to honour — and a no-op when already present.
    """
    if _OHIF_CLOSE_MARKER in body:
        return body
    for anchor in (b"</head>", b"</body>"):
        idx = body.find(anchor)
        if idx != -1:
            return body[:idx] + _OHIF_VIEWER_CLOSE + body[idx:]
    return body + _OHIF_VIEWER_CLOSE


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

# Upstream response headers to drop in addition to hop-by-hop:
#   cross-origin-opener-policy[-report-only]: Orthanc serves the OHIF build
#   with COOP: same-origin, but the app pages on this origin carry no COOP.
#   Navigating the second-screen popup to a mismatched-COOP document makes
#   the browser sever the opener relationship — window.closed reads true and
#   location.replace() goes nowhere — which breaks routing row clicks to the
#   popup. Cost of stripping: a top-level OHIF document is no longer
#   crossOriginIsolated (no SharedArrayBuffer). The embedded preview pane
#   never was (isolation is decided by its /app top-level page), and OHIF
#   demonstrably runs fine without it there.
_DROP_RESPONSE_HEADERS = _HOP_BY_HOP | {
    "cross-origin-opener-policy",
    "cross-origin-opener-policy-report-only",
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
        if k.lower() not in _DROP_RESPONSE_HEADERS
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
    is_entry_doc = (
        upstream.status_code == 200
        and request.url.path.startswith("/ohif")
        and upstream.headers.get("content-type", "").lower().startswith("text/html")
    )
    is_app_config = (
        upstream.status_code == 200
        and request.url.path == _OHIF_APP_CONFIG_PATH
    )
    if is_entry_doc or is_app_config:
        # The two small, deliberately uncached OHIF files: the entry documents
        # (/ohif/, /ohif/viewer) take the input shim and the worklist-return
        # replacement, app-config.js takes the extra hotkey defaults. Buffer
        # and rewrite in transit — aread() decodes any content-encoding, so
        # that header and the stale content-length must go; Response
        # recomputes the length. Everything else streams below untouched.
        try:
            raw = await upstream.aread()
        finally:
            await upstream.aclose()
        headers.pop("content-encoding", None)
        headers.pop("content-length", None)
        if is_entry_doc:
            raw = inject_viewer_close(inject_wheel_shim(raw))
        else:
            raw = inject_extra_hotkeys(raw)
        if is_app_config:
            # Orthanc sends no validators, and Chrome's memory cache may
            # reuse a validator-less script within a session — which would
            # keep serving a pre-injection copy after a deploy. Forbid reuse;
            # the file is ~6 KB, refetching it is free.
            headers["cache-control"] = "no-store"
        return Response(
            content=raw,
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
