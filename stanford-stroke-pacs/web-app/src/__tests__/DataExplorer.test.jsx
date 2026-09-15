import {
  fireEvent,
  render,
  screen,
  waitFor,
  within,
} from "@testing-library/react";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { beforeEach, describe, expect, it, vi } from "vitest";
import DataExplorer from "../modules/data-explorer/DataExplorer";

const state = vi.hoisted(() => ({ admin: true, staff: false }));
vi.mock("../context/AuthContext", () => ({
  useAuth: () => ({
    isAdmin: state.admin,
    isStaff: state.staff,
    loading: false,
    currentUser: "admin",
    logout: vi.fn(),
  }),
}));
vi.mock("../api/client", () => ({ apiFetch: vi.fn() }));
import { apiFetch } from "../api/client";
const tables = [
  {
    name: "patient_labelled",
    description: "Patients with labels",
    columns: [
      { name: "patient_id", type: "text" },
      { name: "dataset", type: "text[]" },
    ],
  },
];
const preview = {
  columns: ["patient_id"],
  rows: [["00123"]],
  has_more: false,
  offset: 0,
  sql: "SELECT patient_id FROM patient_labelled",
};
function mount() {
  return render(
    <MemoryRouter initialEntries={["/admin/data-explorer"]}>
      <Routes>
        <Route path="/admin/data-explorer" element={<DataExplorer />} />
        <Route path="/" element={<p>Home page</p>} />
      </Routes>
    </MemoryRouter>,
  );
}
beforeEach(() => {
  state.admin = true;
  state.staff = false;
  apiFetch.mockReset();
  apiFetch.mockImplementation(async (path, options) => {
    let data = [];
    if (path.endsWith("/catalog")) data = { tables, relationships: [] };
    if (path.includes("/preview")) data = preview;
    if (path.endsWith("/exports") && options.method === "POST")
      data = { id: "job1", status: "queued" };
    if (path.endsWith("/reports") && options.method === "POST")
      data = { id: "report1" };
    return { ok: true, json: async () => data };
  });
});
describe("Data Exports", () => {
  it("redirects non-admins without loading research data", async () => {
    state.admin = false;
    mount();
    expect(await screen.findByText("Home page")).toBeInTheDocument();
    expect(apiFetch).not.toHaveBeenCalled();
  });
  it("previews selected columns and displays equivalent SQL", async () => {
    mount();
    await screen.findByText("Patients with labels");
    fireEvent.click(screen.getByText("Preview results"));
    expect(await screen.findByText("00123")).toBeInTheDocument();
    expect(screen.getByText(preview.sql)).toBeInTheDocument();
    const call = apiFetch.mock.calls.find(([path]) =>
      path.includes("/preview"),
    );
    expect(JSON.parse(call[1].body).builder.columns).toEqual([
      "patient_labelled.patient_id",
      "patient_labelled.dataset",
    ]);
  });
  it("submits advanced SQL exports and opens history", async () => {
    mount();
    await screen.findByText("Patients with labels");
    fireEvent.click(screen.getByText("SQL query"));
    fireEvent.change(screen.getByLabelText("SQL query"), {
      target: { value: "SELECT count(*) FROM patient_labelled" },
    });
    fireEvent.change(screen.getByLabelText("Export name"), {
      target: { value: "My Excel export" },
    });
    fireEvent.click(screen.getByText("Export Excel"));
    expect(await screen.findByText(/Export queued/)).toBeInTheDocument();
    const call = apiFetch.mock.calls.find(
      ([path, options]) =>
        path.endsWith("/exports") && options.method === "POST",
    );
    expect(JSON.parse(call[1].body)).toMatchObject({
      mode: "sql",
      sql: "SELECT count(*) FROM patient_labelled",
      format: "xlsx",
    });
  });
  it("saves reports and exposes validation failures", async () => {
    mount();
    await screen.findByText("Patients with labels");
    fireEvent.change(screen.getByLabelText("Export name"), {
      target: { value: "Cohort" },
    });
    fireEvent.click(screen.getByText("Save report"));
    expect(await screen.findByText("Report saved.")).toBeInTheDocument();
    apiFetch.mockResolvedValueOnce({
      ok: false,
      json: async () => ({ detail: "Preview timed out" }),
    });
    fireEvent.click(screen.getByText("Preview results"));
    await waitFor(() =>
      expect(screen.getByRole("alert")).toHaveTextContent("Preview timed out"),
    );
  });
  it("uses a selected existing array element in the preview condition", async () => {
    const original = apiFetch.getMockImplementation();
    apiFetch.mockImplementation((path, options) =>
      path.includes("/values?")
        ? Promise.resolve({
            ok: true,
            json: async () => ({ values: ["crisp2", "lvo"], has_more: false }),
          })
        : original(path, options),
    );
    mount();
    await screen.findByText("Patients with labels");
    fireEvent.click(screen.getByText("Add condition"));
    fireEvent.change(screen.getByLabelText("Filter column"), {
      target: { value: "patient_labelled.dataset" },
    });
    fireEvent.change(screen.getByLabelText("Filter operator"), {
      target: { value: "contains" },
    });
    fireEvent.click(screen.getByText("Choose existing value"));
    const choices = await screen.findByLabelText("Existing values");
    fireEvent.change(choices, { target: { value: "0" } });
    expect(screen.getByLabelText("Filter value")).toHaveValue("crisp2");
    expect(
      apiFetch.mock.calls.some(([path]) =>
        path.includes("column=patient_labelled.dataset&operator=contains"),
      ),
    ).toBe(true);
    fireEvent.click(screen.getByText("Preview results"));
    await screen.findByText("00123");
    const call = apiFetch.mock.calls.find(([path]) =>
      path.includes("/preview"),
    );
    expect(JSON.parse(call[1].body).builder.filters.rules).toEqual([
      { column: "patient_labelled.dataset", op: "contains", value: "crisp2" },
    ]);
  });
  it("preserves nested AND, OR and NOT groups in the preview configuration", async () => {
    mount();
    await screen.findByText("Patients with labels");
    fireEvent.click(screen.getByText("Add condition group"));
    const outer = within(
      screen.getByRole("group", { name: "Nested condition group" }),
    );
    fireEvent.change(outer.getByLabelText("Filter combination"), {
      target: { value: "or" },
    });
    fireEvent.click(outer.getByText("Add condition"));
    fireEvent.change(outer.getByLabelText("Filter value"), {
      target: { value: "A" },
    });
    fireEvent.click(outer.getByText("Add condition group"));
    const inner = within(
      outer.getByRole("group", { name: "Nested condition group" }),
    );
    fireEvent.change(inner.getByLabelText("Filter combination"), {
      target: { value: "and" },
    });
    fireEvent.click(inner.getByLabelText(/NOT — exclude/));
    fireEvent.click(inner.getByText("Add condition"));
    fireEvent.change(inner.getByLabelText("Filter value"), {
      target: { value: "B" },
    });
    fireEvent.click(screen.getByText("Preview results"));
    await screen.findByText("00123");
    const call = apiFetch.mock.calls.find(([path]) =>
      path.includes("/preview"),
    );
    expect(JSON.parse(call[1].body).builder.filters).toEqual({
      op: "and",
      rules: [
        {
          op: "or",
          rules: [
            { column: "patient_labelled.patient_id", op: "eq", value: "A" },
            {
              op: "and",
              negated: true,
              rules: [
                { column: "patient_labelled.patient_id", op: "eq", value: "B" },
              ],
            },
          ],
        },
      ],
    });
  });
  it("scopes existing choices and IN filters to the top-level dataset", async () => {
    const original = apiFetch.getMockImplementation();
    apiFetch.mockImplementation((path, options) => {
      if (path.endsWith("/catalog"))
        return Promise.resolve({
          ok: true,
          json: async () => ({
            datasets: ["crisp2", "lvo"],
            relationships: [],
            tables: [
              {
                name: "image_series_labelled",
                description: "Series labels",
                columns: [
                  { name: "seriesinstanceuid", type: "text" },
                  {
                    name: "label_series_type",
                    type: "text",
                    label_name: "series_type",
                    label_datatype: "select",
                  },
                ],
              },
            ],
          }),
        });
      if (path.includes("/values?")) {
        const params = new URLSearchParams(path.split("?")[1]);
        return Promise.resolve({
          ok: true,
          json: async () => ({
            values:
              params.get("search") === "NCCT" ? ["NCCT"] : ["CTA", "NCCT"],
            has_more: false,
          }),
        });
      }
      if (path.includes("/preview"))
        return Promise.resolve({
          ok: true,
          json: async () => ({
            ...preview,
            sql: "dataset-scoped SQL",
            base_sql: "SELECT label_series_type FROM image_series_labelled",
          }),
        });
      return original(path, options);
    });
    mount();
    await screen.findByText("Series labels");
    fireEvent.change(screen.getByLabelText("Dataset"), {
      target: { value: "crisp2" },
    });
    expect(screen.getByText("Viewing: crisp2")).toBeInTheDocument();
    fireEvent.click(screen.getByText("Add condition"));
    expect(
      within(screen.getByLabelText("Filter column"))
        .getAllByRole("option")
        .map((option) => option.textContent),
    ).toEqual([
      "image_series_labelled.label_series_type",
      "image_series_labelled.seriesinstanceuid",
    ]);
    fireEvent.change(screen.getByLabelText("Filter column"), {
      target: { value: "image_series_labelled.label_series_type" },
    });
    await screen.findByLabelText("Existing values");
    fireEvent.change(screen.getByLabelText("Filter operator"), {
      target: { value: "in" },
    });
    fireEvent.click(
      await screen.findByRole("checkbox", { name: "CTA", exact: true }),
    );
    fireEvent.change(screen.getByLabelText("Search existing values"), {
      target: { value: "NCCT" },
    });
    fireEvent.click(
      await screen.findByRole("checkbox", { name: "NCCT", exact: true }),
    );
    expect(
      screen.getByRole("button", { name: "Remove value CTA" }),
    ).toBeInTheDocument();
    const lookup = apiFetch.mock.calls
      .filter(([path]) => path.includes("/values?"))
      .at(-1);
    expect(new URLSearchParams(lookup[0].split("?")[1]).get("dataset")).toBe(
      "crisp2",
    );
    fireEvent.click(screen.getByText("Preview results"));
    await screen.findByText("00123");
    const call = apiFetch.mock.calls.find(([path]) =>
      path.includes("/preview"),
    );
    expect(JSON.parse(call[1].body)).toMatchObject({
      dataset: "crisp2",
      builder: { filters: { rules: [{ op: "in", value: ["CTA", "NCCT"] }] } },
    });
    fireEvent.click(screen.getByRole("button", { name: "SQL query" }));
    expect(screen.getByLabelText("SQL query")).toHaveValue(
      "SELECT label_series_type FROM image_series_labelled",
    );
    fireEvent.change(screen.getByLabelText("Dataset"), {
      target: { value: "lvo" },
    });
    expect(screen.queryByText("00123")).not.toBeInTheDocument();
    fireEvent.change(screen.getByLabelText("Export name"), {
      target: { value: "My CSV export" },
    });
    fireEvent.click(screen.getByText("Export CSV"));
    await screen.findByText(/Export queued/);
    const exported = apiFetch.mock.calls.find(
      ([path, options]) =>
        path.endsWith("/exports") && options.method === "POST",
    );
    expect(JSON.parse(exported[1].body)).toMatchObject({
      dataset: "lvo",
      mode: "sql",
      sql: "SELECT label_series_type FROM image_series_labelled",
    });
  });
});

