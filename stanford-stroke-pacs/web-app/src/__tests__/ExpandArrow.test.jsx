import { describe, it, expect, vi } from "vitest";
import { render, screen, fireEvent, waitFor } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";

// Expanding a patient row must toggle the CSS modifier (the rotation lives in
// DataTable.css per the BEM-classes-in-JSX rule, not as a raw utility class).
const PATIENT_ROW = {
  patient_id: "P-0001",
  stroke_date: "2024-01-01",
  annotations: [],
  inherited_annotations: [],
};

vi.mock("../api/client", () => ({
  apiFetch: vi.fn().mockResolvedValue({ ok: true, json: () => Promise.resolve({}) }),
  apiGet: vi.fn().mockImplementation((path) => {
    if (path === "/api/me") return Promise.resolve({ username: "tester", is_admin: false });
    if (path === "/api/label-definitions") return Promise.resolve([]);
    if (path === "/api/labels/summary") return Promise.resolve([]);
    if (path === "/api/study-import-labels") return Promise.resolve([]);
    if (path === "/api/datasets") return Promise.resolve([]);
    if (path.startsWith("/api/preferences/")) return Promise.resolve({ prefs: {} });
    if (path.includes("/studies")) return Promise.resolve([]);
    if (path.startsWith("/api/patients")) {
      return Promise.resolve({ total: 1, page: 1, per_page: 50, items: [PATIENT_ROW] });
    }
    return Promise.resolve({ total: 0, page: 1, per_page: 50, items: [] });
  }),
  apiPost: vi.fn().mockResolvedValue({ ok: true, json: () => Promise.resolve({}) }),
  apiDelete: vi.fn().mockResolvedValue({ ok: true }),
  markApiActivity: vi.fn(),
  getLastApiActivityAt: vi.fn(() => Date.now()),
}));

vi.mock("../api/warmOhif", () => ({
  getStorageMode: vi.fn().mockResolvedValue("legacy"),
  getBatchCacheStatus: vi.fn().mockResolvedValue({ studies: {}, patients: {}, series: {} }),
  queueWarmStudy: vi.fn().mockResolvedValue(),
  queueWarmSeries: vi.fn().mockResolvedValue(),
  queueWarmPatient: vi.fn().mockResolvedValue(),
  resolveOhifViewerUrl: vi.fn().mockResolvedValue(null),
}));

import { AuthProvider } from "../context/AuthContext";
import Navigator from "../pages/Navigator";

describe("Expand arrow modifier class", () => {
  it("toggles dt__expand-arrow--open when a patient row expands", async () => {
    render(
      <MemoryRouter>
        <AuthProvider>
          <Navigator />
        </AuthProvider>
      </MemoryRouter>,
    );
    const cell = await screen.findByText("P-0001");
    const arrow = document.querySelector(".dt__expand-arrow");
    expect(arrow.classList.contains("dt__expand-arrow--open")).toBe(false);

    fireEvent.click(cell.closest("tr"));
    await waitFor(() =>
      expect(arrow.classList.contains("dt__expand-arrow--open")).toBe(true),
    );

    fireEvent.click(cell.closest("tr"));
    await waitFor(() =>
      expect(arrow.classList.contains("dt__expand-arrow--open")).toBe(false),
    );
  });
});
