import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, fireEvent } from "@testing-library/react";
import PropTypes from "prop-types";

import ChildRows from "../ChildRows";
import useDragColumns from "../useDragColumns";

const noop = () => {};

const CHILD_COLS = [
  {
    key: "builtin:study:studydate",
    sourceKey: "studydate",
    label: "Study date",
    level: "study",
    builtin: true,
  },
  {
    key: "builtin:study:studydescription",
    sourceKey: "studydescription",
    label: "Study description",
    level: "study",
    builtin: true,
  },
];

const GC_COLS = [
  {
    key: "builtin:series:modality",
    sourceKey: "modality",
    label: "Modality",
    level: "series",
    builtin: true,
  },
  {
    key: "builtin:series:seriesdescription",
    sourceKey: "seriesdescription",
    label: "Series description",
    level: "series",
    builtin: true,
  },
];

// One expanded child (study st1) with its grandchild (series) table open, so
// both subtable headers render at once.
function Harness({ childReorder, grandReorder }) {
  const childDrag = useDragColumns(childReorder);
  const grandChildDrag = useDragColumns(grandReorder);
  return (
    <table>
      <tbody>
        <ChildRows
          parentRowId="p1"
          childRows={{
            p1: [{ studyinstanceuid: "st1", studydate: "20240101" }],
          }}
          childConfig={{ idCol: "studyinstanceuid", expandable: true }}
          childCols={CHILD_COLS}
          childIsExpandable={true}
          childDrag={childDrag}
          grandChildDrag={grandChildDrag}
          parentColSpan={4}
          grandExpanded={{ "p1::st1": true }}
          grandChildRows={{
            "p1::st1": [{ seriesinstanceuid: "se1", modality: "CT" }],
          }}
          grandChildCols={GC_COLS}
          grandChildConfig={{ idCol: "seriesinstanceuid" }}
          gcColSpan={4}
          onChildRowClick={noop}
          onGrandChildRowClick={noop}
          onResolveOhifLink={noop}
          onDicomDownload={noop}
          onMutated={noop}
        />
      </tbody>
    </table>
  );
}

Harness.propTypes = {
  childReorder: PropTypes.func.isRequired,
  grandReorder: PropTypes.func.isRequired,
};

const dataTransfer = () => ({
  setData: vi.fn(),
  effectAllowed: "",
  dropEffect: "",
});

const thOf = (label) => screen.getByText(label).closest("th");

describe("Subtable column drag-and-drop", () => {
  let childReorder;
  let grandReorder;

  beforeEach(() => {
    childReorder = vi.fn();
    grandReorder = vi.fn();
    render(<Harness childReorder={childReorder} grandReorder={grandReorder} />);
  });

  it("makes column headers draggable, but not Actions or spacer cells", () => {
    expect(thOf("Study date")).toHaveAttribute("draggable", "true");
    expect(thOf("Modality")).toHaveAttribute("draggable", "true");
    for (const th of screen.getAllByText("Actions")) {
      expect(th.closest("th")).not.toHaveAttribute("draggable", "true");
    }
  });

  it("drag within the child table shows a drop indicator and reorders", () => {
    const dt = dataTransfer();
    const from = thOf("Study date");
    const to = thOf("Study description");

    fireEvent.dragStart(from, { dataTransfer: dt });
    fireEvent.dragOver(to, { dataTransfer: dt });
    // jsdom rects are zero-sized, so the pointer always lands "after".
    expect(to.className).toContain("dt__child-th--drop-after");

    fireEvent.drop(to, { dataTransfer: dt });
    expect(childReorder).toHaveBeenCalledWith(
      "builtin:study:studydate",
      "builtin:study:studydescription",
      "after",
    );
    expect(grandReorder).not.toHaveBeenCalled();
  });

  it("drag within the grandchild table reorders via its own instance", () => {
    const dt = dataTransfer();
    fireEvent.dragStart(thOf("Modality"), { dataTransfer: dt });
    fireEvent.drop(thOf("Series description"), { dataTransfer: dt });
    expect(grandReorder).toHaveBeenCalledWith(
      "builtin:series:modality",
      "builtin:series:seriesdescription",
      "after",
    );
    expect(childReorder).not.toHaveBeenCalled();
  });

  it("a drag from the child table dropped on the grandchild table is inert", () => {
    const dt = dataTransfer();
    const foreign = thOf("Modality");

    fireEvent.dragStart(thOf("Study date"), { dataTransfer: dt });
    fireEvent.dragOver(foreign, { dataTransfer: dt });
    // The grandchild instance never saw a dragStart, so no indicator...
    expect(foreign.className).not.toContain("--drop-");

    fireEvent.drop(foreign, { dataTransfer: dt });
    // ...and neither reorder fires.
    expect(childReorder).not.toHaveBeenCalled();
    expect(grandReorder).not.toHaveBeenCalled();
  });
});