describe("instrument selection", () => {
  it("selects instrument columns without losing selections in other groups", async () => {
    const columns = [
      { name: "patient_id", type: "text" },
      {
        name: "label_score",
        type: "integer",
        label_name: "score",
        instrument: "Intake",
      },
      {
        name: "label_onset",
        type: "text",
        label_name: "onset",
        instrument: "Intake",
      },
      {
        name: "label_outcome",
        type: "integer",
        label_name: "outcome",
        instrument: "Follow-up",
      },
      {
        name: "label_notes",
        type: "text",
        label_name: "notes",
        instrument: null,
      },
    ];
    const original = apiFetch.getMockImplementation();
    apiFetch.mockImplementation((path, options) =>
      path.endsWith("/catalog")
        ? Promise.resolve({
            ok: true,
            json: async () => ({
              tables: [{ ...tables[0], columns }],
              relationships: [],
            }),
          })
        : original(path, options),
    );
    const { container } = mount();
    await screen.findByText("Patients with labels");
    const selected = within(
      screen.getByRole("list", { name: "Selected column order" }),
    );
    expect(selected.getAllByRole("listitem")).toHaveLength(1);
    expect(
      selected.getByText("patient_labelled.patient_id"),
    ).toBeInTheDocument();
    fireEvent.click(screen.getByText("Deselect shown"));
    fireEvent.change(screen.getByLabelText("Column instrument"), {
      target: { value: "instrument:Intake" },
    });
    const list = within(container.querySelector(".explorer__columns"));
    expect(list.getAllByRole("checkbox")).toHaveLength(2);
    fireEvent.click(screen.getByText("Select shown (2)"));
    fireEvent.change(screen.getByLabelText("Column instrument"), {
      target: { value: "instrument:Follow-up" },
    });
    fireEvent.click(screen.getByText("Select shown (1)"));
    fireEvent.change(screen.getByLabelText("Column instrument"), {
      target: { value: "instrument:Intake" },
    });
    fireEvent.click(screen.getByText("Deselect shown"));
    fireEvent.change(screen.getByLabelText("Column instrument"), {
      target: { value: "unassigned" },
    });
    expect(list.getAllByRole("checkbox")).toHaveLength(1);
    fireEvent.click(screen.getByText("Preview results"));
    await screen.findByText("00123");
    const call = apiFetch.mock.calls.find(([path]) =>
      path.includes("/preview"),
    );
    expect(JSON.parse(call[1].body).builder.columns).toEqual([
      "patient_labelled.label_outcome",
    ]);
    fireEvent.click(screen.getByText("Patients with labels").closest("button"));
    expect(selected.getAllByRole("listitem")).toHaveLength(1);
    expect(
      selected.getByText("patient_labelled.patient_id"),
    ).toBeInTheDocument();
  });
});

