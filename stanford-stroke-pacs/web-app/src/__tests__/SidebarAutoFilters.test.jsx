import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import {
  render,
  screen,
  fireEvent,
  act,
  waitFor,
} from "@testing-library/react";

const apiGet = vi.fn();
vi.mock("../api/client", () => ({ apiGet: (...a) => apiGet(...a) }));

import Sidebar from "../components/Sidebar";

const VOCAB = {
  series_types: [
    { value: "NCCT", count: 3730 },
    { value: "CTA", count: 3199 },
  ],
  timepoints: [
    { value: "BL", count: 3267 },
    { value: "THROMBECTOMY", count: 577 },
  ],
};

// Hover-open delay in LabelValueFilter (HOVER_OPEN_MS).
const OPEN_DELAY_MS = 200;

function mockApi(vocab = VOCAB) {
  apiGet.mockImplementation((path) => {
    if (path === "/api/classification-values") return Promise.resolve(vocab);
    if (path === "/api/labels/summary") return Promise.resolve([]);
    if (path === "/api/label-definitions") return Promise.resolve([]);
    return Promise.resolve([]);
  });
}

function renderSidebar(filters = {}, onFilterChange = vi.fn()) {
  render(
    <Sidebar
      level="series"
      filters={{ autoValues: {}, labelValues: {}, ...filters }}
      onFilterChange={onFilterChange}
      open
      onToggle={vi.fn()}
    />,
  );
  return onFilterChange;
}

describe("Sidebar — Auto classification quick filters", () => {
  beforeEach(() => {
    apiGet.mockReset();
    mockApi();
  });
  afterEach(() => vi.useRealTimers());

  it("renders a row per Auto column, with the classified count", async () => {
    renderSidebar();
    expect(await screen.findByText("Auto Classification")).toBeInTheDocument();
    expect(screen.getByLabelText("Auto Series Type")).toBeInTheDocument();
    expect(screen.getByLabelText("Auto Timepoint")).toBeInTheDocument();
    // Badge is the total classified count for that column (3730 + 3199).
    expect(screen.getByLabelText("Auto Series Type").textContent).toContain(
      "6929",
    );
  });

  it("selecting a value pushes it into filters.autoValues, keyed by API field", async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    const onFilterChange = renderSidebar();
    const row = await screen.findByLabelText("Auto Series Type");

    fireEvent.mouseEnter(row);
    act(() => vi.advanceTimersByTime(OPEN_DELAY_MS));
    fireEvent.click(screen.getByText("NCCT"));

    expect(onFilterChange).toHaveBeenCalledWith({
      autoValues: { series_type: ["NCCT"] },
    });
  });

  it("adds to the existing selection rather than replacing it (multi-select)", async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    const onFilterChange = renderSidebar({
      autoValues: { series_type: ["NCCT"] },
    });
    const row = await screen.findByLabelText("Auto Series Type");

    fireEvent.mouseEnter(row);
    act(() => vi.advanceTimersByTime(OPEN_DELAY_MS));
    fireEvent.click(screen.getByText("CTA"));

    expect(onFilterChange).toHaveBeenCalledWith({
      autoValues: { series_type: ["NCCT", "CTA"] },
    });
  });

  it("deselecting the last value drops the field entirely", async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    const onFilterChange = renderSidebar({
      autoValues: { series_type: ["NCCT"] },
    });
    const row = await screen.findByLabelText("Auto Series Type");

    fireEvent.mouseEnter(row);
    act(() => vi.advanceTimersByTime(OPEN_DELAY_MS));
    fireEvent.click(screen.getByText("NCCT"));

    expect(onFilterChange).toHaveBeenCalledWith({ autoValues: {} });
  });

  it("hides the section when nothing has been classified yet", async () => {
    mockApi({ series_types: [], timepoints: [] });
    renderSidebar();
    await waitFor(() =>
      expect(apiGet).toHaveBeenCalledWith("/api/classification-values"),
    );
    expect(screen.queryByText("Auto Classification")).toBeNull();
  });
});
