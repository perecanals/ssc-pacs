import { describe, it, expect } from "vitest";
import {
  formatDatetime,
  normalizeSelectFilterValues,
  hasFilterValue,
  getTextFilterValue,
  buildBuiltinColumnCatalog,
  buildPatientStudiesUrl,
  appendCascadeFilters,
  buildLabelFiltersFromValues,
  appendAutoValueParams,
  compareLabelDefsDefault,
  LEVEL_CONFIG,
  PER_PAGE,
} from "../table";

describe("formatDatetime", () => {
  it("returns empty string for falsy input", () => {
    expect(formatDatetime(null)).toBe("");
    expect(formatDatetime("")).toBe("");
    expect(formatDatetime(undefined)).toBe("");
  });

  it("returns the original string for invalid dates", () => {
    expect(formatDatetime("not-a-date")).toBe("not-a-date");
  });

  it("formats a valid ISO datetime", () => {
    const result = formatDatetime("2024-03-15T10:30:00Z");
    expect(result).toMatch(/2024-03-15/);
    expect(result).toMatch(/\d{2}:\d{2}/);
  });
});

describe("normalizeSelectFilterValues", () => {
  it("returns empty array for null/undefined", () => {
    expect(normalizeSelectFilterValues(null)).toEqual([]);
    expect(normalizeSelectFilterValues(undefined)).toEqual([]);
  });

  it("wraps a string in an array", () => {
    expect(normalizeSelectFilterValues("hello")).toEqual(["hello"]);
  });

  it("trims strings", () => {
    expect(normalizeSelectFilterValues("  hello  ")).toEqual(["hello"]);
  });

  it("returns empty array for blank string", () => {
    expect(normalizeSelectFilterValues("  ")).toEqual([]);
  });

  it("filters empty values from arrays", () => {
    expect(normalizeSelectFilterValues(["a", "", "  ", "b"])).toEqual([
      "a",
      "b",
    ]);
  });
});

describe("hasFilterValue", () => {
  it("returns false for null/undefined/empty", () => {
    expect(hasFilterValue(null)).toBe(false);
    expect(hasFilterValue(undefined)).toBe(false);
    expect(hasFilterValue("")).toBe(false);
  });

  it("returns true for non-empty string", () => {
    expect(hasFilterValue("x")).toBe(true);
  });

  it("returns true for array with values", () => {
    expect(hasFilterValue(["a"])).toBe(true);
  });

  it("returns false for array of empty strings", () => {
    expect(hasFilterValue(["", "  "])).toBe(false);
  });
});

describe("getTextFilterValue", () => {
  it("returns the string as-is", () => {
    expect(getTextFilterValue("hello")).toBe("hello");
  });

  it("returns empty string for non-string", () => {
    expect(getTextFilterValue(null)).toBe("");
    expect(getTextFilterValue(42)).toBe("");
  });
});

describe("buildBuiltinColumnCatalog", () => {
  it("returns columns for all levels", () => {
    const cols = buildBuiltinColumnCatalog("patient");
    const levels = [...new Set(cols.map((c) => c.level))];
    expect(levels).toContain("patient");
    expect(levels).toContain("study");
    expect(levels).toContain("series");
  });

  it("marks patient-level columns as visible when activeLevel is patient", () => {
    const cols = buildBuiltinColumnCatalog("patient");
    // Columns flagged defaultVisible:false (the opt-in "study import labels"
    // column) stay hidden even at their own level; all others default to
    // visible — including "dataset", which replaced import labels as the
    // default cohort column.
    const patientCols = cols.filter(
      (c) => c.level === "patient" && c.sourceKey !== "study_import_labels",
    );
    expect(patientCols.every((c) => c.defaultVisible === true)).toBe(true);
    const importLabels = cols.find(
      (c) => c.level === "patient" && c.sourceKey === "study_import_labels",
    );
    expect(importLabels.defaultVisible).toBe(false);
  });

  it("hides import_label by default at study and series levels", () => {
    const cols = buildBuiltinColumnCatalog("series");
    const importLabelCols = cols.filter((c) => c.sourceKey === "import_label");
    expect(importLabelCols.length).toBeGreaterThan(0);
    expect(importLabelCols.every((c) => c.defaultVisible === false)).toBe(true);
  });

  it("shows dataset by default at every level", () => {
    for (const level of ["patient", "study", "series"]) {
      const cols = buildBuiltinColumnCatalog(level);
      const dataset = cols.find(
        (c) => c.level === level && c.sourceKey === "dataset",
      );
      expect(dataset).toBeTruthy();
      expect(dataset.defaultVisible).toBe(true);
    }
  });

  it("keys are prefixed with builtin:", () => {
    const cols = buildBuiltinColumnCatalog("study");
    expect(cols.every((c) => c.key.startsWith("builtin:"))).toBe(true);
  });
});