it("exports columns in their drag-and-drop order", async () => {
  mount();
  await screen.findByText("Patients with labels");
  const list = within(
    screen.getByRole("list", { name: "Selected column order" }),
  );
  const [sourceRow, target] = list.getAllByRole("listitem");
  expect(sourceRow).toHaveAttribute("draggable", "true");
  const source = within(sourceRow).getByText("patient_labelled.patient_id");
  const dataTransfer = { setData: vi.fn(), effectAllowed: "", dropEffect: "" };
  fireEvent.dragStart(source, { dataTransfer });
  fireEvent.dragOver(target, { dataTransfer });
  fireEvent.drop(target, { dataTransfer });
  fireEvent.dragEnd(source, { dataTransfer });
  fireEvent.click(screen.getByText("Preview results"));
  await screen.findByText("00123");
  const call = apiFetch.mock.calls.find(([path]) => path.includes("/preview"));
  expect(JSON.parse(call[1].body).builder.columns).toEqual([
    "patient_labelled.dataset",
    "patient_labelled.patient_id",
  ]);
  fireEvent.click(
    list.getByRole("button", { name: "Remove patient_labelled.dataset" }),
  );
  expect(list.getAllByRole("listitem")).toHaveLength(1);
  expect(
    screen.getByRole("checkbox", { name: /patient_labelled.dataset/ }),
  ).not.toBeChecked();
  expect(screen.queryByText("00123")).not.toBeInTheDocument();
  fireEvent.change(screen.getByLabelText("Export name"), {
    target: { value: "Ordered columns" },
  });
  fireEvent.click(screen.getByText("Export CSV"));
  await screen.findByText(/Export queued/);
  const exportCall = apiFetch.mock.calls.find(
    ([path, options]) => path.endsWith("/exports") && options.method === "POST",
  );
  expect(JSON.parse(exportCall[1].body).builder.columns).toEqual([
    "patient_labelled.patient_id",
  ]);
});

