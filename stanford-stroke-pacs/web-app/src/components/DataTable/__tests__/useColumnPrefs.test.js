import { describe, it, expect } from "vitest";
import { act, renderHook } from "@testing-library/react";

import useColumnPrefs from "../useColumnPrefs";
import { COLUMN_DEFAULTS_VERSION } from "../../../utils/table";

const OLD_COL = {
  key: "builtin:series:modality",
  sourceKey: "modality",
  level: "series",
};
const NEW_COL = {
  key: "builtin:series:series_type",
  sourceKey: "series_type",
  level: "series",
  introducedIn: 1,
  readOnlyAuto: true,
};
const BUILTINS = [OLD_COL, NEW_COL];

const render = (initialPrefs) =>
  renderHook(() => useColumnPrefs([], BUILTINS, "series", initialPrefs));

describe("useColumnPrefs — newly-introduced builtin columns", () => {
  it("shows them to users with no saved prefs (plain defaultVisible path)", () => {
    const { result } = render({});
    expect(result.current.visibleKeys).toContain(NEW_COL.key);
    expect(result.current.prefsUpgraded).toBe(false);
  });

  it("merges them into saved prefs that predate the marker, once", () => {
    const { result } = render({ visibleKeys: [OLD_COL.key] });
    expect(result.current.visibleKeys).toContain(NEW_COL.key);
    expect(result.current.prefsUpgraded).toBe(true);
  });

  it("does not resurrect a column the user hid after the merge", () => {
    // Marker already current and the key absent = a deliberate hide, not a
    // stale pref. Re-adding it here would make the column impossible to hide.
    const { result } = render({
      visibleKeys: [OLD_COL.key],
      defaultsVersion: COLUMN_DEFAULTS_VERSION,
    });
    expect(result.current.visibleKeys).not.toContain(NEW_COL.key);
    expect(result.current.prefsUpgraded).toBe(false);
  });

  it("brings them back on Reset View", () => {
    const { result } = render({
      visibleKeys: [OLD_COL.key],
      defaultsVersion: COLUMN_DEFAULTS_VERSION,
    });
    act(() => result.current.resetColumns());
    expect(result.current.visibleKeys).toContain(NEW_COL.key);
  });
});

// Column prefs are saved per user and outlive the columns they name. A builtin
// that is retired (femoral_sheath_time, Alembic 0018/v1.13) leaves a dangling
// key in every saved pref that had it selected. Resolution is by lookup against
// the live catalog, so an unknown key matches nothing and is simply not
// rendered — it must never throw or blank the table.
const RETIRED_KEY = "builtin:patient:femoral_sheath_time";

describe("useColumnPrefs — prefs naming a column that no longer exists", () => {
  it("ignores the dangling key and still renders the surviving columns", () => {
    const { result } = render({
      visibleKeys: [OLD_COL.key, RETIRED_KEY],
      defaultsVersion: COLUMN_DEFAULTS_VERSION,
    });
    expect(result.current.visibleCols.map((c) => c.key)).toEqual([OLD_COL.key]);
    expect(result.current.allCols.map((c) => c.key)).not.toContain(RETIRED_KEY);
  });

  it("survives a saved column order that references it", () => {
    const { result } = render({
      visibleKeys: [OLD_COL.key, RETIRED_KEY],
      columnOrder: [RETIRED_KEY, OLD_COL.key],
      defaultsVersion: COLUMN_DEFAULTS_VERSION,
    });
    expect(result.current.visibleCols.map((c) => c.key)).toEqual([OLD_COL.key]);
  });

  it("survives prefs that name ONLY the retired column", () => {
    // The worst case: nothing left to render from the saved set. An empty table
    // is recoverable via Reset View; a crash is not.
    const { result } = render({
      visibleKeys: [RETIRED_KEY],
      defaultsVersion: COLUMN_DEFAULTS_VERSION,
    });
    expect(result.current.visibleCols).toEqual([]);
    act(() => result.current.resetColumns());
    expect(result.current.visibleKeys).toContain(NEW_COL.key);
  });
});
