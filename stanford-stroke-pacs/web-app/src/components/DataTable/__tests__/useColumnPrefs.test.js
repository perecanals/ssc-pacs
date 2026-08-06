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

// Subtable (child/grandchild) column order: a per-sublevel key array saved as
// prefs.subtableColumnOrder. Default composition is visible builtins in
// catalog order followed by that level's label columns; a saved order sorts
// the whole set, so builtins and labels can interleave.
const PATIENT_COL = {
  key: "builtin:patient:patient_id",
  sourceKey: "patient_id",
  level: "patient",
};
const STUDY_A = {
  key: "builtin:study:studydate",
  sourceKey: "studydate",
  level: "study",
};
const STUDY_B = {
  key: "builtin:study:studydescription",
  sourceKey: "studydescription",
  level: "study",
};
const SERIES_COL = {
  key: "builtin:series:modality",
  sourceKey: "modality",
  level: "series",
};
const SUB_BUILTINS = [PATIENT_COL, STUDY_A, STUDY_B, SERIES_COL];
const STUDY_LABEL = { name: "read_status", level: "study" };
const LABEL_KEY = "label:read_status";
const ALL_SUB_KEYS = [...SUB_BUILTINS.map((c) => c.key), LABEL_KEY];

const renderSub = (initialPrefs = {}) =>
  renderHook(() =>
    useColumnPrefs([STUDY_LABEL], SUB_BUILTINS, "patient", {
      visibleKeys: ALL_SUB_KEYS,
      defaultsVersion: COLUMN_DEFAULTS_VERSION,
      ...initialPrefs,
    }),
  );

const studyKeys = (result) =>
  result.current.subtableColsForLevel("study").map((c) => c.key);

describe("useColumnPrefs — subtable column order", () => {
  it("defaults to builtins in catalog order, then labels", () => {
    const { result } = renderSub();
    expect(studyKeys(result)).toEqual([STUDY_A.key, STUDY_B.key, LABEL_KEY]);
  });

  it("applies a saved order, including a label between builtins", () => {
    const { result } = renderSub({
      subtableColumnOrder: { study: [STUDY_A.key, LABEL_KEY, STUDY_B.key] },
    });
    expect(studyKeys(result)).toEqual([STUDY_A.key, LABEL_KEY, STUDY_B.key]);
  });

  it("reorderSubtable moves a column without touching other sublevels", () => {
    const { result } = renderSub({
      subtableColumnOrder: { series: [SERIES_COL.key] },
    });
    act(() =>
      result.current.reorderSubtable(
        "study",
        STUDY_B.key,
        STUDY_A.key,
        "before",
      ),
    );
    expect(studyKeys(result)).toEqual([STUDY_B.key, STUDY_A.key, LABEL_KEY]);
    expect(result.current.subtableOrder.series).toEqual([SERIES_COL.key]);
  });

  it("ignores stale keys in a saved order and appends unlisted columns last", () => {
    const { result } = renderSub({
      subtableColumnOrder: {
        study: ["builtin:study:retired_col", STUDY_B.key, STUDY_A.key],
      },
    });
    // Stale key matches nothing; the label is unlisted so it keeps its
    // default position at the end.
    expect(studyKeys(result)).toEqual([STUDY_B.key, STUDY_A.key, LABEL_KEY]);
  });

  it("never renders a hidden column, whatever the saved order says", () => {
    const { result } = renderSub({
      visibleKeys: ALL_SUB_KEYS.filter((k) => k !== STUDY_B.key),
      subtableColumnOrder: { study: [STUDY_B.key, STUDY_A.key] },
    });
    expect(studyKeys(result)).toEqual([STUDY_A.key, LABEL_KEY]);
  });

  it("resetColumns restores the default subtable order", () => {
    const { result } = renderSub({
      subtableColumnOrder: { study: [STUDY_B.key, STUDY_A.key] },
    });
    act(() => result.current.resetColumns());
    expect(result.current.subtableOrder).toEqual({});
    expect(studyKeys(result)).toEqual([STUDY_A.key, STUDY_B.key]);
  });

  it("sanitizes malformed saved subtable order", () => {
    const arrayInput = renderSub({
      subtableColumnOrder: [STUDY_B.key],
    });
    expect(arrayInput.result.current.subtableOrder).toEqual({});

    const junkLevels = renderSub({
      subtableColumnOrder: {
        bogus: [STUDY_A.key],
        study: [STUDY_B.key, STUDY_B.key, STUDY_A.key],
      },
    });
    expect(junkLevels.result.current.subtableOrder).toEqual({
      study: [STUDY_B.key, STUDY_A.key],
    });
  });
});