it("allows staff to use Data Exports with private reports and permitted datasets", async () => {
  state.admin = false;
  state.staff = true;
  mount();
  expect(await screen.findByText("Patients with labels")).toBeInTheDocument();
  expect(
    screen.getByRole("heading", { name: "Data Exports" }),
  ).toBeInTheDocument();
  expect(screen.getByLabelText("My reports")).toBeInTheDocument();
  expect(
    screen.getByRole("option", { name: "All permitted datasets" }),
  ).toBeInTheDocument();
  fireEvent.click(screen.getByText("Preview results"));
  expect(await screen.findByText("00123")).toBeInTheDocument();
});

it("requires an export name but still allows unnamed previews", async () => {
  mount();
  await screen.findByText("Patients with labels");
  expect(screen.getByLabelText("Export name")).toBeRequired();
  expect(screen.getByText("Export CSV")).toBeDisabled();
  expect(screen.getByText("Export Excel")).toBeDisabled();
  expect(screen.getByText("Preview results")).toBeEnabled();
  fireEvent.change(screen.getByLabelText("Export name"), {
    target: { value: "   " },
  });
  expect(screen.getByText("Export CSV")).toBeDisabled();
  fireEvent.change(screen.getByLabelText("Export name"), {
    target: { value: "  My cohort  " },
  });
  fireEvent.click(screen.getByText("Export CSV"));
  await screen.findByText(/Export queued/);
  const call = apiFetch.mock.calls.find(
    ([path, options]) => path.endsWith("/exports") && options.method === "POST",
  );
  expect(JSON.parse(call[1].body).name).toBe("My cohort");
});

