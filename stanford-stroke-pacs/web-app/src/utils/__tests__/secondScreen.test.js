import { describe, it, expect, vi, afterEach } from "vitest";
import { hasSecondScreen, openViewerWindow } from "../secondScreen";

const CURRENT = {
  availLeft: 0,
  availTop: 0,
  availWidth: 1920,
  availHeight: 1080,
};
const OTHER = {
  availLeft: 1920,
  availTop: 0,
  availWidth: 2560,
  availHeight: 1440,
};

function setScreen(props) {
  for (const [k, v] of Object.entries(props)) {
    Object.defineProperty(window.screen, k, { value: v, configurable: true });
  }
}

function features() {
  return window.open.mock.calls[0][2];
}

describe("detached viewer window", () => {
  afterEach(() => {
    vi.restoreAllMocks();
    delete window.getScreenDetails;
    for (const k of [
      "isExtended",
      "availWidth",
      "availHeight",
      "availLeft",
      "availTop",
    ]) {
      delete window.screen[k];
    }
  });

  describe("hasSecondScreen", () => {
    it("is false without the Window Management API (Firefox/Safari)", () => {
      setScreen({ isExtended: true });
      expect(hasSecondScreen()).toBe(false);
    });

    it("is false on Chromium with a single display", () => {
      window.getScreenDetails = vi.fn();
      setScreen({ isExtended: false });
      expect(hasSecondScreen()).toBe(false);
    });

    it("is true on Chromium with an extended desktop", () => {
      window.getScreenDetails = vi.fn();
      setScreen({ isExtended: true });
      expect(hasSecondScreen()).toBe(true);
    });
  });

  describe("openViewerWindow", () => {
    it("fills the other display and asks fullscreen when there is one", async () => {
      window.getScreenDetails = vi.fn().mockResolvedValue({
        currentScreen: CURRENT,
        screens: [CURRENT, OTHER],
      });
      setScreen({ isExtended: true });
      vi.spyOn(window, "open").mockReturnValue({});

      await openViewerWindow("/ohif/viewer?a=1");

      expect(features()).toContain("fullscreen");
      expect(features()).toContain("left=1920");
      expect(features()).toContain("width=2560");
      expect(features()).toContain("height=1440");
    });

    it("opens a centered, non-fullscreen window on a single display", async () => {
      setScreen({
        availWidth: 1000,
        availHeight: 800,
        availLeft: 0,
        availTop: 0,
      });
      vi.spyOn(window, "open").mockReturnValue({});

      const win = await openViewerWindow("/ohif/viewer?a=1");

      expect(win).toBeTruthy();
      // 80% of the available space, centered — a window the user can arrange
      // beside the table rather than one that buries it.
      expect(features()).not.toContain("fullscreen");
      expect(features()).toContain("width=800");
      expect(features()).toContain("height=640");
      expect(features()).toContain("left=100");
      expect(features()).toContain("top=80");
    });

    it("falls back to a plain window when the permission is denied", async () => {
      window.getScreenDetails = vi.fn().mockRejectedValue(new Error("denied"));
      setScreen({ isExtended: true, availWidth: 1000, availHeight: 800 });
      vi.spyOn(window, "open").mockReturnValue({});

      const win = await openViewerWindow("/ohif/viewer?a=1");

      // The feature degrades to this screen instead of vanishing.
      expect(win).toBeTruthy();
      expect(features()).not.toContain("fullscreen");
      expect(features()).toContain("width=800");
    });

    it("returns null without opening anything for an empty URL", async () => {
      vi.spyOn(window, "open").mockReturnValue({});
      expect(await openViewerWindow("")).toBeNull();
      expect(window.open).not.toHaveBeenCalled();
    });
  });
});
