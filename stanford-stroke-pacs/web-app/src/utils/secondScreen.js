// Detached OHIF viewer window (the "Second Screen" / "New Window" pane tab).
//
// With a second display attached, Chromium's Window Management API places the
// window fullscreen on the display the app is *not* on. With a single display
// — or in Firefox/Safari, which expose neither `getScreenDetails` nor
// `screen.isExtended` — it opens as a large ordinary popup on the current
// screen for the user to arrange. Row-click routing is identical either way,
// so the feature is always offered; only the placement is an enhancement.
// Requires a secure context; http://localhost through the SSH tunnel qualifies.

// True only while a second display is attached *and* this browser can place a
// window on it. Drives the button label — never a gate on the feature.
// `screen.isExtended` is live and prompt-free, so read it at render time;
// there is no plug/unplug event without getScreenDetails().
export function hasSecondScreen() {
  return (
    typeof window.getScreenDetails === "function" &&
    window.screen?.isExtended === true
  );
}

// Fraction of the available screen the single-display popup covers. Not
// fullscreen: on one monitor the whole point is a window the user can arrange
// beside the table, and a fullscreen popup would simply bury it.
const SINGLE_SCREEN_FILL = 0.8;

function openPopup(url, { left, top, width, height, fullscreen }) {
  const features = [
    "popup",
    ...(fullscreen ? ["fullscreen"] : []),
    `left=${Math.round(left)}`,
    `top=${Math.round(top)}`,
    `width=${Math.round(width)}`,
    `height=${Math.round(height)}`,
  ].join(",");
  return window.open(url, "_blank", features);
}

// Opens `url` in the detached viewer window. Returns the WindowProxy, or null
// when the popup was blocked (or `url` is empty).
//
// First-use caveat on the second-screen path: getScreenDetails() triggers a
// one-time permission prompt, and answering it can outlive the click's
// transient activation, in which case window.open is popup-blocked and returns
// null. The caller should then leave the pane as-is; the next click resolves
// instantly and succeeds.
export async function openViewerWindow(url) {
  if (!url) return null;

  if (hasSecondScreen()) {
    try {
      const details = await window.getScreenDetails();
      const target = details.screens.find((s) => s !== details.currentScreen);
      if (target) {
        return openPopup(url, {
          left: target.availLeft,
          top: target.availTop,
          width: target.availWidth,
          height: target.availHeight,
          fullscreen: true,
        });
      }
    } catch {
      // Permission denied, or the details went away between the check and the
      // call: fall through to a plain popup on this screen rather than losing
      // the feature entirely.
    }
  }

  const {
    availWidth = 1024,
    availHeight = 768,
    availLeft = 0,
    availTop = 0,
  } = window.screen || {};
  const width = availWidth * SINGLE_SCREEN_FILL;
  const height = availHeight * SINGLE_SCREEN_FILL;
  return openPopup(url, {
    left: availLeft + (availWidth - width) / 2,
    top: availTop + (availHeight - height) / 2,
    width,
    height,
    fullscreen: false,
  });
}
