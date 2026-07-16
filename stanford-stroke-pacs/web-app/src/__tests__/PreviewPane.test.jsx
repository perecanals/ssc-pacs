import { render, screen, fireEvent, act } from "@testing-library/react";
import { createRef } from "react";
import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import PreviewPane from "../components/PreviewPane";

// jsdom implements neither requestFullscreen nor exitFullscreen. Stub them and
// drive state by setting document.fullscreenElement + dispatching the event,
// which is how the browser actually reports fullscreen (Esc included).
//
// The act() calls below must use a braced body. `act(() => el.requestFullscreen())`
// implicitly returns the promise, which puts React into *async* act mode; not
// awaiting it corrupts the renderer for every later test in the file (they fail
// with an empty container and a null ref, far from the real cause).
function stubFullscreenApi() {
  Element.prototype.requestFullscreen = vi.fn(function () {
    setFullscreenElement(this);
    return Promise.resolve();
  });
  document.exitFullscreen = vi.fn(() => {
    setFullscreenElement(null);
    return Promise.resolve();
  });
}

function setFullscreenElement(el) {
  Object.defineProperty(document, "fullscreenElement", {
    value: el,
    configurable: true,
    writable: true,
  });
  document.dispatchEvent(new Event("fullscreenchange"));
}

const BASE_PROPS = {
  selection: { rowKey: "study:1.2.3" },
  previewUrl: "/ohif/viewer?StudyInstanceUIDs=1.2.3",
  loading: false,
  loadingLabel: "",
  error: "",
  isOpen: true,
  height: null,
  onHeightChange: () => {},
};

function renderPane(overrides = {}) {
  const paneRef = createRef();
  const utils = render(
    <PreviewPane {...BASE_PROPS} {...overrides} paneRef={paneRef} />,
  );
  return { ...utils, paneRef };
}

describe("PreviewPane", () => {
  beforeEach(() => {
    stubFullscreenApi();
    setFullscreenElement(null);
  });

  afterEach(() => {
    setFullscreenElement(null);
    vi.restoreAllMocks();
  });

  it("renders nothing without a selection", () => {
    const { container } = renderPane({ selection: null });
    expect(container.querySelector("iframe")).toBeNull();
  });

  it("keeps the iframe mounted when collapsed", () => {
    // The whole point of hiding rather than unmounting: re-opening must not
    // cost a full OHIF cold boot.
    const { container } = renderPane({ isOpen: false });
    const frame = container.querySelector("iframe");
    expect(frame).not.toBeNull();
    expect(frame.getAttribute("src")).toBe(BASE_PROPS.previewUrl);
    expect(container.querySelector(".preview-pane--collapsed")).not.toBeNull();
  });

  it("does NOT remount the iframe when entering fullscreen", () => {
    // This is the entire reason the feature exists. If React swaps the element,
    // OHIF reloads and refetches every frame — exactly what we set out to
    // avoid, and the failure would be invisible except as slowness.
    const { container, paneRef } = renderPane();
    const before = container.querySelector("iframe");

    act(() => {
      paneRef.current.requestFullscreen();
    });

    const after = container.querySelector("iframe");
    expect(after).toBe(before); // same DOM node, not merely an equal one
  });

  it("shows an exit control only while fullscreen", () => {
    const { paneRef } = renderPane();
    expect(
      screen.queryByRole("button", { name: /exit fullscreen/i }),
    ).toBeNull();

    act(() => {
      paneRef.current.requestFullscreen();
    });
    expect(
      screen.getByRole("button", { name: /exit fullscreen/i }),
    ).toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: /exit fullscreen/i }));
    expect(document.exitFullscreen).toHaveBeenCalled();
    expect(
      screen.queryByRole("button", { name: /exit fullscreen/i }),
    ).toBeNull();
  });

  it("hides the resize handle while fullscreen", () => {
    const { paneRef } = renderPane();
    expect(screen.getByRole("separator")).toBeInTheDocument();
    act(() => {
      paneRef.current.requestFullscreen();
    });
    expect(screen.queryByRole("separator")).toBeNull();
  });

  it("suppresses the drag-resized height while fullscreen, and restores it", () => {
    // An inline height would fight the :fullscreen rule; on exit the user's
    // drag-resized height must come back untouched.
    const { container, paneRef } = renderPane({ height: 600 });
    const pane = container.querySelector(".preview-pane");
    expect(pane.style.height).toBe("600px");

    act(() => {
      paneRef.current.requestFullscreen();
    });
    expect(pane.style.height).toBe("");

    act(() => {
      document.exitFullscreen();
    });
    expect(pane.style.height).toBe("600px");
  });
});
