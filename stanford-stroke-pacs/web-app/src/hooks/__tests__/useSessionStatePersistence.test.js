import { describe, it, expect, vi, beforeEach } from "vitest";
import { renderHook, waitFor } from "@testing-library/react";

const apiGet = vi.fn();
vi.mock("../../api/client", () => ({
  apiGet: (...a) => apiGet(...a),
  apiFetch: vi.fn().mockResolvedValue({ ok: true }),
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
  const { result } = renderHook(() =>
    useSessionStatePersistence({
      ready: true,
      currentUser: "tester",
      level: "patient",
      filters: DEFAULT_FILTERS,
      defaultFilters: DEFAULT_FILTERS,
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
