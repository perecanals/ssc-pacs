import { describe, it, expect, vi, beforeEach } from "vitest";
import {
  render,
  screen,
  waitFor,
  fireEvent,
  within,
} from "@testing-library/react";
import { MemoryRouter, Routes, Route } from "react-router-dom";

vi.mock("../api/client", () => ({
  apiFetch: vi
    .fn()
    .mockResolvedValue({ ok: true, json: () => Promise.resolve({}) }),
  apiGet: vi.fn(),
  apiPost: vi
    .fn()
    .mockResolvedValue({ ok: true, json: () => Promise.resolve({}) }),
  apiPut: vi.fn(),
  apiDelete: vi.fn().mockResolvedValue({ ok: true }),
  markApiActivity: vi.fn(),
  getLastApiActivityAt: vi.fn(() => Date.now()),
}));

import { apiDelete, apiGet, apiPut } from "../api/client";
import { AuthProvider } from "../context/AuthContext";
import AdminLabels from "../pages/AdminLabels";

const LABELS = [
  {
    id: 1,
    name: "open_label",
    level: "patient",
    datatype: "text",
    instrument: null,
    created_by: "alice",
    edit_policy: "everyone",
    edit_users: [],
  },
  {
    id: 2,
    name: "femoral_sheath_time",
    level: "patient",
    datatype: "text",
    instrument: "redcap_lvo_clinical",
    created_by: "bulk:perecanals",
    edit_policy: "nobody",
    edit_users: [],
  },
  {
    id: 3,
    name: "aspects_total",
    level: "patient",
    datatype: "int",
    instrument: "crisp2_blo_aspects",
    created_by: "alice",
    edit_policy: "everyone",
    edit_users: [],
  },
];
const USERS = [
  { username: "admin1", is_admin: true, allowed_datasets: [] },
  { username: "alice", is_admin: false, allowed_datasets: ["lvo"] },
];

function mockApi({ me, deletionPlan }) {
  apiGet.mockImplementation((path) => {
    if (path === "/api/me") return Promise.resolve(me);
    if (path === "/api/admin/label-definitions")
      return Promise.resolve(LABELS.map((l) => ({ ...l })));
    if (path === "/api/admin/users") return Promise.resolve(USERS);
    if (path.startsWith("/api/admin/instruments/deletion-plan"))
      return Promise.resolve({
        instrument: "crisp2_blo_aspects",
        labels: [{ name: "aspects_total", level: "patient", n_annotations: 7 }],
        n_labels: 1,
        n_annotations: 7,
      });
    if (path.endsWith("/deletion-plan"))
      return Promise.resolve(
        deletionPlan || {
          name: "open_label",
          level: "patient",
          n_annotations: 4,
          labelled_table: "patient_labelled",
          column: "open_label",
        },
      );
    return Promise.resolve({});
  });
}

const ADMIN = { username: "admin1", is_admin: true, allowed_datasets: [] };

function renderAdminLabels() {
  return render(
    <MemoryRouter initialEntries={["/admin/labels"]}>
      <AuthProvider>
        <Routes>
          <Route path="/admin/labels" element={<AdminLabels />} />
          <Route path="/" element={<div>home page</div>} />
        </Routes>
      </AuthProvider>
    </MemoryRouter>,
  );
}

