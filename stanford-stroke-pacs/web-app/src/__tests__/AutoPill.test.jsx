import { describe, it, expect } from "vitest";
import { render, screen } from "@testing-library/react";

import BuiltinCell, { AutoPill } from "../components/DataTable/BuiltinCell";

const SERIES_TYPE_COL = { sourceKey: "series_type", readOnlyAuto: true };
const TIMEPOINT_COL = { sourceKey: "timepoint", readOnlyAuto: true };
const MODALITY_COL = { sourceKey: "modality" };

describe("AutoPill", () => {
  it("renders nothing when the classifier had no verdict", () => {
    const { container } = render(<AutoPill value="" />);
    expect(container).toBeEmptyDOMElement();
  });

  // The whole feature rests on this: the machine value must not look editable
  // next to the human label pill it sits beside.
  it("is not the editable SelectPill", () => {
    render(<AutoPill value="CTA" />);
    const pill = screen.getByText("CTA");
    expect(pill).toHaveClass("auto-pill");
    expect(pill).not.toHaveClass("select-pill");
    expect(pill).not.toHaveClass("select-pill--clickable");
  });
});

describe("BuiltinCell", () => {
  it("renders an Auto column as a pill carrying its provenance", () => {
    render(
      <BuiltinCell
        col={SERIES_TYPE_COL}
        row={{
          series_type: "NCCT",
          series_type_rule: "kernel-soft",
          series_type_version: "rules-v1",
        }}
      />,
    );
    const pill = screen.getByText("NCCT");
    expect(pill).toHaveClass("auto-pill");
    expect(pill.getAttribute("title")).toContain("kernel-soft");
  });

  // The rank rides as a badge, not as part of the value, so the hash colour
  // stays keyed on the type: every NCCT looks like an NCCT.
  it("shows the preference rank as a badge beside the type", () => {
    const { container } = render(
      <BuiltinCell
        col={SERIES_TYPE_COL}
        row={{
          series_type: "NCCT",
          series_type_rank: 2,
          series_label: "NCCT_2",
        }}
      />,
    );
    expect(screen.getByText("NCCT")).toBeInTheDocument();
    expect(container.querySelector(".auto-pill__rank").textContent).toBe("2");
    expect(container.querySelector(".auto-pill__rank--primary")).toBeNull();
  });

  it("emphasises rank 1 — the series to open", () => {
    const { container } = render(
      <BuiltinCell
        col={SERIES_TYPE_COL}
        row={{
          series_type: "NCCT",
          series_type_rank: 1,
          series_label: "NCCT_1",
        }}
      />,
    );
    expect(container.querySelector(".auto-pill__rank--primary")).not.toBeNull();
    expect(screen.getByText("NCCT").getAttribute("title")).toContain(
      "the NCCT to use",
    );
  });

  it("omits the badge when the series is unranked", () => {
    const { container } = render(
      <BuiltinCell col={SERIES_TYPE_COL} row={{ series_type: "ADC" }} />,
    );
    expect(container.querySelector(".auto-pill__rank")).toBeNull();
  });

  // Most of the corpus is excluded rather than unclassified; the reason must be
  // reachable rather than rendered as an empty cell.
  it("marks a deliberately excluded series with its reason", () => {
    const { container } = render(
      <BuiltinCell
        col={SERIES_TYPE_COL}
        row={{ series_type: null, series_type_rule: "description-derived" }}
      />,
    );
    const cell = container.querySelector(".auto-pill__excluded");
    expect(cell).not.toBeNull();
    expect(cell.getAttribute("title")).toContain("description-derived");
    expect(container.querySelector(".auto-pill")).toBeNull();
  });

  it("leaves a genuinely unclassified series blank", () => {
    const { container } = render(
      <BuiltinCell col={SERIES_TYPE_COL} row={{ series_type: null }} />,
    );
    expect(container).toBeEmptyDOMElement();
  });

  it("marks a timepoint derived from an estimated anchor", () => {
    const { container } = render(
      <BuiltinCell
        col={TIMEPOINT_COL}
        row={{ timepoint: "FU", timepoint_anchor_source: "time_recognized" }}
      />,
    );
    expect(container.querySelector(".auto-pill--estimated")).not.toBeNull();
  });

  it("does not mark a timepoint anchored on a recorded puncture time", () => {
    const { container } = render(
      <BuiltinCell
        col={TIMEPOINT_COL}
        row={{
          timepoint: "BL",
          timepoint_anchor_source: "femoral_sheath_time",
        }}
      />,
    );
    expect(container.querySelector(".auto-pill")).not.toBeNull();
    expect(container.querySelector(".auto-pill--estimated")).toBeNull();
  });

  it("leaves ordinary builtin columns as plain text", () => {
    const { container } = render(
      <BuiltinCell col={MODALITY_COL} row={{ modality: "CT" }} />,
    );
    expect(container.textContent).toBe("CT");
    expect(container.querySelector(".auto-pill")).toBeNull();
  });
});
