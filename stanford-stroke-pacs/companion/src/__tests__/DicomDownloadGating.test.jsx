import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, fireEvent } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";

// One series row so the series-level table renders an actions cell with
// the OHIF + (admin-only) Download buttons. is_admin is flipped per test.
const SERIES_ROW = {
  seriesinstanceuid: "1.2.3.4.5.6",
  studyinstanceuid: "1.2.3.4.5",
  patient_id: "P-0001",
  modality: "CT",
  seriesdescription: "AxialGatingTest",
  annotations: [],
  inherited_annotations: [],
};

let isAdminFlag = false;

vi.mock("../api/client", () => ({
  apiFetch: vi.fn().mockResolvedValue({ ok: true, json: () => Promise.resolve({}) }),
  apiGet: vi.fn().mockImplementation((path) => {
    if (path === "/api/me") {
      return Promise.resolve({ username: "tester", is_admin: isAdminFlag });
    }
    if (path === "/api/storage-mode") return Promise.resolve({ storage_mode: "legacy" });
    if (path === "/api/label-definitions") return Promise.resolve([]);
    if (path === "/api/labels/summary") return Promise.resolve([]);
    if (path === "/api/study-import-labels") return Promise.resolve([]);
    if (path.startsWith("/api/series")) {
      return Promise.resolve({ total: 1, page: 1, per_page: 50, series: [SERIES_ROW] });
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
  // Gate on the level switcher (rendered after auth resolves), then
  // switch to the series level and wait for the seeded row.
  fireEvent.click(await screen.findByRole("button", { name: /^series$/i }));
  await screen.findByText("AxialGatingTest");
}

describe("DICOM download button gating", () => {
  beforeEach(() => {
    isAdminFlag = false;
  });

  it("shows the Download button to admins", async () => {
    isAdminFlag = true;
    await renderSeriesLevel();
    expect(screen.getByTitle("Download DICOM as zip")).toBeInTheDocument();
  });

  it("hides the Download button from non-admins (OHIF still shown)", async () => {
    isAdminFlag = false;
    await renderSeriesLevel();
    expect(screen.queryByTitle("Download DICOM as zip")).not.toBeInTheDocument();
    expect(screen.getByText("OHIF")).toBeInTheDocument();
  });
});