describe("AdminLabels page", () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  it("redirects non-admin users to the landing page", async () => {
    mockApi({
      me: { username: "alice", is_admin: false, allowed_datasets: ["lvo"] },
    });
    renderAdminLabels();
    await waitFor(() => {
      expect(screen.getByText("home page")).toBeInTheDocument();
    });
  });

  it("lists labels with their owner and policy", async () => {
    mockApi({ me: ADMIN });
    renderAdminLabels();
    await waitFor(() => {
      expect(screen.getByText("femoral_sheath_time")).toBeInTheDocument();
    });
    // The bulk owner is shown: it is why only an admin can unlock this one.
    expect(screen.getByText("bulk:perecanals")).toBeInTheDocument();
    expect(
      screen.getByLabelText("Who can edit femoral_sheath_time"),
    ).toHaveValue("nobody");
  });

  it("groups labels by instrument, alphabetically, Unassigned last", async () => {
    mockApi({ me: ADMIN });
    renderAdminLabels();
    await waitFor(() => {
      expect(screen.getByText("crisp2_blo_aspects")).toBeInTheDocument();
    });
    const headers = screen
      .getAllByRole("columnheader")
      // Real instrument headers also carry the Delete-instrument button.
      .map((th) => th.textContent.trim().replace(/Delete instrument$/, ""))
      .filter(
        // "" is the unlabelled actions column (the per-row Delete button).
        (t) =>
          !["Label", "Level", "Owner", "Who can edit values", ""].includes(t),
      );
    expect(headers).toEqual([
      "crisp2_blo_aspects (1)",
      "redcap_lvo_clinical (1)",
      "Unassigned (1)",
    ]);
  });

  it("puts each label under its own instrument", async () => {
    mockApi({ me: ADMIN });
    renderAdminLabels();
    await waitFor(() => {
      expect(screen.getByText("open_label")).toBeInTheDocument();
    });
    // Each group is its own tbody, so the row must live inside the right one.
    const group = screen.getByText("redcap_lvo_clinical").closest("tbody");
    expect(group).toHaveTextContent("femoral_sheath_time");
    expect(group).not.toHaveTextContent("open_label");
  });

  it("locking a label PUTs the new policy", async () => {
    mockApi({ me: ADMIN });
    apiPut.mockResolvedValue({ ok: true, json: () => Promise.resolve({}) });
    renderAdminLabels();
    await waitFor(() => {
      expect(screen.getByText("open_label")).toBeInTheDocument();
    });

    fireEvent.change(screen.getByLabelText("Who can edit open_label"), {
      target: { value: "nobody" },
    });

    await waitFor(() => {
      expect(apiPut).toHaveBeenCalledWith(
        "/api/admin/label-definitions/1/permissions",
        { edit_policy: "nobody", edit_users: [] },
      );
    });
  });

  it("selecting 'users' seeds the list with the owner", async () => {
    mockApi({ me: ADMIN });
    apiPut.mockResolvedValue({ ok: true, json: () => Promise.resolve({}) });
    renderAdminLabels();
    await waitFor(() => {
      expect(screen.getByText("open_label")).toBeInTheDocument();
    });

    fireEvent.change(screen.getByLabelText("Who can edit open_label"), {
      target: { value: "users" },
    });

    await waitFor(() => {
      expect(apiPut).toHaveBeenCalledWith(
        "/api/admin/label-definitions/1/permissions",
        { edit_policy: "users", edit_users: ["alice"] },
      );
    });
  });

  it("reverts and surfaces the error when the save fails", async () => {
    mockApi({ me: ADMIN });
    apiPut.mockResolvedValue({
      ok: false,
      json: () => Promise.resolve({ detail: "Unknown user(s): ghost" }),
    });
    renderAdminLabels();
    await waitFor(() => {
      expect(screen.getByText("open_label")).toBeInTheDocument();
    });

    const select = screen.getByLabelText("Who can edit open_label");
    fireEvent.change(select, { target: { value: "nobody" } });

    await waitFor(() => {
      expect(screen.getByText("Unknown user(s): ghost")).toBeInTheDocument();
    });
    // Optimistic change rolled back.
    expect(screen.getByLabelText("Who can edit open_label")).toHaveValue(
      "everyone",
    );
  });

  it("deleting a label always confirms first, showing the plan", async () => {
    mockApi({ me: ADMIN });
    renderAdminLabels();
    await waitFor(() => {
      expect(screen.getByText("open_label")).toBeInTheDocument();
    });

    fireEvent.click(screen.getByLabelText("Delete label open_label"));
    // Nothing deleted yet — the dialog with the plan is showing.
    expect(apiDelete).not.toHaveBeenCalled();
    await waitFor(() => {
      expect(screen.getByText(/4 annotations/)).toBeInTheDocument();
    });

    fireEvent.click(screen.getByText("Delete label"));
    await waitFor(() => {
      expect(apiDelete).toHaveBeenCalledWith("/api/admin/label-definitions/1");
    });
    // Row removed from the table.
    await waitFor(() => {
      expect(screen.queryByText("open_label")).not.toBeInTheDocument();
    });
  });

  it("cancelling the delete dialog leaves the label untouched", async () => {
    mockApi({ me: ADMIN });
    renderAdminLabels();
    await waitFor(() => {
      expect(screen.getByText("open_label")).toBeInTheDocument();
    });

    fireEvent.click(screen.getByLabelText("Delete label open_label"));
    await waitFor(() => {
      expect(screen.getByText(/4 annotations/)).toBeInTheDocument();
    });
    fireEvent.click(screen.getByText("Cancel"));

    expect(apiDelete).not.toHaveBeenCalled();
    expect(screen.getByText("open_label")).toBeInTheDocument();
  });

  it("deleting an instrument confirms with its label roster, then removes the group", async () => {
    mockApi({ me: ADMIN });
    renderAdminLabels();
    await waitFor(() => {
      expect(screen.getByText("aspects_total")).toBeInTheDocument();
    });
    // The Unassigned group is not an instrument — no delete button for it.
    expect(
      screen.queryByLabelText("Delete instrument Unassigned"),
    ).not.toBeInTheDocument();

    fireEvent.click(
      screen.getByLabelText("Delete instrument crisp2_blo_aspects"),
    );
    expect(apiDelete).not.toHaveBeenCalled();
    await waitFor(() => {
      expect(screen.getByText(/all 1 label/)).toBeInTheDocument();
    });
    const dialog = screen
      .getByText(/all 1 label/)
      .closest(".admin-labels__confirm");
    expect(within(dialog).getAllByText(/7 annotations/).length).toBeGreaterThan(
      0,
    );

    fireEvent.click(within(dialog).getByText("Delete instrument"));
    await waitFor(() => {
      expect(apiDelete).toHaveBeenCalledWith(
        "/api/admin/instruments?name=crisp2_blo_aspects",
      );
    });
    await waitFor(() => {
      expect(screen.queryByText("aspects_total")).not.toBeInTheDocument();
    });
    // Other groups untouched.
    expect(screen.getByText("open_label")).toBeInTheDocument();
  });
});
