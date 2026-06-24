import { describe, it, expect, vi } from "vitest";
import { render, screen, fireEvent, waitFor } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";

const SERIES_ROW = {
  seriesinstanceuid: "1.2.3.4.5.6",
  studyinstanceuid: "1.2.3.4.5",
  patient_id: "P-0001",
  modality: "CT",
  seriesdescription: "AxialResetTest",
  annotations: [],
  inherited_annotations: [],
};

vi.mock("../api/client", () => ({
  apiFetch: vi.fn().mockResolvedValue({ ok: true, json: () => Promise.resolve({}) }),
  apiGet: vi.fn().mockImplementation((path) => {
    if (path === "/api/me") return Promise.resolve({ username: "tester", is_admin: false });
    if (path === "/api/storage-mode") return Promise.resolve({ storage_mode: "legacy" });
    if (path === "/api/label-definitions") return Promise.resolve([]);
    if (path === "/api/labels/summary") return Promise.resolve([]);
    if (path === "/api/study-import-labels") return Promise.resolve(["PRECISE"]);
    if (path === "/api/datasets") return Promise.resolve([]);
    if (path.startsWith("/api/preferences/")) return Promise.resolve({ prefs: {} });
    if (path.startsWith("/api/series")) {
      return Promise.resolve({ total: 1, page: 1, per_page: 50, series: [SERIES_ROW] });
    }
    return Promise.resolve({ total: 0, page: 1, per_page: 50, items: [] });
  }),
  apiPost: vi.fn().mockResolvedValue({ ok: true, json: () => Promise.resolve({}) }),
  apiPut: vi.fn().mockResolvedValue({ ok: true, json: () => Promise.resolve({}) }),
  apiDelete: vi.fn().mockResolvedValue({ ok: true }),
  markApiActivity: vi.fn(),
  getLastApiActivityAt: vi.fn(() => Date.now()),
}));

vi.mock("../api/warmOhif", () => ({
  getStorageMode: vi.fn().mockResolvedValue("legacy"),
  resolveOhifViewerUrl: vi.fn().mockResolvedValue(null),
}));

import { AuthProvider } from "../context/AuthContext";
import Navigator from "../pages/Navigator";

describe("Reset Filters button", () => {
  it("clears the sidebar quick filter (and is wired to clear column filters)", async () => {
    render(
      <MemoryRouter>
        <AuthProvider>
          <Navigator />
        </AuthProvider>
      </MemoryRouter>,
    );

    // Go to the series level so the sidebar import-label quick filter shows.
    fireEvent.click(await screen.findByRole("button", { name: /^series$/i }));
    await screen.findByText("AxialResetTest");

    // Set the sidebar import-label quick filter.
    const importLabel = document.getElementById("sidebar-study-import-label");
    fireEvent.change(importLabel, { target: { value: "PRECISE" } });
    expect(importLabel.value).toBe("PRECISE");

    // The single "Reset Filters" toolbar button clears it.
    fireEvent.click(screen.getByRole("button", { name: /reset filters/i }));
    await waitFor(() => expect(importLabel.value).toBe(""));
  });
});
