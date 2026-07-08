import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, fireEvent, waitFor } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";

// Session state stored under the `_global` preferences level; swapped
// per-test before render.
let globalPrefs = {};

vi.mock("../api/client", () => ({
  apiFetch: vi.fn().mockResolvedValue({ ok: true, json: () => Promise.resolve({}) }),
  apiGet: vi.fn().mockImplementation((path) => {
    if (path === "/api/me") return Promise.resolve({ username: "tester", is_admin: false });
    if (path === "/api/storage-mode") return Promise.resolve({ storage_mode: "legacy" });
    if (path === "/api/label-definitions") return Promise.resolve([]);
    if (path === "/api/labels/summary") return Promise.resolve([]);
    if (path === "/api/study-import-labels") return Promise.resolve(["PRECISE"]);
    if (path === "/api/datasets") return Promise.resolve(["lvo", "crisp2"]);
    if (path === "/api/preferences/_global") return Promise.resolve({ prefs: globalPrefs });
    if (path.startsWith("/api/preferences/")) return Promise.resolve({ prefs: {} });
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

import { apiFetch } from "../api/client";
import { AuthProvider } from "../context/AuthContext";
import Navigator from "../pages/Navigator";

function renderNavigator() {
  return render(
    <MemoryRouter>
      <AuthProvider>
        <Navigator />
      </AuthProvider>
    </MemoryRouter>,
  );
}

describe("Navigator session state persistence", () => {
  beforeEach(() => {
    globalPrefs = {};
    vi.mocked(apiFetch).mockClear();
  });

  it("restores level and sidebar filters from the _global prefs", async () => {
    globalPrefs = {
      session: { level: "study", filters: { dataset: "lvo", studyImportLabel: "PRECISE" } },
    };
    renderNavigator();

    // Study level restored → Dataset + Import-label dropdowns render (no longer
    // pruned at non-patient levels) with the saved values; the import-label
    // section is titled "Import Label" (not the patient-only "Study Import Label").
    await screen.findByRole("heading", { name: "Import Label" });
    // The dropdown values settle via a separate async flow (the /api/datasets
    // + import-label fetches populating the <option>s), which can lag the
    // heading render — poll rather than assert synchronously.
    await waitFor(() => {
      expect(document.getElementById("sidebar-dataset").value).toBe("lvo");
      expect(document.getElementById("sidebar-study-import-label").value).toBe("PRECISE");
    });
  });

  it("falls back to defaults when the stored session is invalid", async () => {
    globalPrefs = { session: { level: "bogus", filters: { nonsense: 1, modality: 7 } } };
    renderNavigator();

    // Patient level → the Dataset quick filter section renders, unfiltered.
    await screen.findByRole("heading", { name: "Dataset" });
    const [dataset] = screen.getAllByRole("combobox");
    expect(dataset.value).toBe("");
  });

  it("saves level + filters to _global after a change (debounced)", async () => {
    renderNavigator();
    fireEvent.click(await screen.findByRole("button", { name: /^series$/i }));

    await waitFor(
      () => {
        const put = vi.mocked(apiFetch).mock.calls.find(
          ([path, opts]) => path === "/api/preferences/_global" && opts?.method === "PUT",
        );
        expect(put).toBeTruthy();
        const body = JSON.parse(put[1].body);
        expect(body.prefs.session.level).toBe("series");
        expect(body.prefs.session.filters.modality).toBeNull();
        expect(body.prefs.session.filters.dataset).toBeNull();
      },
      { timeout: 3000 },
    );
  });
});
