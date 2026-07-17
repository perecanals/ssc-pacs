import { describe, it, expect, vi, beforeEach } from "vitest";
import { renderHook, waitFor } from "@testing-library/react";

const apiFetch = vi.fn().mockResolvedValue({ ok: true });
vi.mock("../../../api/client", () => ({
  apiFetch: (...a) => apiFetch(...a),
}));

import usePreferencePersistence from "../usePreferencePersistence";
import { COLUMN_DEFAULTS_VERSION } from "../../../utils/table";

const BASE = {
  currentUser: "tester",
  level: "patient",
  visibleKeys: ["builtin:patient:patient_id"],
  columnOrder: [],
  sortBy: "patient_id",
  sortDir: "asc",
  columnFilters: {},
  frozenFirstCol: false,
  fontScale: 1,
  statusColVisible: true,
};

function renderPersistence(overrides = {}) {
  return renderHook(
    (props) => usePreferencePersistence({ ...BASE, ...props }),
    {
      initialProps: overrides,
    },
  );
}

describe("usePreferencePersistence", () => {
  beforeEach(() => apiFetch.mockClear());

  it("does not save on mount; one debounced PUT with the latest prefs after a change", async () => {
    const { rerender } = renderPersistence();
    expect(apiFetch).not.toHaveBeenCalled();

    // Two rapid changes must collapse into a single PUT carrying the last state.
    rerender({ sortBy: "stroke_date" });
    rerender({ sortBy: "stroke_date", sortDir: "desc" });

    await waitFor(() => expect(apiFetch).toHaveBeenCalledTimes(1), {
      timeout: 3000,
    });
    const [path, opts] = apiFetch.mock.calls[0];
    expect(path).toBe("/api/preferences/patient");
    expect(opts.method).toBe("PUT");
    const body = JSON.parse(opts.body);
    expect(body.prefs.sortBy).toBe("stroke_date");
    expect(body.prefs.sortDir).toBe("desc");
  });

  it("drops empty column filters from the saved prefs", async () => {
    const { rerender } = renderPersistence();
    rerender({
      columnFilters: { modality: "CT", studydescription: null, dataset: "" },
    });

    await waitFor(() => expect(apiFetch).toHaveBeenCalled(), { timeout: 3000 });
    const body = JSON.parse(apiFetch.mock.calls.at(-1)[1].body);
    expect(body.prefs.columnFilters).toEqual({ modality: "CT" });
  });

  it("flushes a pending save on unmount with keepalive", async () => {
    const { rerender, unmount } = renderPersistence();
    rerender({ fontScale: 1.1 });
    expect(apiFetch).not.toHaveBeenCalled(); // still inside the debounce window

    unmount();
    expect(apiFetch).toHaveBeenCalledTimes(1);
    const [, opts] = apiFetch.mock.calls[0];
    expect(opts.keepalive).toBe(true);
    expect(JSON.parse(opts.body).prefs.fontScale).toBe(1.1);
  });

  it("does not save when logged out", async () => {
    const { rerender, unmount } = renderPersistence({ currentUser: null });
    rerender({ currentUser: null, sortDir: "desc" });
    unmount();
    await new Promise((r) => setTimeout(r, 1000));
    expect(apiFetch).not.toHaveBeenCalled();
  });

  // Without this, useColumnPrefs re-merges the new columns on every load and a
  // user who hid one can never make it stay hidden.
  it("saves on mount when useColumnPrefs merged newly-introduced columns", async () => {
    renderPersistence({ prefsUpgraded: true });

    await waitFor(() => expect(apiFetch).toHaveBeenCalledTimes(1), {
      timeout: 3000,
    });
    const body = JSON.parse(apiFetch.mock.calls[0][1].body);
    expect(body.prefs.defaultsVersion).toBe(COLUMN_DEFAULTS_VERSION);
  });

  it("stamps the current defaultsVersion on every save", async () => {
    const { rerender } = renderPersistence();
    rerender({ sortDir: "desc" });

    await waitFor(() => expect(apiFetch).toHaveBeenCalled(), { timeout: 3000 });
    const body = JSON.parse(apiFetch.mock.calls.at(-1)[1].body);
    expect(body.prefs.defaultsVersion).toBe(COLUMN_DEFAULTS_VERSION);
  });
});

