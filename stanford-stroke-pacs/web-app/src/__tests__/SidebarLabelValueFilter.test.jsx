import { describe, it, expect, vi, afterEach } from "vitest";
import { render, screen, fireEvent, act } from "@testing-library/react";

import LabelValueFilter from "../components/Sidebar/LabelValueFilter";

// Hover-open delay in LabelValueFilter (HOVER_OPEN_MS) — advance past it.
const OPEN_DELAY_MS = 200;

function renderHoverOpen() {
  render(
    <ul>
      <LabelValueFilter
        label="timepoint"
        caseCount={3}
        options={["baseline", "followup", "late"]}
        selected={[]}
        pinned={false}
        onToggleValue={vi.fn()}
        onClear={vi.fn()}
        onTogglePin={vi.fn()}
      />
    </ul>,
  );
  fireEvent.mouseEnter(screen.getByLabelText("timepoint"));
  act(() => vi.advanceTimersByTime(OPEN_DELAY_MS));
  const popup = document.body.querySelector(".sidebar__lvf-popup");
  expect(popup).not.toBeNull();
  return popup;
}

describe("LabelValueFilter hover popup", () => {
  afterEach(() => {
    vi.useRealTimers();
  });

  it("stays open when its own option list scrolls (mouse-wheel inside popup)", () => {
    vi.useFakeTimers();
    const popup = renderHoverOpen();
    fireEvent.scroll(popup.querySelector(".sidebar__lvf-options"));
    expect(document.body.querySelector(".sidebar__lvf-popup")).not.toBeNull();
  });

  it("closes when something outside the popup scrolls (anchor detaches)", () => {
    vi.useFakeTimers();
    renderHoverOpen();
    fireEvent.scroll(document.body);
    expect(document.body.querySelector(".sidebar__lvf-popup")).toBeNull();
  });
});
