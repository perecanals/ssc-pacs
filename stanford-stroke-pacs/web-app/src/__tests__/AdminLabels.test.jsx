import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, waitFor, fireEvent } from "@testing-library/react";
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

import { apiGet, apiPut } from "../api/client";
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

function mockApi({ me }) {
  apiGet.mockImplementation((path) => {
    if (path === "/api/me") return Promise.resolve(me);
    if (path === "/api/admin/label-definitions")
      return Promise.resolve(LABELS.map((l) => ({ ...l })));
    if (path === "/api/admin/users") return Promise.resolve(USERS);
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
      .map((th) => th.textContent.trim())
      .filter(
        (t) => !["Label", "Level", "Owner", "Who can edit values"].includes(t),
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
});
