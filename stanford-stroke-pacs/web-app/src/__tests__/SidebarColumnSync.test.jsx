import { describe, it, expect, vi } from "vitest";
import { render, screen, fireEvent, waitFor } from "@testing-library/react";
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

const LABEL = {
  name: "SyncLabel",
  level: "series",
  datatype: "bool",
  description: "",
  options: [],
  instrument: null,
};
const SELECT_LABEL = {
  name: "SelectSync",
  level: "series",
  datatype: "select",
  description: "",
  options: ["OptA", "OptB"],
  instrument: null,
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
    if (path === "/api/label-definitions")
      return Promise.resolve([LABEL, SELECT_LABEL]);
    if (path === "/api/labels/summary") {
      return Promise.resolve([
        { label: "SyncLabel", level: "series", instrument: null, count: 1 },
        { label: "SelectSync", level: "series", instrument: null, count: 1 },
      ]);
    }
    if (path === "/api/study-import-labels") return Promise.resolve([]);
    if (path.startsWith("/api/preferences/"))
      return Promise.resolve({ prefs: {} });
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
  apiPut: vi
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

import { apiGet } from "../api/client";
import { AuthProvider } from "../context/AuthContext";
import Navigator from "../pages/Navigator";

describe("Sidebar label filter <-> ColumnSelector sync", () => {
  it("enables UID columns and sends their header filters to the series API", async () => {
    render(
      <MemoryRouter>
        <AuthProvider>
          <Navigator />
        </AuthProvider>
      </MemoryRouter>,
    );
    fireEvent.click(await screen.findByRole("button", { name: /^series$/i }));
    await screen.findByText("AxialSyncTest");
    expect(
      screen.queryByText(SERIES_ROW.seriesinstanceuid),
    ).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: /displayed columns/i }));
    const studyBoxes = screen.getAllByRole("checkbox", {
      name: "Study Instance UID",
    });
    studyBoxes.forEach((box) => expect(box).not.toBeChecked());
    const seriesBox = screen.getByRole("checkbox", {
      name: "Series Instance UID",
    });
    expect(seriesBox).not.toBeChecked();
    fireEvent.click(studyBoxes[1]);
    fireEvent.click(seriesBox);
    fireEvent.click(document.body);
    await screen.findByText(SERIES_ROW.studyinstanceuid);
    await screen.findByText(SERIES_ROW.seriesinstanceuid);
    const headers = screen.getAllByRole("columnheader");
    for (const [label, value] of [
      ["Study Instance UID", "3.4.5"],
      ["Series Instance UID", "5.6"],
    ]) {
      const header = screen.getByRole("columnheader", {
        name: new RegExp(label),
      });
      const filter = headers[headers.indexOf(header) + headers.length / 2];
      fireEvent.change(filter.querySelector("input"), { target: { value } });
      await waitFor(() => {
        expect(
          apiGet.mock.calls.some(([url]) => {
            const params = new URL(url, "http://localhost").searchParams;
            return (
              params.get(
                label === "Study Instance UID"
                  ? "studyinstanceuid"
                  : "seriesinstanceuid",
              ) === value
            );
          }),
        ).toBe(true);
      });
    }
  });

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
      fireEvent.click(
        screen.getByRole("button", { name: /displayed columns/i }),
      );

    // Initially the label column is not enabled in the dropdown.
    openDropdown();
    expect(
      screen.getByRole("checkbox", { name: /SyncLabel/ }),
    ).not.toBeChecked();
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
      expect(
        screen.getByRole("checkbox", { name: /SyncLabel/ }),
      ).not.toBeChecked(),
    );

    // The sidebar quick filter is still active (column hidden, filter not cleared).
    const stillActive = container.querySelector('li[aria-label="SyncLabel"]');
    expect(stillActive.className).toMatch(/sidebar__label-item--active/);
  });

  it("selecting a value in a select-label quick filter checks its column; hiding it is not undone by further value picks", async () => {
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
      fireEvent.click(
        screen.getByRole("button", { name: /displayed columns/i }),
      );

    openDropdown();
    expect(
      screen.getByRole("checkbox", { name: /SelectSync/ }),
    ).not.toBeChecked();
    fireEvent.click(document.body);

    // Pin the select label's value popup and pick a value.
    const labelItem = container.querySelector('li[aria-label="SelectSync"]');
    expect(labelItem).toBeTruthy();
    fireEvent.click(labelItem);
    fireEvent.click(await screen.findByRole("checkbox", { name: /OptA/ }));

    // The column is now enabled.
    await waitFor(() => {
      openDropdown();
      expect(
        screen.getByRole("checkbox", { name: /SelectSync/ }),
      ).toBeChecked();
    });

    // Hide the column, then pick a second value — the hide must stick.
    fireEvent.click(screen.getByRole("checkbox", { name: /SelectSync/ }));
    await waitFor(() =>
      expect(
        screen.getByRole("checkbox", { name: /SelectSync/ }),
      ).not.toBeChecked(),
    );
    fireEvent.click(document.body);
    fireEvent.click(screen.getByRole("checkbox", { name: /OptB/ }));
    openDropdown();
    expect(
      screen.getByRole("checkbox", { name: /SelectSync/ }),
    ).not.toBeChecked();
  });
});
