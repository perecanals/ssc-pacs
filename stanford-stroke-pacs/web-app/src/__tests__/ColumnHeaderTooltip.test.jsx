import { describe, it, expect } from "vitest";
import { render, screen } from "@testing-library/react";

import TableHeader from "../components/DataTable/TableHeader";

const noop = () => {};

const LABEL_COL = {
  key: "label:nihss_total",
  label: "NIHSS Total",
  description: "Total NIH Stroke Scale score at admission.",
  instrument: "NIHSS",
  datatype: "select",
  level: "series",
  options: [],
};

function renderHeader(cols) {
  return render(
    <table>
      <TableHeader
        config={{ expandable: false, filterParamMap: {} }}
        level="series"
        mainTableCols={cols}
        showActions={false}
        frozenFirstCol={false}
        setFrozenFirstCol={noop}
        sortBy="seriesinstanceuid"
        sortDir="asc"
        columnFilters={{}}
        dragColKeyRef={{ current: null }}
        onSort={noop}
        onColumnFilter={noop}
        onBoolFilter={noop}
        onSelectFilterToggle={noop}
        onSelectFilterClear={noop}
        onDragStart={noop}
        onDragOver={noop}
        onDragLeave={noop}
        onDrop={noop}
        onDragEnd={noop}
      />
    </table>,
  );
}

describe("Column header description tooltip", () => {
  it("renders a styled tooltip with description, instrument and data type", () => {
    renderHeader([LABEL_COL]);
    const tip = screen.getByRole("tooltip");
    expect(tip).toHaveTextContent("Total NIH Stroke Scale score at admission.");
    expect(tip).toHaveTextContent("NIHSS");
    expect(tip).toHaveTextContent("select");
    // Level is intentionally excluded from the tooltip (it's intuitive
    // and already shown via the dt__level-hint badge).
    expect(tip).not.toHaveTextContent(/\bseries\b/);
  });

  it("uses the custom tooltip element, not the slow native title attribute", () => {
    renderHeader([LABEL_COL]);
    const th = screen.getByText("NIHSS Total").closest("th");
    expect(th).not.toHaveAttribute("title");
  });

  it("renders no tooltip for builtin columns (no description metadata)", () => {
    renderHeader([
      { key: "seriesinstanceuid", label: "Series UID", builtin: true, sourceKey: "seriesinstanceuid" },
    ]);
    expect(screen.queryByRole("tooltip")).not.toBeInTheDocument();
  });
});
