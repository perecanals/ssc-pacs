import { describe, it, expect, vi, beforeEach } from "vitest";
import { renderHook, act, waitFor } from "@testing-library/react";

// Mock the API client; apiGet returns 2 rows per page out of a 5-row total so
// hasMore flips false once everything is loaded.
const apiGet = vi.fn();
vi.mock("../../../api/client", () => ({ apiGet: (...a) => apiGet(...a) }));

import useTableData from "../useTableData";

const ROWS = ["a", "b", "c", "d", "e"].map((id) => ({ id }));
const config = { endpoint: "/api/things", itemsKey: "items", filterParamMap: {} };

function pageResponse(page) {
  const start = (page - 1) * 2;
  return { total: ROWS.length, page, per_page: 50, items: ROWS.slice(start, start + 2) };
}

const baseArgs = {
  level: "patient",
  config,
  filters: {},
  sortBy: "id",
  sortDir: "asc",
  columnFilters: {},
  allCols: [],
};

beforeEach(() => {
  apiGet.mockReset();
  apiGet.mockImplementation((url) => {
    const page = Number(new URL(url, "http://x").searchParams.get("page"));
    return Promise.resolve(pageResponse(page));
  });
});

describe("useTableData (infinite scroll)", () => {
  it("loads page 1 on mount", async () => {
    const { result } = renderHook(() => useTableData(baseArgs));
    await waitFor(() => expect(result.current.items).toHaveLength(2));
    expect(result.current.total).toBe(5);
    expect(result.current.hasMore).toBe(true);
    expect(result.current.items.map((r) => r.id)).toEqual(["a", "b"]);
  });

  it("loadMore appends the next page preserving order", async () => {
    const { result } = renderHook(() => useTableData(baseArgs));
    await waitFor(() => expect(result.current.items).toHaveLength(2));
    act(() => result.current.loadMore());
    await waitFor(() => expect(result.current.items).toHaveLength(4));
    expect(result.current.items.map((r) => r.id)).toEqual(["a", "b", "c", "d"]);
    expect(result.current.hasMore).toBe(true);
  });

  it("hasMore turns false once all rows are loaded", async () => {
    const { result } = renderHook(() => useTableData(baseArgs));
    await waitFor(() => expect(result.current.items).toHaveLength(2));
    act(() => result.current.loadMore());
    await waitFor(() => expect(result.current.items).toHaveLength(4));
    act(() => result.current.loadMore());
    await waitFor(() => expect(result.current.items).toHaveLength(5));
    expect(result.current.hasMore).toBe(false);
  });

  it("a sort change replaces the list (no append leakage) and bumps resetNonce", async () => {
    const { result, rerender } = renderHook((args) => useTableData(args), {
      initialProps: baseArgs,
    });
    await waitFor(() => expect(result.current.items).toHaveLength(2));
    act(() => result.current.loadMore());
    await waitFor(() => expect(result.current.items).toHaveLength(4));
    const nonceBefore = result.current.resetNonce;

    rerender({ ...baseArgs, sortDir: "desc" });
    await waitFor(() => expect(result.current.items).toHaveLength(2));
    expect(result.current.items.map((r) => r.id)).toEqual(["a", "b"]);
    expect(result.current.resetNonce).toBeGreaterThan(nonceBefore);
  });

  it("does not reset when inputs change identity but not content (annotation-save churn)", async () => {
    // After an annotation save, handleMutated refetches label definitions,
    // which rebuilds allCols/filters with identical content but fresh object
    // identities. That must NOT reset the list (which would collapse to page 1
    // and scroll the table to the top).
    const makeArgs = () => ({
      ...baseArgs,
      filters: { labelValues: {} },
      columnFilters: {},
      allCols: [{ key: "label:timepoint", level: "series", datatype: "select" }],
    });
    const { result, rerender } = renderHook((args) => useTableData(args), {
      initialProps: makeArgs(),
    });
    await waitFor(() => expect(result.current.items).toHaveLength(2));
    act(() => result.current.loadMore());
    await waitFor(() => expect(result.current.items).toHaveLength(4));
    const nonceBefore = result.current.resetNonce;

    apiGet.mockClear();
    rerender(makeArgs()); // all-new identities, same content
    await new Promise((r) => setTimeout(r, 20));

    expect(result.current.resetNonce).toBe(nonceBefore);
    expect(result.current.items).toHaveLength(4);
    expect(apiGet).not.toHaveBeenCalled();
  });

  it("merges filters.labelValues into the label_filters param (union with column filter)", async () => {
    const allCols = [{ key: "label:timepoint", level: "series", datatype: "select" }];
    const args = {
      ...baseArgs,
      level: "patient",
      allCols,
      columnFilters: { "label:timepoint": ["baseline"] },
      filters: {
        labelValues: {
          "series:timepoint": ["followup"],
          "patient:outcome": ["good", "bad"],
        },
      },
    };
    renderHook(() => useTableData(args));
    await waitFor(() => expect(apiGet).toHaveBeenCalled());

    const url = new URL(apiGet.mock.calls[0][0], "http://x");
    const labelFilters = JSON.parse(url.searchParams.get("label_filters"));

    const timepoint = labelFilters.find((f) => f.label === "timepoint");
    expect(timepoint).toMatchObject({ level: "series", datatype: "select" });
    expect([...timepoint.values].sort()).toEqual(["baseline", "followup"]);

    const outcome = labelFilters.find((f) => f.label === "outcome");
    expect(outcome).toMatchObject({ level: "patient", datatype: "select" });
    expect([...outcome.values].sort()).toEqual(["bad", "good"]);
  });

  it("omits label_filters when labelValues is empty", async () => {
    const args = { ...baseArgs, filters: { labelValues: {} } };
    renderHook(() => useTableData(args));
    await waitFor(() => expect(apiGet).toHaveBeenCalled());
    const url = new URL(apiGet.mock.calls[0][0], "http://x");
    expect(url.searchParams.get("label_filters")).toBeNull();
  });

  it("emits dataset + study_import_label at patient level", async () => {
    const args = {
      ...baseArgs,
      level: "patient",
      filters: { dataset: "lvo", studyImportLabel: "PRECISE" },
    };
    renderHook(() => useTableData(args));
    await waitFor(() => expect(apiGet).toHaveBeenCalled());
    const url = new URL(apiGet.mock.calls[0][0], "http://x");
    expect(url.searchParams.get("dataset")).toBe("lvo");
    expect(url.searchParams.get("study_import_label")).toBe("PRECISE");
    expect(url.searchParams.get("import_label")).toBeNull();
  });

  it("emits dataset + import_label (not study_import_label) at study/series level", async () => {
    const args = {
      ...baseArgs,
      level: "study",
      filters: { dataset: "lvo", studyImportLabel: "PRECISE" },
    };
    renderHook(() => useTableData(args));
    await waitFor(() => expect(apiGet).toHaveBeenCalled());
    const url = new URL(apiGet.mock.calls[0][0], "http://x");
    expect(url.searchParams.get("dataset")).toBe("lvo");
    expect(url.searchParams.get("import_label")).toBe("PRECISE");
    expect(url.searchParams.get("study_import_label")).toBeNull();
  });

  it("reload re-fetches pages 1..N and replaces", async () => {
    const { result } = renderHook(() => useTableData(baseArgs));
    await waitFor(() => expect(result.current.items).toHaveLength(2));
    act(() => result.current.loadMore());
    await waitFor(() => expect(result.current.items).toHaveLength(4));

    apiGet.mockClear();
    act(() => result.current.reload());
    await waitFor(() => expect(result.current.items).toHaveLength(4));
    // pages 1 and 2 both re-requested
    const pages = apiGet.mock.calls
      .map((c) => Number(new URL(c[0], "http://x").searchParams.get("page")))
      .sort();
    expect(pages).toEqual([1, 2]);
    expect(result.current.items.map((r) => r.id)).toEqual(["a", "b", "c", "d"]);
  });
});
