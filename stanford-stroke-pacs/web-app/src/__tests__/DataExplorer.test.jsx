import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { beforeEach, describe, expect, it, vi } from "vitest";
import DataExplorer from "../modules/data-explorer/DataExplorer";

const state = vi.hoisted(() => ({ admin: true }));
vi.mock("../context/AuthContext", () => ({
  useAuth: () => ({
    isAdmin: state.admin,
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
describe("Data Explorer", () => {
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
    fireEvent.change(screen.getByLabelText("Report name"), {
      target: { value: "Cohort" },
    });
    fireEvent.click(screen.getByText("Save report"));
    expect(await screen.findByText("Shared report saved.")).toBeInTheDocument();
    apiFetch.mockResolvedValueOnce({
      ok: false,
      json: async () => ({ detail: "Preview timed out" }),
    });
    fireEvent.click(screen.getByText("Preview results"));
    await waitFor(() =>
      expect(screen.getByRole("alert")).toHaveTextContent("Preview timed out"),
    );
  });
});
