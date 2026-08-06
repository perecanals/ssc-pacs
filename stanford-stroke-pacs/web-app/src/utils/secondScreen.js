// Second-monitor support for the OHIF preview, via the Window Management API.
// Chromium-only (Chrome/Edge 100+; the `fullscreen` window feature needs 119+);
// Firefox/Safari expose neither getScreenDetails nor screen.isExtended, so the
// UI must treat this as a progressive enhancement. Requires a secure context —
// http://localhost through the SSH tunnel qualifies.

// Feature gate for the "Second Screen" pane tab. `screen.isExtended` is a live,
// prompt-free boolean that is true only while more than one display is
// attached, so this hides the button on single-monitor setups too. Read it at
// render time — there is no plug/unplug event without getScreenDetails().
export function canUseSecondScreen() {
  return (
    typeof window.getScreenDetails === "function" &&
    window.screen?.isExtended === true
  );
}

// Opens `url` as a popup filling the display the app window is NOT on, and
// asks for it fullscreen (Chrome 119+ honours the `fullscreen` feature when
// the window-management permission is granted; older versions ignore it and
// still get the right position/size). Returns the WindowProxy, or null when
// the user denied the permission or the popup was blocked.
//
// First-use caveat: getScreenDetails() triggers a one-time permission prompt,
// and answering it can outlive the click's transient activation, in which case
// window.open is popup-blocked and returns null. The caller should then leave
// the pane as-is; the next click resolves instantly and succeeds.
export async function openOnSecondScreen(url) {
  let details;
  try {
    details = await window.getScreenDetails();
  } catch {
    return null;
  }
  const target =
    details.screens.find((s) => s !== details.currentScreen) ??
    details.currentScreen;
  const features = [
    "popup",
    "fullscreen",
    `left=${target.availLeft}`,
    `top=${target.availTop}`,
    `width=${target.availWidth}`,
    `height=${target.availHeight}`,
  ].join(",");
  return window.open(url, "_blank", features);
}
