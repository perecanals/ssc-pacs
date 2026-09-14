import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import ConditionValue from "../modules/data-explorer/ConditionValue";

describe("date and timestamp conditions", () => {
  it("offers a calendar with ISO date guidance", () => {
    const onChange = vi.fn();
    render(
      <ConditionValue
        column={{ type: "date" }}
        value="2026-09-14"
        onChange={onChange}
      />,
    );
    const input = screen.getByLabelText("Filter value");
    expect(input).toHaveAttribute("type", "date");
    expect(input).toHaveAccessibleDescription(/YYYY-MM-DD/);
    input.showPicker = vi.fn();
    fireEvent.click(screen.getByRole("button", { name: "Open calendar" }));
    expect(input.showPicker).toHaveBeenCalledOnce();
    fireEvent.change(input, { target: { value: "2026-10-01" } });
    expect(onChange).toHaveBeenCalledWith("2026-10-01");
  });
  it("keeps timestamp-without-timezone values and fractional seconds unchanged", () => {
    render(
      <ConditionValue
        column={{ type: "timestamp(6) without time zone" }}
        value="2026-09-14 14:30:05.123456"
        onChange={vi.fn()}
      />,
    );
    const input = screen.getByLabelText("Filter value");
    expect(input).toHaveAttribute("type", "datetime-local");
    expect(input).toHaveAttribute("step", "1");
    expect(input).toHaveAccessibleDescription(/stores no timezone/);
    expect(input.value).toMatch(/^2026-09-14T14:30:05(?:\.000)?$/);
    expect(screen.getByLabelText("Fractional seconds (optional)")).toHaveValue(
      "123456",
    );
  });
  it("shows offset-based saved timestamps in UTC without editing the report", () => {
    const onChange = vi.fn();
    render(
      <ConditionValue
        column={{ type: "timestamp with time zone" }}
        value="2026-09-14 14:30:05.123456-07:00"
        onChange={onChange}
      />,
    );
    const input = screen.getByLabelText("Filter value");
    expect(input.value).toMatch(/^2026-09-14T21:30:05(?:\.000)?$/);
    expect(screen.getByLabelText("Fractional seconds (optional)")).toHaveValue(
      "123456",
    );
    expect(input).toHaveAccessibleDescription(/UTC/);
    expect(onChange).not.toHaveBeenCalled();
  });
  it("preserves ordinary text values and does not offer an unrelated calendar", () => {
    render(
      <ConditionValue
        column={{ type: "text" }}
        value="example"
        onChange={vi.fn()}
      />,
    );
    expect(screen.getByLabelText("Filter value")).toHaveValue("example");
    expect(
      screen.queryByRole("button", { name: "Open calendar" }),
    ).not.toBeInTheDocument();
  });
});
