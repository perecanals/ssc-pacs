import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";

// Mock the API client so Companion doesn't hit a real backend.
vi.mock("../api/client", () => ({
  apiFetch: vi.fn().mockResolvedValue({ ok: true, json: () => Promise.resolve({}) }),
  apiGet: vi.fn().mockImplementation((path) => {
    if (path === "/api/me") return Promise.resolve({ username: "testuser" });
    if (path === "/api/storage-mode") return Promise.resolve({ storage_mode: "legacy" });
    if (path === "/api/label-definitions") return Promise.resolve([]);
    if (path === "/api/labels/summary") return Promise.resolve([]);
    if (path === "/api/study-import-labels") return Promise.resolve([]);
    // Default: paginated listing
    return Promise.resolve({ total: 0, page: 1, per_page: 50, items: [] });
  }),
  apiPost: vi.fn().mockResolvedValue({ ok: true, json: () => Promise.resolve({}) }),
  apiDelete: vi.fn().mockResolvedValue({ ok: true }),
  markApiActivity: vi.fn(),
  getLastApiActivityAt: vi.fn(() => Date.now()),
}));

// Companion uses warmOhif which also calls fetch-based APIs
vi.mock("../api/warmOhif", () => ({
  getStorageMode: vi.fn().mockResolvedValue("legacy"),
  resolveOhifViewerUrl: vi.fn().mockResolvedValue(null),
}));

import { AuthProvider } from "../context/AuthContext";
import Companion from "../pages/Companion";

describe("Companion page", () => {
  it("renders without crashing and shows the level switcher", async () => {
    render(
      <MemoryRouter>
        <AuthProvider>
          <Companion />
        </AuthProvider>
      </MemoryRouter>,
    );

    // The TopBar renders level buttons (Patients / Studies / Series).
    await waitFor(() => {
      expect(screen.getByText("Patients")).toBeInTheDocument();
    });
  });
});
