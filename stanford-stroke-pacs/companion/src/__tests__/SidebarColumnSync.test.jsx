import { describe, it, expect, vi } from "vitest";
import { render, screen, fireEvent, waitFor, within } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";

const SERIES_ROW = {
  seriesinstanceuid: "1.2.3.4.5.6",
  studyinstanceuid: "1.2.3.4.5",
  patient_id: "P-0001",
  modality: "CT",
  seriesdescription: "AxialSyncTest",
  annotations: [],
  inherited_annotations: [],
};

const LABEL = { name: "SyncLabel", level: "series", datatype: "bool", description: "", options: [], instrument: null };

vi.mock("../api/client", () => ({
  apiFetch: vi.fn().mockResolvedValue({ ok: true, json: () => Promise.resolve({}) }),
  apiGet: vi.fn().mockImplementation((path) => {
    if (path === "/api/me") return Promise.resolve({ username: "tester", is_admin: false });
    if (path === "/api/storage-mode") return Promise.resolve({ storage_mode: "legacy" });
    if (path === "/api/label-definitions") return Promise.resolve([LABEL]);
    if (path === "/api/labels/summary") {
      return Promise.resolve([{ label: "SyncLabel", level: "series", instrument: null, count: 1 }]);
    }
    if (path === "/api/study-import-labels") return Promise.resolve([]);
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

describe("Sidebar label filter <-> ColumnSelector sync", () => {
  it("activating a sidebar label checks its column; it can be hidden while the filter stays active", async () => {
    const { container } = render(
      <MemoryRouter>
        <AuthProvider>
          <Navigator />
        </AuthProvider>
      </MemoryRouter>,
    );

    fireEvent.click(await screen.findByRole("button", { name: /^series$/i }));
    await screen.findByText("AxialSyncTest");

    const openDropdown = () =>
      fireEvent.click(screen.getByRole("button", { name: /displayed columns/i }));

    // Initially the label column is not enabled in the dropdown.
    openDropdown();
    expect(screen.getByRole("checkbox", { name: /SyncLabel/ })).not.toBeChecked();
    // Close the dropdown (outside click) before touching the sidebar.
    fireEvent.click(document.body);

    // Click the sidebar label to activate the quick filter.
    const labelItem = container.querySelector('li[aria-label="SyncLabel"]');
    expect(labelItem).toBeTruthy();
    fireEvent.click(labelItem);

    // The dropdown checkbox now reflects the enabled column.
    await waitFor(() => {
      openDropdown();
      expect(screen.getByRole("checkbox", { name: /SyncLabel/ })).toBeChecked();
    });

    // Hide the column from the dropdown.
    fireEvent.click(screen.getByRole("checkbox", { name: /SyncLabel/ }));
    await waitFor(() =>
      expect(screen.getByRole("checkbox", { name: /SyncLabel/ })).not.toBeChecked(),
    );

    // The sidebar quick filter is still active (column hidden, filter not cleared).
    const stillActive = container.querySelector('li[aria-label="SyncLabel"]');
    expect(stillActive.className).toMatch(/sidebar__label-item--active/);
  });
});
