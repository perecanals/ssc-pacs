import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { renderHook, act, waitFor } from "@testing-library/react";

const openViewerWindow = vi.fn();
vi.mock("../../utils/secondScreen", () => ({
  openViewerWindow: (...a) => openViewerWindow(...a),
}));

import useSecondScreenViewer from "../useSecondScreenViewer";

function makePopup() {
  return {
    closed: false,
    focus: vi.fn(),
    location: { replace: vi.fn() },
  };
}

describe("useSecondScreenViewer", () => {
  beforeEach(() => {
    openViewerWindow.mockReset();
  });

  afterEach(() => {
    vi.useRealTimers();
  });

  it("open() activates on success and re-points instead of reopening", async () => {
    const popup = makePopup();
    openViewerWindow.mockResolvedValue(popup);
    const { result } = renderHook(() => useSecondScreenViewer());

    await act(async () => {
      expect(await result.current.open("/ohif/viewer?a=1")).toBe(true);
    });
    expect(result.current.active).toBe(true);
    expect(result.current.isLive()).toBe(true);

    // Second open() with a live popup must not spawn another window; it
    // re-points the existing one (the pane -> second-screen handoff).
    await act(async () => {
      expect(await result.current.open("/ohif/viewer?a=2")).toBe(true);
    });
    expect(openViewerWindow).toHaveBeenCalledTimes(1);
    expect(popup.location.replace).toHaveBeenCalledWith("/ohif/viewer?a=2");
    expect(popup.focus).toHaveBeenCalled();
  });

  it("open() with no URL just focuses a live popup", async () => {
    const popup = makePopup();
    openViewerWindow.mockResolvedValue(popup);
    const { result } = renderHook(() => useSecondScreenViewer());
    await act(async () => {
      await result.current.open("/ohif/viewer?a=1");
    });

    await act(async () => {
      expect(await result.current.open("")).toBe(true);
    });
    expect(popup.location.replace).not.toHaveBeenCalled();
    expect(popup.focus).toHaveBeenCalled();
  });

  it("open() stays inactive when the popup is blocked or denied", async () => {
    openViewerWindow.mockResolvedValue(null);
    const { result } = renderHook(() => useSecondScreenViewer());

    await act(async () => {
      expect(await result.current.open("/ohif/viewer?a=1")).toBe(false);
    });
    expect(result.current.active).toBe(false);
    expect(result.current.isLive()).toBe(false);
  });

  it("navigate() replaces only on a new URL and always focuses", async () => {
    const popup = makePopup();
    openViewerWindow.mockResolvedValue(popup);
    const { result } = renderHook(() => useSecondScreenViewer());
    await act(async () => {
      await result.current.open("/ohif/viewer?a=1");
    });

    // Same URL as the one the popup was opened with: no reload.
    act(() => {
      expect(result.current.navigate("/ohif/viewer?a=1")).toBe(true);
    });
    expect(popup.location.replace).not.toHaveBeenCalled();

    act(() => {
      expect(result.current.navigate("/ohif/viewer?a=2")).toBe(true);
    });
    expect(popup.location.replace).toHaveBeenCalledWith("/ohif/viewer?a=2");

    // Re-click of the now-current study: still no second reload.
    act(() => {
      result.current.navigate("/ohif/viewer?a=2");
    });
    expect(popup.location.replace).toHaveBeenCalledTimes(1);
    expect(popup.focus).toHaveBeenCalled();
  });

  it("navigate() refuses when the popup is closed or the URL empty", async () => {
    const popup = makePopup();
    openViewerWindow.mockResolvedValue(popup);
    const { result } = renderHook(() => useSecondScreenViewer());
    await act(async () => {
      await result.current.open("/ohif/viewer?a=1");
    });

    expect(result.current.navigate("")).toBe(false);
    popup.closed = true;
    expect(result.current.navigate("/ohif/viewer?a=2")).toBe(false);
    expect(popup.location.replace).not.toHaveBeenCalled();
  });

  it("polls the popup and deactivates once the user closes it", async () => {
    vi.useFakeTimers();
    const popup = makePopup();
    openViewerWindow.mockResolvedValue(popup);
    const { result } = renderHook(() => useSecondScreenViewer());
    await act(async () => {
      await result.current.open("/ohif/viewer?a=1");
    });
    act(() => {
      result.current.setStatus("Warming imaging cache…");
    });

    popup.closed = true;
    act(() => {
      vi.advanceTimersByTime(1100);
    });
    vi.useRealTimers();

    await waitFor(() => expect(result.current.active).toBe(false));
    expect(result.current.status).toBe("");
    expect(result.current.isLive()).toBe(false);
  });
});
