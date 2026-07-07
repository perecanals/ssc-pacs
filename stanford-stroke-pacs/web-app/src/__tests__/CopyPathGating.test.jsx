import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, fireEvent, waitFor } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";

// One series row so the series-level table renders an actions cell with the
// admin-only copy-path buttons (folder = DICOM dir, box = compressed archive).
const SERIES_ROW = {
  seriesinstanceuid: "1.2.3.4.5.6",
  studyinstanceuid: "1.2.3.4.5",
  patient_id: "P-0001",
  modality: "CT",
  seriesdescription: "AxialCopyPathTest",
  annotations: [],
  inherited_annotations: [],
};

const SERIES_PATHS = {
  dicom_dir_path: "/data/imaging/P-0001/1.2.3.4.5/Axial/1.2.3.4.5.6/DICOM",
  dicom_archive_path: "/data/compressed/P-0001/1.2.3.4.5/Axial/1.2.3.4.5.6/DICOM.tar.zst",
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
    if (path.endsWith("/paths")) return Promise.resolve(SERIES_PATHS);
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
  fireEvent.click(await screen.findByRole("button", { name: /^series$/i }));
  await screen.findByText("AxialCopyPathTest");
}

describe("copy-path button gating", () => {
  beforeEach(() => {
    isAdminFlag = false;
  });

  it("shows both copy-path buttons to admins", async () => {
    isAdminFlag = true;
    await renderSeriesLevel();
    expect(screen.getByTitle("Copy DICOM directory path")).toBeInTheDocument();
    expect(screen.getByTitle("Copy compressed archive path")).toBeInTheDocument();
  });

  it("hides the copy-path buttons from non-admins", async () => {
    isAdminFlag = false;
    await renderSeriesLevel();
    expect(screen.queryByTitle("Copy DICOM directory path")).not.toBeInTheDocument();
    expect(screen.queryByTitle("Copy compressed archive path")).not.toBeInTheDocument();
  });

  it("clicking copies the respective path to the clipboard", async () => {
    isAdminFlag = true;
    const writeText = vi.fn().mockResolvedValue(undefined);
    Object.defineProperty(navigator, "clipboard", {
      value: { writeText },
      configurable: true,
    });
    await renderSeriesLevel();

    fireEvent.click(screen.getByTitle("Copy DICOM directory path"));
    await waitFor(() => expect(writeText).toHaveBeenCalledWith(SERIES_PATHS.dicom_dir_path));

    fireEvent.click(screen.getByTitle("Copy compressed archive path"));
    await waitFor(() =>
      expect(writeText).toHaveBeenCalledWith(SERIES_PATHS.dicom_archive_path),
    );
  });
});
