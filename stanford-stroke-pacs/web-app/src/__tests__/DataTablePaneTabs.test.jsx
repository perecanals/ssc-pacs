import { describe, it, expect, vi, afterEach } from "vitest";
import { render, screen, waitFor, fireEvent } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";

// Same shape as Navigator.smoke.test.jsx — DataTable pulls preferences and
// label definitions on mount.
vi.mock("../api/client", () => ({
  apiFetch: vi
    .fn()
    .mockResolvedValue({ ok: true, json: () => Promise.resolve({}) }),
  apiGet: vi.fn().mockImplementation((path) => {
    if (path === "/api/me") return Promise.resolve({ username: "testuser" });
    if (path === "/api/storage-mode")
      return Promise.resolve({ storage_mode: "legacy" });
    if (path === "/api/label-definitions") return Promise.resolve([]);
    if (path === "/api/labels/summary") return Promise.resolve([]);
    if (path === "/api/study-import-labels") return Promise.resolve([]);
    if (path.startsWith("/api/preferences/"))
      return Promise.resolve({ prefs: {} });
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
import DataTable from "../components/DataTable";

const PREVIEW_URL = "/ohif/viewer?StudyInstanceUIDs=1.2.3";

function renderTable(props = {}) {
  return render(
    <MemoryRouter>
      <AuthProvider>
        <DataTable
          level="patient"
          filters={{}}
          previewOpen
          previewUrl={PREVIEW_URL}
          onPreviewClose={() => {}}
          {...props}
        />
      </AuthProvider>
    </MemoryRouter>,
  );
}

describe("DataTable pane tabs", () => {
  it("calls onPreviewFullscreen when Fullscreen is clicked", async () => {
    const onPreviewFullscreen = vi.fn();
    renderTable({ onPreviewFullscreen });

    const btn = await screen.findByRole("button", { name: /fullscreen/i });
    fireEvent.click(btn);
    expect(onPreviewFullscreen).toHaveBeenCalledTimes(1);
  });

  it("omits Fullscreen when no handler is provided", async () => {
    renderTable();
    // Open in New Tab still renders, so this asserts the gate, not an empty footer.
    await screen.findByRole("link", { name: /open in new tab/i });
    expect(screen.queryByRole("button", { name: /fullscreen/i })).toBeNull();
  });

  it("keeps Open in New Tab alongside Fullscreen", async () => {
    renderTable({ onPreviewFullscreen: vi.fn() });

    await waitFor(() => {
      expect(
        screen.getByRole("button", { name: /fullscreen/i }),
      ).toBeInTheDocument();
    });
    expect(
      screen.getByRole("link", { name: /open in new tab/i }),
    ).toHaveAttribute("href", PREVIEW_URL);
  });

  describe("Second Screen tab", () => {
    // jsdom has neither getScreenDetails nor screen.isExtended; these tests
    // install both to simulate Chromium with an extended desktop. The popup
    // open/navigate logic itself lives in useSecondScreenViewer (tested in
    // hooks/__tests__); DataTable only gates and forwards.
    function installWindowManagement() {
      Object.defineProperty(window.screen, "isExtended", {
        value: true,
        configurable: true,
      });
      window.getScreenDetails = vi.fn();
    }

    afterEach(() => {
      delete window.getScreenDetails;
      delete window.screen.isExtended;
    });

    it("is hidden when the Window Management API is absent (non-Chromium)", async () => {
      renderTable({ onOpenSecondScreen: vi.fn() });
      await screen.findByRole("link", { name: /open in new tab/i });
      expect(
        screen.queryByRole("button", { name: /second screen/i }),
      ).toBeNull();
    });

    it("calls onOpenSecondScreen when clicked", async () => {
      installWindowManagement();
      const onOpenSecondScreen = vi.fn();
      renderTable({ onOpenSecondScreen });

      fireEvent.click(
        await screen.findByRole("button", { name: /second screen/i }),
      );
      expect(onOpenSecondScreen).toHaveBeenCalledTimes(1);
    });

    it("leaves the footer empty while the popup is live and idle", async () => {
      installWindowManagement();
      renderTable({
        previewOpen: false,
        secondScreenActive: true,
        onOpenSecondScreen: vi.fn(),
      });

      // The popup owns the viewer: no lingering Second Screen button, and the
      // pane's own tabs are gone with the pane collapsed.
      await waitFor(() => {
        expect(
          screen.queryByRole("button", { name: /second screen/i }),
        ).toBeNull();
      });
      expect(
        screen.queryByRole("link", { name: /open in new tab/i }),
      ).toBeNull();
    });

    it("surfaces routing status transiently, and focuses the popup on click", async () => {
      installWindowManagement();
      const onOpenSecondScreen = vi.fn();
      renderTable({
        previewOpen: false,
        secondScreenActive: true,
        secondScreenStatus: "Warming imaging cache…",
        onOpenSecondScreen,
      });

      const chip = await screen.findByRole("button", {
        name: /warming imaging cache/i,
      });
      fireEvent.click(chip);
      expect(onOpenSecondScreen).toHaveBeenCalledTimes(1);
    });
  });
});
