import { describe, it, expect, vi } from "vitest";
import { render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";

// Landing now consumes useAuth() — mock the API client so AuthProvider can
// resolve the initial /api/me call without hitting a real backend.
vi.mock("../api/client", () => ({
  apiFetch: vi.fn().mockResolvedValue({ ok: true, json: () => Promise.resolve({}) }),
  apiGet: vi.fn().mockImplementation((path) => {
    if (path === "/api/me") {
      return Promise.resolve({ username: "testadmin", is_admin: true });
    }
    return Promise.resolve({});
  }),
  apiPost: vi.fn().mockResolvedValue({ ok: true, json: () => Promise.resolve({}) }),
  apiDelete: vi.fn().mockResolvedValue({ ok: true }),
}));

import { AuthProvider } from "../context/AuthContext";
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
    expect(
      screen.getByText("Stanford Stroke Center PACS"),
    ).toBeInTheDocument();
    await waitFor(() => {
      expect(screen.getByText("testadmin")).toBeInTheDocument();
    });
    expect(screen.getByRole("button", { name: /log out/i })).toBeInTheDocument();
  });

  it("renders all three navigation cards for an admin user", async () => {
    renderLanding();
    // "Companion" is a base card rendered before auth resolves, so waiting on
    // it does not gate on the async /api/me. Wait on an admin-only card.
    await waitFor(() => {
      expect(screen.getByText("Orthanc Explorer")).toBeInTheDocument();
    });
    expect(screen.getByText("OHIF Viewer")).toBeInTheDocument();
    expect(screen.getByText("Companion")).toBeInTheDocument();
  });
});