// Saved prefs outlive the columns they name: a retired builtin
// (femoral_sheath_time, v1.13) or a deleted label leaves a dead key behind.
// Reading already tolerates them; these pin that we also stop writing them back.
describe("usePreferencePersistence — pruning prefs that name dead columns", () => {
  beforeEach(() => apiFetch.mockClear());

  const LIVE = {
    key: "builtin:patient:stroke_date",
    sourceKey: "stroke_date",
    builtin: true,
  };
  const LABEL = { key: "label:series_type", builtin: false };
  const ALL_COLS = [LIVE, LABEL];
  const DEAD = "builtin:patient:femoral_sheath_time";

  const withCatalog = (extra = {}) => ({
    allCols: ALL_COLS,
    catalogReady: true,
    ...extra,
  });

  it("self-heals on load: drops the dead key without waiting for an interaction", async () => {
    renderPersistence(
      withCatalog({
        visibleKeys: [LIVE.key, DEAD],
        columnOrder: [DEAD, LIVE.key],
      }),
    );
    await waitFor(() => expect(apiFetch).toHaveBeenCalled(), { timeout: 3000 });
    const body = JSON.parse(apiFetch.mock.calls.at(-1)[1].body);
    expect(body.prefs.visibleKeys).toEqual([LIVE.key]);
    expect(body.prefs.columnOrder).toEqual([LIVE.key]);
  });

  it("drops a filter naming a dead column", async () => {
    renderPersistence(
      withCatalog({
        columnFilters: { stroke_date: "2025", femoral_sheath_time: "09:30" },
      }),
    );
    await waitFor(() => expect(apiFetch).toHaveBeenCalled(), { timeout: 3000 });
    const body = JSON.parse(apiFetch.mock.calls.at(-1)[1].body);
    expect(body.prefs.columnFilters).toEqual({ stroke_date: "2025" });
  });

  it("keeps builtin filters, which are keyed by sourceKey not by column key", async () => {
    // The namespaces differ (TableHeader filters builtins by sourceKey, labels
    // by key). Pruning filters against the column-key set would wipe every
    // builtin filter — this is the regression guard for that.
    renderPersistence(
      withCatalog({
        columnFilters: { stroke_date: "2025", "label:series_type": "CTA" },
      }),
    );
    await waitFor(() => expect(apiFetch).toHaveBeenCalled(), { timeout: 3000 });
    const body = JSON.parse(apiFetch.mock.calls.at(-1)[1].body);
    expect(body.prefs.columnFilters).toEqual({
      stroke_date: "2025",
      "label:series_type": "CTA",
    });
  });

  it("does not prune before the label catalog has loaded", async () => {
    // labelDefs arrive async. Pruning against a catalog that has not landed
    // would delete every saved label column — so nothing is pruned, and with
    // nothing to prune there is no reason to save on mount either.
    const { rerender } = renderPersistence({
      allCols: [LIVE],
      catalogReady: false,
      visibleKeys: [LIVE.key, LABEL.key],
    });
    await new Promise((r) => setTimeout(r, 600));
    expect(apiFetch).not.toHaveBeenCalled();

    rerender({
      allCols: [LIVE],
      catalogReady: false,
      visibleKeys: [LIVE.key, LABEL.key],
      sortDir: "desc",
    });
    await waitFor(() => expect(apiFetch).toHaveBeenCalled(), { timeout: 3000 });
    const body = JSON.parse(apiFetch.mock.calls.at(-1)[1].body);
    expect(body.prefs.visibleKeys).toEqual([LIVE.key, LABEL.key]);
  });

  it("does not save on mount when there is nothing to prune", async () => {
    renderPersistence(withCatalog({ visibleKeys: [LIVE.key, LABEL.key] }));
    await new Promise((r) => setTimeout(r, 600));
    expect(apiFetch).not.toHaveBeenCalled();
  });
});
