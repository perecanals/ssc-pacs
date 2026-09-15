import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { beforeEach, expect, it, vi } from "vitest";
import ConditionValue from "../modules/data-exports/ConditionValue";
import { apiFetch } from "../api/client";

vi.mock("../api/client", () => ({ apiFetch: vi.fn() }));
beforeEach(() => apiFetch.mockReset());

it("automatically shows and searches existing choices for select labels", async () => {
  apiFetch.mockResolvedValue({
    ok: true,
    json: async () => ({ values: ["CTA", "CTA1"], has_more: false }),
  });
  const onChange = vi.fn();
  render(
    <ConditionValue
      column={{
        ref: "image_series_labelled.label_series_type",
        type: "text",
        label_datatype: "select",
      }}
      value=""
      onChange={onChange}
    />,
  );
  await screen.findByLabelText("Existing values");
  fireEvent.change(screen.getByLabelText("Search existing values"), {
    target: { value: "CTA" },
  });
  await screen.findByLabelText("Existing values");
  const params = new URLSearchParams(apiFetch.mock.lastCall[0].split("?")[1]);
  expect(params.get("column")).toBe("image_series_labelled.label_series_type");
  expect(params.get("search")).toBe("CTA");
  fireEvent.change(screen.getByLabelText("Existing values"), {
    target: { value: "0" },
  });
  expect(onChange).toHaveBeenCalledWith("CTA");
});

it.each(["html", "404"])(
  "explains an unavailable endpoint returning %s",
  async (kind) => {
    apiFetch.mockResolvedValue({
      ok: kind === "html",
      status: kind === "html" ? 200 : 404,
      json: async () => {
        if (kind === "html") throw new SyntaxError("Unexpected token '<'");
        return { detail: "Not Found" };
      },
    });
    render(
      <ConditionValue
        column={{
          ref: "image_series_labelled.label_series_type",
          type: "text",
          label_datatype: "select",
        }}
        value=""
        onChange={vi.fn()}
      />,
    );
    expect(await screen.findByRole("alert")).toHaveTextContent(
      "Existing values are unavailable",
    );
    expect(screen.queryByText(/No matching values/)).not.toBeInTheDocument();
  },
);

it("searches choices and preserves empty strings as selectable values", async () => {
  apiFetch.mockResolvedValue({
    ok: true,
    json: async () => ({ values: ["", "100%"], has_more: true }),
  });
  const onChange = vi.fn();
  render(
    <ConditionValue
      column={{ ref: "patient_labelled.label_value", type: "text" }}
      value="old"
      onChange={onChange}
    />,
  );
  expect(apiFetch).not.toHaveBeenCalled();
  fireEvent.click(screen.getByText("Choose existing value"));
  await screen.findByLabelText("Existing values");
  expect(screen.getByText(/Showing the first 100/)).toBeInTheDocument();
  fireEvent.change(screen.getByLabelText("Search existing values"), {
    target: { value: "100%" },
  });
  await screen.findByLabelText("Existing values");
  const params = new URLSearchParams(apiFetch.mock.lastCall[0].split("?")[1]);
  expect(params.get("search")).toBe("100%");
  fireEvent.change(screen.getByLabelText("Existing values"), {
    target: { value: "0" },
  });
  expect(onChange).toHaveBeenCalledWith("");
});

it("discards stale choices after changing columns and keeps manual entry on lookup errors", async () => {
  let resolveFirst;
  apiFetch.mockImplementationOnce(
    () =>
      new Promise((resolve) => {
        resolveFirst = resolve;
      }),
  );
  const onChange = vi.fn();
  const { rerender } = render(
    <ConditionValue
      column={{ ref: "patient_labelled.patient_id", type: "text" }}
      value=""
      onChange={onChange}
    />,
  );
  fireEvent.click(screen.getByText("Choose existing value"));
  await waitFor(() => expect(apiFetch).toHaveBeenCalledOnce());
  const signal = apiFetch.mock.lastCall[1].signal;
  rerender(
    <ConditionValue
      column={{ ref: "patient_labelled.dataset", type: "text[]" }}
      value=""
      onChange={onChange}
    />,
  );
  expect(signal.aborted).toBe(true);
  resolveFirst({
    ok: true,
    json: async () => ({ values: ["stale"], has_more: false }),
  });
  apiFetch.mockResolvedValue({
    ok: false,
    json: async () => ({ detail: "Value lookup timed out" }),
  });
  fireEvent.click(screen.getByText("Choose existing value"));
  expect(await screen.findByRole("alert")).toHaveTextContent(
    "Value lookup timed out",
  );
  expect(screen.queryByText("stale")).not.toBeInTheDocument();
  fireEvent.change(screen.getByLabelText("Filter value"), {
    target: { value: "manual" },
  });
  expect(onChange).toHaveBeenCalledWith("manual");
});
