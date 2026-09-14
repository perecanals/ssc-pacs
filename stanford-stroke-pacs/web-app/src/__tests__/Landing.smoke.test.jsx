import { describe, it, expect, vi } from "vitest";
import { render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";

// Landing now consumes useAuth() — mock the API client so AuthProvider can
// resolve the initial /api/me call without hitting a real backend.
vi.mock("../api/client", () => ({
  apiFetch: vi
    .fn()
    .mockResolvedValue({ ok: true, json: () => Promise.resolve({}) }),
  apiGet: vi.fn().mockImplementation((path) => {
    if (path === "/api/me") {
      return Promise.resolve({ username: "testadmin", is_admin: true });
    }
    return Promise.resolve({});
  }),
  apiPost: vi
    .fn()
    .mockResolvedValue({ ok: true, json: () => Promise.resolve({}) }),
  apiDelete: vi.fn().mockResolvedValue({ ok: true }),
  markApiActivity: vi.fn(),
  getLastApiActivityAt: vi.fn(() => Date.now()),
}));

import { AuthProvider } from "../context/AuthContext";
import { apiGet } from "../api/client";
import Landing from "../pages/Landing";

function renderLanding() {
  return render(
    <MemoryRouter>
      <AuthProvider>
        <Landing />
      </AuthProvider>
    </MemoryRouter>,
  );
}

describe("Landing page", () => {
  it("renders the title and the identity strip after auth resolves", async () => {
    renderLanding();
    expect(screen.getByText("Stanford Stroke Center PACS")).toBeInTheDocument();
    await waitFor(() => {
      expect(screen.getByText("testadmin")).toBeInTheDocument();
    });
    expect(
      screen.getByRole("button", { name: /log out/i }),
    ).toBeInTheDocument();
  });

  it("renders all four navigation cards for an admin user", async () => {
    renderLanding();
    // "Navigator" is a base card rendered before auth resolves, so waiting on
    // it does not gate on the async /api/me. Wait on an admin-only card.
    await waitFor(() => {
      expect(screen.getByText("Orthanc Explorer")).toBeInTheDocument();
    });
    expect(screen.getByText("OHIF Viewer")).toBeInTheDocument();
    expect(screen.getByText("User Access")).toBeInTheDocument();
    expect(screen.getByText("Navigator")).toBeInTheDocument();
  });
});

it("shows staff Data Exports while keeping administration hidden", async () => {
  apiGet.mockImplementation((path) =>
    Promise.resolve(
      path === "/api/me"
        ? { username: "staff", is_admin: false, is_staff: true }
        : { enabled: true },
    ),
  );
  renderLanding();
  expect(await screen.findByText("Data Exports")).toBeInTheDocument();
  expect(screen.getByRole("link", { name: /Data Exports/ })).toHaveAttribute(
    "href",
    "/data-exports",
  );
  expect(screen.queryByText("User Access")).not.toBeInTheDocument();
  expect(screen.queryByText("Orthanc Explorer")).not.toBeInTheDocument();
});
