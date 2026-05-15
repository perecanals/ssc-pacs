import { describe, it, expect } from "vitest";
import {
  formatDatetime,
  normalizeSelectFilterValues,
  hasFilterValue,
  getTextFilterValue,
  buildBuiltinColumnCatalog,
  buildPatientStudiesUrl,
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
    expect(normalizeSelectFilterValues(["a", "", "  ", "b"])).toEqual(["a", "b"]);
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
    const patientCols = cols.filter((c) => c.level === "patient");
    expect(patientCols.every((c) => c.defaultVisible === true)).toBe(true);
  });

  it("keys are prefixed with builtin:", () => {
    const cols = buildBuiltinColumnCatalog("study");
    expect(cols.every((c) => c.key.startsWith("builtin:"))).toBe(true);
  });
});

describe("buildPatientStudiesUrl", () => {
  it("returns base URL without label", () => {
    expect(buildPatientStudiesUrl({ patient_id: "P1" })).toBe("/api/patients/P1/studies");
  });

  it("appends label parameter", () => {
    expect(buildPatientStudiesUrl({ patient_id: "P1" }, "batch1")).toBe(
      "/api/patients/P1/studies?study_import_label=batch1",
    );
  });

  it("ignores blank label", () => {
    expect(buildPatientStudiesUrl({ patient_id: "P1" }, "  ")).toBe("/api/patients/P1/studies");
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