it("edits a past export with its name, dataset, columns and nested conditions restored", async () => {
  const configuration = {
    mode: "builder",
    dataset: "crisp2",
    builder: {
      table: "patient_labelled",
      joins: [],
      columns: ["patient_labelled.patient_id"],
      sort: [],
      filters: {
        op: "and",
        rules: [
          {
            op: "or",
            rules: [
              {
                column: "patient_labelled.patient_id",
                op: "in",
                value: ["P-0001"],
              },
            ],
          },
        ],
      },
    },
  };
  const original = apiFetch.getMockImplementation();
  apiFetch.mockImplementation((path, options) =>
    path.includes("/exports?")
      ? Promise.resolve({
          ok: true,
          json: async () => [
            {
              id: "past-export",
              name: "Baseline cohort",
              configuration,
              created_at: "2026-09-14T12:00:00Z",
              username: "admin",
              format: "csv",
              status: "completed",
              row_count: 1,
            },
          ],
        })
      : original(path, options),
  );
  mount();
  await screen.findByText("Patients with labels");
  fireEvent.click(
    screen.getByRole("button", { name: "Export history", exact: true }),
  );
  expect(await screen.findByText("Baseline cohort")).toBeInTheDocument();
  fireEvent.click(screen.getByText("Edit export"));
  expect(screen.getByLabelText("Export name")).toHaveValue("Baseline cohort");
  expect(screen.getByLabelText("Dataset")).toHaveValue("crisp2");
  expect(
    screen.getByText(/The original export stays available/),
  ).toBeInTheDocument();
  fireEvent.change(screen.getByLabelText("Export name"), {
    target: { value: "Updated cohort" },
  });
  fireEvent.click(screen.getByText("Export Excel"));
  await screen.findByText(/Export queued/);
  const call = apiFetch.mock.calls.find(
    ([path, options]) => path.endsWith("/exports") && options.method === "POST",
  );
  expect(JSON.parse(call[1].body)).toMatchObject({
    ...configuration,
    name: "Updated cohort",
    format: "xlsx",
    source_export_id: "past-export",
  });
});