describe("buildPatientStudiesUrl", () => {
  it("returns base URL without filters", () => {
    expect(buildPatientStudiesUrl({ patient_id: "P1" }, {})).toBe(
      "/api/patients/P1/studies",
    );
    expect(buildPatientStudiesUrl({ patient_id: "P1" })).toBe(
      "/api/patients/P1/studies",
    );
  });

  it("appends study import label", () => {
    expect(
      buildPatientStudiesUrl(
        { patient_id: "P1" },
        { studyImportLabel: "batch1" },
      ),
    ).toBe("/api/patients/P1/studies?study_import_label=batch1");
  });

  it("ignores blank label", () => {
    expect(
      buildPatientStudiesUrl({ patient_id: "P1" }, { studyImportLabel: "  " }),
    ).toBe("/api/patients/P1/studies");
  });

  it("appends cascade filters alongside the import label", () => {
    const url = buildPatientStudiesUrl(
      { patient_id: "P1" },
      { studyImportLabel: "batch1", autoValues: { series_type: ["CTA"] } },
    );
    expect(url).toBe(
      "/api/patients/P1/studies?study_import_label=batch1&series_type=CTA",
    );
  });
});

describe("appendAutoValueParams", () => {
  it("appends repeated params the API ORs", () => {
    const params = new URLSearchParams();
    appendAutoValueParams(params, {
      series_type: ["NCCT", "CTA"],
      timepoint: ["BL"],
    });
    expect(params.getAll("series_type")).toEqual(["NCCT", "CTA"]);
    expect(params.getAll("timepoint")).toEqual(["BL"]);
  });

  it("is a no-op for empty/invalid input", () => {
    const params = new URLSearchParams();
    appendAutoValueParams(params, null);
    appendAutoValueParams(params, {});
    expect(params.toString()).toBe("");
  });
});

describe("buildLabelFiltersFromValues", () => {
  it("serializes labelValues keyed by <level>:<label>", () => {
    const out = buildLabelFiltersFromValues({ "series:foo": ["a", "b"] });
    expect(out).toEqual([
      { label: "foo", level: "series", values: ["a", "b"], datatype: "select" },
    ]);
  });

  it("unions into an existing select filter for the same label+level", () => {
    const existing = [
      { label: "foo", level: "series", values: ["a"], datatype: "select" },
    ];
    buildLabelFiltersFromValues({ "series:foo": ["a", "c"] }, existing);
    expect(existing[0].values).toEqual(["a", "c"]);
  });
});

describe("appendCascadeFilters", () => {
  it("returns the base URL unchanged when there are no cascade filters", () => {
    expect(appendCascadeFilters("/api/x", {})).toBe("/api/x");
  });

  it("appends autoValues and labelValues", () => {
    const url = appendCascadeFilters("/api/x", {
      autoValues: { series_type: ["CTA"] },
      labelValues: { "series:foo": ["a"] },
    });
    expect(url).toContain("series_type=CTA");
    expect(url).toContain(
      `label_filters=${encodeURIComponent(
        JSON.stringify([
          { label: "foo", level: "series", values: ["a"], datatype: "select" },
        ]),
      )}`,
    );
  });

  it("uses & when the base URL already has a query string", () => {
    const url = appendCascadeFilters("/api/x?study_import_label=b", {
      autoValues: { timepoint: ["BL"] },
    });
    expect(url).toBe("/api/x?study_import_label=b&timepoint=BL");
  });
});

