import { describe, it, expect } from "vitest";
import { hashStr, valueColor, NOTION_COLORS } from "../colors";

describe("hashStr", () => {
  it("returns a non-negative integer", () => {
    expect(hashStr("hello")).toBeGreaterThanOrEqual(0);
    expect(Number.isInteger(hashStr("hello"))).toBe(true);
  });

  it("is deterministic", () => {
    expect(hashStr("test")).toBe(hashStr("test"));
  });

  it("returns 0 for empty string", () => {
    expect(hashStr("")).toBe(0);
  });

  it("differs for different strings", () => {
    expect(hashStr("a")).not.toBe(hashStr("b"));
  });
});

describe("valueColor", () => {
  it("returns an object with bg and text keys", () => {
    const c = valueColor("hello");
    expect(c).toHaveProperty("bg");
    expect(c).toHaveProperty("text");
  });

  it("returns a color from NOTION_COLORS", () => {
    const c = valueColor("anything");
    expect(NOTION_COLORS).toContainEqual(c);
  });

  it("is deterministic", () => {
    expect(valueColor("x")).toEqual(valueColor("x"));
  });
});

describe("timepoint colours", () => {
  it.each([
    ["BL", "BL3", "#dcfce7"],
    ["BLO", "BLO_MR", "#ecfccb"],
    ["BLT", "BLT1", "#d1fae5"],
    ["THROMBECTOMY", "thrombectomy", "#dbeafe"],
    ["PRE", "PRE2", "#ffedd5"],
    ["FU", "Fu2", "#fee2e2"],
    ["FU", "24h_FU", "#fee2e2"],
  ])("colours %s and %s alike", (base, variant, bg) => {
    expect(valueColor(base, "timepoint").bg).toBe(bg);
    expect(valueColor(variant, "timepoint")).toEqual(
      valueColor(base, "timepoint"),
    );
  });

  it("preserves the categorical colours for other labels and unknown timepoints", () => {
    expect(valueColor("FU", "series_type")).toEqual(valueColor("FU"));
    expect(valueColor("PRELIMINARY", "timepoint")).toEqual(
      valueColor("PRELIMINARY"),
    );
  });
});
