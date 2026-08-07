import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { render, screen, fireEvent, waitFor } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";

// One series row, so the series-level table renders an actions cell.
const SERIES_ROW = {
  seriesinstanceuid: "1.2.3.4.5.6",
  studyinstanceuid: "1.2.3.4.5",
  patient_id: "P-0001",
  modality: "CT",
  seriesdescription: "AxialForcePane",
  annotations: [],
  inherited_annotations: [],
};

vi.mock("../api/client", () => ({
  apiFetch: vi
    .fn()
    .mockResolvedValue({ ok: true, json: () => Promise.resolve({}) }),
  apiGet: vi.fn().mockImplementation((path) => {
    if (path === "/api/me")
      return Promise.resolve({ username: "tester", is_admin: false });
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

const resolveOhifViewerUrl = vi.fn();
vi.mock("../api/warmOhif", () => ({
  getStorageMode: vi.fn().mockResolvedValue("legacy"),
  resolveOhifViewerUrl: (...a) => resolveOhifViewerUrl(...a),
}));

const openViewerWindow = vi.fn();
vi.mock("../utils/secondScreen", async (importOriginal) => ({
  ...(await importOriginal()),
  // Keep the real hasSecondScreen so the button is labelled from the
  // simulated Window Management API below.
  openViewerWindow: (...a) => openViewerWindow(...a),
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
  await screen.findByText("AxialForcePane");
}

function paneIframe() {
  return document.querySelector("iframe[title='OHIF preview']");
}

describe("Pane action (force the pane open)", () => {
  beforeEach(() => {
    resolveOhifViewerUrl.mockReset();
    resolveOhifViewerUrl.mockResolvedValue("/ohif/viewer?StudyInstanceUIDs=1");
    openViewerWindow.mockReset();
  });

  afterEach(() => {
    delete window.getScreenDetails;
    delete window.screen.isExtended;
  });

  it("opens the preview pane from the row action", async () => {
    await renderSeriesLevel();

    fireEvent.click(screen.getByRole("button", { name: /^pane$/i }));

    await waitFor(() => expect(paneIframe()).toBeTruthy());
    expect(resolveOhifViewerUrl).toHaveBeenCalledWith(
      SERIES_ROW.studyinstanceuid,
      SERIES_ROW.seriesinstanceuid,
    );
  });

  it("still opens the pane while the second-screen popup owns row clicks", async () => {
    Object.defineProperty(window.screen, "isExtended", {
      value: true,
      configurable: true,
    });
    window.getScreenDetails = vi.fn();
    const popup = {
      closed: false,
      focus: vi.fn(),
      location: { replace: vi.fn() },
    };
    openViewerWindow.mockResolvedValue(popup);
    await renderSeriesLevel();

    // Route the row to the popup first (row click -> pane -> Second Screen).
    fireEvent.click(screen.getByText("AxialForcePane"));
    fireEvent.click(
      await screen.findByRole("button", { name: /second screen/i }),
    );
    await waitFor(() => expect(openViewerWindow).toHaveBeenCalledTimes(1));
    // Popup owns the viewer now: the pane is collapsed and its iframe dropped.
    await waitFor(() => expect(paneIframe()).toBeNull());

    // A plain row click re-points the popup rather than reopening the pane.
    fireEvent.click(screen.getByText("AxialForcePane"));
    await waitFor(() => expect(resolveOhifViewerUrl).toHaveBeenCalledTimes(2));
    expect(paneIframe()).toBeNull();

    // The Pane action overrides that routing.
    fireEvent.click(screen.getByRole("button", { name: /^pane$/i }));
    await waitFor(() => expect(paneIframe()).toBeTruthy());
    // The popup was never navigated by the forced-pane selection.
    expect(popup.location.replace).toHaveBeenCalledTimes(0);
  });
});
