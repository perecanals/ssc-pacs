import {
  cleanup,
  fireEvent,
  render,
  screen,
  within,
} from "@testing-library/react";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import Codebook from "../modules/data-exports/Codebook";

const originalShow = Object.getOwnPropertyDescriptor(
  HTMLDialogElement.prototype,
  "showModal",
);
const originalClose = Object.getOwnPropertyDescriptor(
  HTMLDialogElement.prototype,
  "close",
);
beforeEach(() => {
  Object.defineProperty(HTMLDialogElement.prototype, "showModal", {
    configurable: true,
    value: vi.fn(function () {
      this.setAttribute("open", "");
    }),
  });
  Object.defineProperty(HTMLDialogElement.prototype, "close", {
    configurable: true,
    value: vi.fn(function () {
      this.removeAttribute("open");
    }),
  });
});
afterEach(() => {
  cleanup();
  for (const [name, descriptor] of [
    ["showModal", originalShow],
    ["close", originalClose],
  ]) {
    if (descriptor)
      Object.defineProperty(HTMLDialogElement.prototype, name, descriptor);
    else delete HTMLDialogElement.prototype[name];
  }
});

const tables = [
  {
    name: "patient_labelled",
    columns: [
      { name: "patient_id", type: "text", description: "Metadata field" },
      {
        name: "label_severity",
        label_name: "severity",
        label_level: "patient",
        label_datatype: "int",
        instrument: "Intake",
        label_description: "Clinical severity.\nMeasured on admission.",
        description: "SQL comment",
      },
      {
        name: "label_notes",
        label_name: "notes",
        label_level: "patient",
        label_datatype: "text",
        label_description: null,
        description: "SQL note comment",
      },
    ],
  },
  {
    name: "image_series_labelled",
    columns: [
      {
        name: "label_series_type",
        label_name: "series_type",
        label_level: "series",
        label_datatype: "select",
        instrument: "Imaging",
        label_description: "Type of acquisition, such as CTA.",
      },
    ],
  },
];

it("opens a searchable label codebook using annotation descriptions", () => {
  render(<Codebook tables={tables} initialTable="patient_labelled" />);
  expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
  fireEvent.click(screen.getByRole("button", { name: "Codebook" }));
  const dialog = screen.getByRole("dialog", { name: "Label codebook" });
  expect(screen.getByLabelText("Search codebook")).toHaveFocus();
  expect(within(dialog).getByText(/Measured on admission/)).toBeInTheDocument();
  expect(within(dialog).queryByText("SQL comment")).not.toBeInTheDocument();
  expect(
    within(dialog).queryByText("SQL note comment"),
  ).not.toBeInTheDocument();
  expect(within(dialog).queryByText("Metadata field")).not.toBeInTheDocument();
  expect(
    within(dialog).getByText("No description provided."),
  ).toBeInTheDocument();
  fireEvent.change(screen.getByLabelText("Codebook table"), {
    target: { value: "" },
  });
  fireEvent.change(screen.getByLabelText("Search codebook"), {
    target: { value: "CTA" },
  });
  expect(within(dialog).getByText("series_type")).toBeInTheDocument();
  expect(within(dialog).queryByText("severity")).not.toBeInTheDocument();
  fireEvent.change(screen.getByLabelText("Codebook instrument"), {
    target: { value: "instrument:Intake" },
  });
  expect(
    within(dialog).getByText("No labels match these filters."),
  ).toBeInTheDocument();
  fireEvent.click(screen.getByRole("button", { name: "Close codebook" }));
  expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
  expect(document.body.style.overflow).toBe("");
});

it("closes on Escape and renders descriptions as plain text", () => {
  render(
    <Codebook
      tables={[
        {
          name: "patient_labelled",
          columns: [
            {
              name: "label_x",
              label_name: "x",
              label_description: "<script>unsafe()</script>",
            },
          ],
        },
      ]}
      initialTable="patient_labelled"
    />,
  );
  fireEvent.click(screen.getByText("Codebook"));
  const dialog = screen.getByRole("dialog");
  expect(
    within(dialog).getByText("<script>unsafe()</script>"),
  ).toBeInTheDocument();
  expect(dialog.querySelector("script")).toBeNull();
  fireEvent(dialog, new Event("cancel", { bubbles: true, cancelable: true }));
  expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
});
