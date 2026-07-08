import { describe, it, expect, vi, beforeEach } from "vitest";
import { renderHook, waitFor } from "@testing-library/react";

const apiFetch = vi.fn().mockResolvedValue({ ok: true });
vi.mock("../../../api/client", () => ({
  apiFetch: (...a) => apiFetch(...a),
}));

import usePreferencePersistence from "../usePreferencePersistence";

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
  return renderHook((props) => usePreferencePersistence({ ...BASE, ...props }), {
    initialProps: overrides,
  });
}

describe("usePreferencePersistence", () => {
  beforeEach(() => apiFetch.mockClear());

  it("does not save on mount; one debounced PUT with the latest prefs after a change", async () => {
    const { rerender } = renderPersistence();
    expect(apiFetch).not.toHaveBeenCalled();

    // Two rapid changes must collapse into a single PUT carrying the last state.
    rerender({ sortBy: "stroke_date" });
    rerender({ sortBy: "stroke_date", sortDir: "desc" });

    await waitFor(() => expect(apiFetch).toHaveBeenCalledTimes(1), { timeout: 3000 });
    const [path, opts] = apiFetch.mock.calls[0];
    expect(path).toBe("/api/preferences/patient");
    expect(opts.method).toBe("PUT");
    const body = JSON.parse(opts.body);
    expect(body.prefs.sortBy).toBe("stroke_date");
    expect(body.prefs.sortDir).toBe("desc");
  });

  it("drops empty column filters from the saved prefs", async () => {
    const { rerender } = renderPersistence();
    rerender({ columnFilters: { modality: "CT", studydescription: null, dataset: "" } });

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
});
