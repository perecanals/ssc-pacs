import { describe, it, expect, vi, beforeEach } from "vitest";
import { renderHook, waitFor } from "@testing-library/react";

const apiGet = vi.fn();
const apiFetch = vi.fn().mockResolvedValue({ ok: true });
vi.mock("../../api/client", () => ({
  apiGet: (...a) => apiGet(...a),
  apiFetch: (...a) => apiFetch(...a),
}));

import useSessionStatePersistence from "../useSessionStatePersistence";

const DEFAULT_FILTERS = {
  label: null,
  labelLevel: null,
  patientId: null,
  modality: null,
  description: null,
  studyImportLabel: null,
  dataset: null,
  labelValues: {},
};

function restore(session) {
  apiGet.mockReset();
  apiGet.mockResolvedValue({ prefs: { session } });
  const { result } = renderHook(
    (props) =>
      useSessionStatePersistence({
        ready: true,
        currentUser: "tester",
        level: "patient",
        filters: DEFAULT_FILTERS,
        previewHeight: null,
        sidebarOpen: true,
        defaultFilters: DEFAULT_FILTERS,
        ...props,
      }),
  );
  return result;
}

describe("useSessionStatePersistence — labelValues restore", () => {
  beforeEach(() => apiGet.mockReset());

  it("round-trips a clean labelValues map and trims/dedupes values", async () => {
    const result = restore({
      level: "series",
      filters: { labelValues: { "series:timepoint": ["a", " b ", "a"] } },
    });
    await waitFor(() => expect(result.current.loaded).toBe(true));
    expect(result.current.restoredFilters.labelValues).toEqual({
      "series:timepoint": ["a", "b"],
    });
  });

  it("drops malformed labelValues entries", async () => {
    const result = restore({
      level: "patient",
      filters: {
        labelValues: {
          "patient:good": ["x", "", 5],
          nokeycolon: ["y"],
          "patient:notarray": "z",
          "patient:empty": ["", "  "],
        },
      },
    });
    await waitFor(() => expect(result.current.loaded).toBe(true));
    expect(result.current.restoredFilters.labelValues).toEqual({
      "patient:good": ["x"],
    });
  });

  it("defaults labelValues to {} when absent or not an object", async () => {
    const result = restore({ level: "patient", filters: { labelValues: "nope" } });
    await waitFor(() => expect(result.current.loaded).toBe(true));
    expect(result.current.restoredFilters.labelValues).toEqual({});
  });
});

describe("useSessionStatePersistence — dataset/import-label restore", () => {
  beforeEach(() => apiGet.mockReset());

  it("keeps dataset + studyImportLabel at series level (no longer pruned)", async () => {
    const result = restore({
      level: "series",
      filters: { dataset: "lvo", studyImportLabel: "PRECISE" },
    });
    await waitFor(() => expect(result.current.loaded).toBe(true));
    expect(result.current.restoredFilters.dataset).toBe("lvo");
    expect(result.current.restoredFilters.studyImportLabel).toBe("PRECISE");
    // Modality stays applicable on series.
    expect(result.current.restoredLevel).toBe("series");
  });

  it("nulls modality when restoring at patient level", async () => {
    const result = restore({ level: "patient", filters: { modality: "CT" } });
    await waitFor(() => expect(result.current.loaded).toBe(true));
    expect(result.current.restoredFilters.modality).toBeNull();
  });
});

describe("useSessionStatePersistence — sidebarOpen", () => {
  beforeEach(() => {
    apiGet.mockReset();
    apiFetch.mockClear();
  });

  it("restores a stored closed sidebar; defaults to open when absent", async () => {
    const closed = restore({ level: "patient", sidebarOpen: false });
    await waitFor(() => expect(closed.current.loaded).toBe(true));
    expect(closed.current.restoredSidebarOpen).toBe(false);

    const absent = restore({ level: "patient" });
    await waitFor(() => expect(absent.current.loaded).toBe(true));
    expect(absent.current.restoredSidebarOpen).toBe(true);
  });

  it("includes sidebarOpen in the debounced save", async () => {
    apiGet.mockReset();
    apiGet.mockResolvedValue({ prefs: { session: null } });
    const { result, rerender } = renderHook(
      ({ sidebarOpen }) =>
        useSessionStatePersistence({
          ready: true,
          currentUser: "tester",
          level: "patient",
          filters: DEFAULT_FILTERS,
          previewHeight: null,
          sidebarOpen,
          defaultFilters: DEFAULT_FILTERS,
        }),
      { initialProps: { sidebarOpen: true } },
    );
    await waitFor(() => expect(result.current.loaded).toBe(true));

    rerender({ sidebarOpen: false });
    await waitFor(() => expect(apiFetch).toHaveBeenCalled(), { timeout: 3000 });
    const body = JSON.parse(apiFetch.mock.calls.at(-1)[1].body);
    expect(body.prefs.session.sidebarOpen).toBe(false);
  });
});

describe("useSessionStatePersistence — previewHeight", () => {
  beforeEach(() => {
    apiGet.mockReset();
    apiFetch.mockClear();
  });

  it("restores a stored height, rounded and clamped to the floor", async () => {
    const result = restore({ level: "patient", previewHeight: 480.6 });
    await waitFor(() => expect(result.current.loaded).toBe(true));
    expect(result.current.restoredPreviewHeight).toBe(481);

    const clamped = restore({ level: "patient", previewHeight: 12 });
    await waitFor(() => expect(clamped.current.loaded).toBe(true));
    expect(clamped.current.restoredPreviewHeight).toBe(320);
  });

  it("restores null when the stored height is missing or not a number", async () => {
    const absent = restore({ level: "patient" });
    await waitFor(() => expect(absent.current.loaded).toBe(true));
    expect(absent.current.restoredPreviewHeight).toBeNull();

    const garbage = restore({ level: "patient", previewHeight: "tall" });
    await waitFor(() => expect(garbage.current.loaded).toBe(true));
    expect(garbage.current.restoredPreviewHeight).toBeNull();
  });

  it("includes previewHeight in the debounced save", async () => {
    apiGet.mockReset();
    apiGet.mockResolvedValue({ prefs: { session: null } });
    const { result, rerender } = renderHook(
      ({ previewHeight }) =>
        useSessionStatePersistence({
          ready: true,
          currentUser: "tester",
          level: "patient",
          filters: DEFAULT_FILTERS,
          previewHeight,
          defaultFilters: DEFAULT_FILTERS,
        }),
      { initialProps: { previewHeight: null } },
    );
    await waitFor(() => expect(result.current.loaded).toBe(true));

    rerender({ previewHeight: 640 });
    await waitFor(() => expect(apiFetch).toHaveBeenCalled(), { timeout: 3000 });
    const body = JSON.parse(apiFetch.mock.calls.at(-1)[1].body);
    expect(body.prefs.session.previewHeight).toBe(640);
  });
});
