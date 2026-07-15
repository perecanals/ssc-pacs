import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, fireEvent, waitFor } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";

// One series row so the series-level table renders an actions cell with the
// admin-only Delete button. is_admin is flipped per test.
const SERIES_ROW = {
  seriesinstanceuid: "1.2.3.4.5.6",
  studyinstanceuid: "1.2.3.4.5",
  patient_id: "P-0001",
  modality: "CT",
  seriesdescription: "AxialDeleteTest",
  annotations: [],
  inherited_annotations: [],
};

let isAdminFlag = false;
const apiFetch = vi.fn();

vi.mock("../api/client", () => ({
  apiFetch: (...args) => apiFetch(...args),
  apiGet: vi.fn().mockImplementation((path) => {
    if (path === "/api/me") {
      return Promise.resolve({ username: "tester", is_admin: isAdminFlag });
    }
    if (path === "/api/storage-mode")
      return Promise.resolve({ storage_mode: "legacy" });
    if (path === "/api/label-definitions") return Promise.resolve([]);
    if (path === "/api/labels/summary") return Promise.resolve([]);
    if (path === "/api/study-import-labels") return Promise.resolve([]);
    if (path.startsWith("/api/series")) {
      return Promise.resolve({
        total: 1,
        page: 1,
        per_page: 50,
        series: [SERIES_ROW],
      });
    }
    return Promise.resolve({ total: 0, page: 1, per_page: 50, items: [] });
  }),
  apiPost: vi
    .fn()
    .mockResolvedValue({ ok: true, json: () => Promise.resolve({}) }),
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

async function renderSeriesLevel() {
  render(
    <MemoryRouter>
      <AuthProvider>
        <Navigator />
      </AuthProvider>
    </MemoryRouter>,
  );
  fireEvent.click(await screen.findByRole("button", { name: /^series$/i }));
  await screen.findByText("AxialDeleteTest");
}

describe("Delete button gating + confirmation modal", () => {
  beforeEach(() => {
    isAdminFlag = false;
    // Default: any apiFetch resolves ok (debounced pref-save hook calls .catch).
    apiFetch.mockReset();
    apiFetch.mockResolvedValue({ ok: true, json: () => Promise.resolve({}) });
  });

  it("hides the Delete button from non-admins", async () => {
    isAdminFlag = false;
    await renderSeriesLevel();
    expect(screen.queryByTitle("Delete this series")).not.toBeInTheDocument();
    expect(screen.getByText("OHIF")).toBeInTheDocument();
  });

  it("admin: opening the modal loads the plan and confirming calls DELETE", async () => {
    isAdminFlag = true;
    // deletion-plan GET, then the DELETE.
    apiFetch.mockImplementation((path, opts) => {
      if (path.includes("/deletion-plan")) {
        return Promise.resolve({
          ok: true,
          json: () =>
            Promise.resolve({
              level: "series",
              patient_id: "P-0001",
              n_series: 1,
              n_annotations_discarded: 3,
              residual_files: ["/cold/P-0001/1.2.3.4.5/x"],
            }),
        });
      }
      if (opts && opts.method === "DELETE") {
        return Promise.resolve({
          ok: true,
          json: () => Promise.resolve({ deleted: true }),
        });
      }
      return Promise.resolve({ ok: true, json: () => Promise.resolve({}) });
    });

    await renderSeriesLevel();
    fireEvent.click(screen.getByTitle("Delete this series"));

    // The modal fetches the deletion plan on open.
    await waitFor(() => {
      const asked = apiFetch.mock.calls.some(([path]) =>
        String(path).includes("/deletion-plan"),
      );
      expect(asked).toBe(true);
    });

    // Confirm button enables once the plan resolves; then click it.
    const confirmBtn = screen.getByRole("button", { name: /^delete series$/i });
    await waitFor(() => expect(confirmBtn).not.toBeDisabled());
    fireEvent.click(confirmBtn);

    await waitFor(() => {
      const called = apiFetch.mock.calls.some(
        ([path, opts]) =>
          String(path).includes("/api/admin/series/") &&
          opts &&
          opts.method === "DELETE",
      );
      expect(called).toBe(true);
    });
  });
});