describe("compareLabelDefsDefault", () => {
  const sortNames = (rows) =>
    [...rows].sort(compareLabelDefsDefault).map((r) => r.name || r.label);

  it("orders instruments alphabetically with unassigned (null) last", () => {
    const rows = [
      { name: "z", instrument: null, created_at: "2020-01-01" },
      { name: "a", instrument: "Beta", created_at: "2020-01-01" },
      { name: "b", instrument: "Alpha", created_at: "2020-01-01" },
    ];
    expect(sortNames(rows)).toEqual(["b", "a", "z"]);
  });

  it("orders by creation time (oldest first) within an instrument", () => {
    const rows = [
      { name: "new", instrument: "Alpha", created_at: "2024-06-01T00:00:00Z" },
      { name: "old", instrument: "Alpha", created_at: "2021-01-01T00:00:00Z" },
      { name: "mid", instrument: "Alpha", created_at: "2022-03-01T00:00:00Z" },
    ];
    expect(sortNames(rows)).toEqual(["old", "mid", "new"]);
  });

  it("falls back to name when timestamps are equal/missing and supports summary rows (.label)", () => {
    const rows = [
      { label: "beta", instrument: "Alpha" },
      { label: "alpha", instrument: "Alpha" },
    ];
    expect(sortNames(rows)).toEqual(["alpha", "beta"]);
  });
});

describe("constants", () => {
  it("LEVEL_CONFIG has all three levels", () => {
    expect(Object.keys(LEVEL_CONFIG)).toEqual(["patient", "study", "series"]);
  });

  it("PER_PAGE is 50", () => {
    expect(PER_PAGE).toBe(50);
  });
});

describe("machine-derived Auto columns", () => {
  const keys = (level) => buildBuiltinColumnCatalog(level).map((c) => c.key);

  const col = (level, key) =>
    buildBuiltinColumnCatalog(level).find((c) => c.key === key);

  it("exposes Auto Series Type and Auto Timepoint at the series level", () => {
    expect(keys("series")).toContain("builtin:series:series_type");
    expect(keys("series")).toContain("builtin:series:timepoint");
  });

  // As sub-rows, series sit under a study row that already shows its own Auto
  // Timepoint — repeating it per child is redundant. On the flat series table
  // there is no parent row, so that is the one place it must default on.
  it("defaults series-level Auto Timepoint on only in the flat series table", () => {
    expect(col("series", "builtin:series:timepoint").defaultVisible).toBe(true);
    expect(col("study", "builtin:series:timepoint").defaultVisible).toBe(false);
    expect(col("patient", "builtin:series:timepoint").defaultVisible).toBe(
      false,
    );
  });

  it("still defaults Auto Series Type on at the series level", () => {
    expect(col("series", "builtin:series:series_type").defaultVisible).toBe(
      true,
    );
    expect(col("study", "builtin:study:timepoint").defaultVisible).toBe(true);
  });

  it("exposes Auto Timepoint, but not a series type, at the study level", () => {
    expect(keys("study")).toContain("builtin:study:timepoint");
    expect(keys("study")).not.toContain("builtin:study:series_type");
  });

  it("marks them read-only so BuiltinCell renders a pill, not editable text", () => {
    const col = buildBuiltinColumnCatalog("series").find(
      (c) => c.key === "builtin:series:series_type",
    );
    expect(col.readOnlyAuto).toBe(true);
    expect(col.introducedIn).toBe(1);
  });

  it("maps them to their API filter params", () => {
    expect(LEVEL_CONFIG.series.filterParamMap.series_type).toBe("series_type");
    expect(LEVEL_CONFIG.series.filterParamMap.timepoint).toBe("timepoint");
    expect(LEVEL_CONFIG.study.filterParamMap.timepoint).toBe("timepoint");
  });
});
