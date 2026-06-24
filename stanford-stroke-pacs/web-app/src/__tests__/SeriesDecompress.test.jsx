import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, fireEvent, waitFor } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";

// One series row at the series level. In cold_path_cache mode (and logged in)
// its actions cell should carry a series-backed Decompress/readiness badge that
// warms just THAT SERIES (not the parent study).
const SERIES_ROW = {
  seriesinstanceuid: "1.2.3.4.5.6",
  studyinstanceuid: "1.2.3.4.5",
  patient_id: "P-0001",
  modality: "CT",
  seriesdescription: "AxialWarmTest",
  annotations: [],
  inherited_annotations: [],
};

vi.mock("../api/client", () => ({
  apiFetch: vi.fn().mockResolvedValue({ ok: true, json: () => Promise.resolve({}) }),
  apiGet: vi.fn().mockImplementation((path) => {
    if (path === "/api/me") return Promise.resolve({ username: "tester", is_admin: false });
    if (path === "/api/storage-mode") return Promise.resolve({ storage_mode: "cold_path_cache" });
    if (path === "/api/label-definitions") return Promise.resolve([]);
    if (path === "/api/labels/summary") return Promise.resolve([]);
    if (path === "/api/study-import-labels") return Promise.resolve([]);
    if (path === "/api/datasets") return Promise.resolve([]);
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

const queueWarmSeries = vi.fn().mockResolvedValue();
vi.mock("../api/warmOhif", () => ({
  getStorageMode: vi.fn().mockResolvedValue("cold_path_cache"),
  getBatchCacheStatus: vi.fn().mockResolvedValue({ studies: {}, patients: {}, series: {} }),
  queueWarmStudy: vi.fn().mockResolvedValue(),
  queueWarmSeries: (...a) => queueWarmSeries(...a),
  queueWarmPatient: vi.fn().mockResolvedValue(),
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
  await screen.findByText("AxialWarmTest");
}

describe("Series row decompress (series-backed)", () => {
  beforeEach(() => queueWarmSeries.mockClear());

  it("shows a Decompress button on series rows and warms just that series", async () => {
    await renderSeriesLevel();
    const btn = await screen.findByRole("button", { name: "Decompress" });
    fireEvent.click(btn);
    await waitFor(() => expect(queueWarmSeries).toHaveBeenCalledWith("1.2.3.4.5.6"));
  });
});
