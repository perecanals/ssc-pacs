import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, waitFor, fireEvent } from "@testing-library/react";
import { MemoryRouter, Routes, Route } from "react-router-dom";

vi.mock("../api/client", () => ({
  apiFetch: vi.fn().mockResolvedValue({ ok: true, json: () => Promise.resolve({}) }),
  apiGet: vi.fn(),
  apiPost: vi.fn().mockResolvedValue({ ok: true, json: () => Promise.resolve({}) }),
  apiPut: vi.fn(),
  apiDelete: vi.fn().mockResolvedValue({ ok: true }),
  markApiActivity: vi.fn(),
  getLastApiActivityAt: vi.fn(() => Date.now()),
}));

import { apiGet, apiPut } from "../api/client";
import { AuthProvider } from "../context/AuthContext";
import AdminUsers from "../pages/AdminUsers";

const USERS = [
  { username: "admin1", is_admin: true, allowed_datasets: [], created_at: null },
  { username: "alice", is_admin: false, allowed_datasets: ["lvo"], created_at: null },
];
const DATASETS = ["lvo", "precise"];

function mockApi({ me }) {
  apiGet.mockImplementation((path) => {
    if (path === "/api/me") return Promise.resolve(me);
    if (path === "/api/admin/users") return Promise.resolve(USERS);
    if (path === "/api/datasets") return Promise.resolve(DATASETS);
    return Promise.resolve({});
  });
}

function renderAdminUsers() {
  return render(
    <MemoryRouter initialEntries={["/admin"]}>
      <AuthProvider>
        <Routes>
          <Route path="/admin" element={<AdminUsers />} />
          <Route path="/" element={<div>home page</div>} />
        </Routes>
      </AuthProvider>
    </MemoryRouter>,
  );
}

describe("AdminUsers page", () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  it("redirects non-admin users to the landing page", async () => {
    mockApi({ me: { username: "alice", is_admin: false, allowed_datasets: ["lvo"] } });
    renderAdminUsers();
    await waitFor(() => {
      expect(screen.getByText("home page")).toBeInTheDocument();
    });
  });

  it("renders a users-by-datasets checkbox grid for admins", async () => {
    mockApi({ me: { username: "admin1", is_admin: true, allowed_datasets: [] } });
    renderAdminUsers();
    await waitFor(() => {
      expect(screen.getByText("alice")).toBeInTheDocument();
    });
    // Dataset column headers.
    expect(screen.getByText("lvo")).toBeInTheDocument();
    expect(screen.getByText("precise")).toBeInTheDocument();
    // Admin rows show the bypass note, not checkboxes.
    expect(screen.getByText("All datasets (admin)")).toBeInTheDocument();
    // alice has lvo granted, precise not.
    const lvoBox = screen.getByLabelText("Grant alice access to lvo");
    const preciseBox = screen.getByLabelText("Grant alice access to precise");
    expect(lvoBox.checked).toBe(true);
    expect(preciseBox.checked).toBe(false);
  });

  it("toggling a checkbox PUTs the new grant list", async () => {
    mockApi({ me: { username: "admin1", is_admin: true, allowed_datasets: [] } });
    apiPut.mockResolvedValue({ ok: true, json: () => Promise.resolve({}) });
    renderAdminUsers();
    await waitFor(() => {
      expect(screen.getByText("alice")).toBeInTheDocument();
    });
    fireEvent.click(screen.getByLabelText("Grant alice access to precise"));
    await waitFor(() => {
      expect(apiPut).toHaveBeenCalledWith(
        "/api/admin/users/alice/datasets",
        { datasets: ["lvo", "precise"] },
      );
    });
  });

  it("reverts the optimistic update when the PUT fails", async () => {
    mockApi({ me: { username: "admin1", is_admin: true, allowed_datasets: [] } });
    apiPut.mockResolvedValue({
      ok: false,
      json: () => Promise.resolve({ detail: "Unknown dataset(s): precise" }),
    });
    renderAdminUsers();
    await waitFor(() => {
      expect(screen.getByText("alice")).toBeInTheDocument();
    });
    fireEvent.click(screen.getByLabelText("Grant alice access to precise"));
    await waitFor(() => {
      expect(screen.getByText("Unknown dataset(s): precise")).toBeInTheDocument();
    });
    expect(screen.getByLabelText("Grant alice access to precise").checked).toBe(false);
  });
});
