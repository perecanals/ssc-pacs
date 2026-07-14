import { describe, it, expect } from "vitest";
import { buildAutoTooltip, isEstimatedAnchor } from "../autoClassification";

describe("isEstimatedAnchor", () => {
  it("treats offset-derived anchors as estimates", () => {
    expect(isEstimatedAnchor("receiving_arrival_time")).toBe(true);
    expect(isEstimatedAnchor("time_recognized")).toBe(true);
  });

  it("treats a recorded puncture time as exact", () => {
    expect(isEstimatedAnchor("femoral_sheath_time")).toBe(false);
    expect(isEstimatedAnchor(null)).toBe(false);
  });
});

describe("buildAutoTooltip", () => {
  it("carries the rule and version for series_type", () => {
    const tip = buildAutoTooltip("series_type", {
      series_type: "NCCT_BONE",
      series_type_rule: "kernel-bone",
      series_type_version: "rules-v1",
    });
    expect(tip).toContain("kernel-bone");
    expect(tip).toContain("rules-v1");
    expect(tip).toContain("not a human label");
  });

  it("explains what rank 1 means", () => {
    const tip = buildAutoTooltip("series_type", {
      series_type: "NCCT",
      series_type_rank: 1,
      series_label: "NCCT_1",
    });
    expect(tip).toContain("NCCT_1");
    expect(tip).toContain("the NCCT to use for this patient");
  });

  it("places a lower-ranked series among its siblings", () => {
    const tip = buildAutoTooltip("series_type", {
      series_type: "CTA",
      series_type_rank: 3,
      series_label: "CTA_3",
    });
    expect(tip).toContain("rank 3");
    expect(tip).not.toContain("to use for this patient");
  });

  it("carries the anchor and offset for timepoint", () => {
    const tip = buildAutoTooltip("timepoint", {
      timepoint: "THROMBECTOMY",
      timepoint_anchor_source: "femoral_sheath_time",
      hours_to_event: 0.83,
    });
    expect(tip).toContain("femoral_sheath_time");
    expect(tip).toContain("+0.8 h");
    expect(tip).not.toContain("ESTIMATED");
  });

  it("flags an estimated anchor", () => {
    const tip = buildAutoTooltip("timepoint", {
      timepoint: "FU",
      timepoint_anchor_source: "time_recognized",
      hours_to_event: 26,
    });
    expect(tip).toContain("ESTIMATED");
  });

  // A zero offset is a real value — a falsy guard would drop it.
  it("renders a zero offset", () => {
    const tip = buildAutoTooltip("timepoint", {
      timepoint: "THROMBECTOMY",
      timepoint_anchor_source: "femoral_sheath_time",
      hours_to_event: 0,
    });
    expect(tip).toContain("+0 h");
  });

  it("renders a negative offset", () => {
    const tip = buildAutoTooltip("timepoint", {
      timepoint: "BL",
      hours_to_event: -3.5,
    });
    expect(tip).toContain("-3.5 h");
  });

  // NULL series_type is a decision, not a gap — most of the corpus is excluded
  // (bone, topogram, RAPID output) and the rule records which exclusion fired.
  it("explains an exclusion when series_type is null but a rule fired", () => {
    const tip = buildAutoTooltip("series_type", {
      series_type: null,
      series_type_rule: "description-derived",
    });
    expect(tip).toContain("Excluded");
    expect(tip).toContain("description-derived");
  });

  it("is undefined when truly unclassified or for an ordinary column", () => {
    expect(
      buildAutoTooltip("series_type", { series_type: null }),
    ).toBeUndefined();
    expect(buildAutoTooltip("timepoint", { timepoint: "" })).toBeUndefined();
    expect(buildAutoTooltip("modality", { modality: "CT" })).toBeUndefined();
  });
});
