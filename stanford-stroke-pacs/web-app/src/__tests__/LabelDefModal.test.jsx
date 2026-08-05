import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, waitFor, fireEvent } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";

vi.mock("../api/client", () => ({
  apiFetch: vi
    .fn()
    .mockResolvedValue({ ok: true, json: () => Promise.resolve({}) }),
  apiGet: vi.fn(),
  apiPost: vi
    .fn()
    .mockResolvedValue({ ok: true, json: () => Promise.resolve({}) }),
  apiPatch: vi
    .fn()
    .mockResolvedValue({ ok: true, json: () => Promise.resolve({}) }),
  markApiActivity: vi.fn(),
  getLastApiActivityAt: vi.fn(() => Date.now()),
}));

import { apiGet, apiPost, apiPatch } from "../api/client";
import { AuthProvider } from "../context/AuthContext";
import LabelDefModal from "../components/LabelDefModal";

const ME = { username: "alice", is_admin: false, allowed_datasets: ["lvo"] };

const SELECT_LABEL = {
  id: 7,
  name: "scan_timepoint",
  description: null,
  level: "study",
  datatype: "select",
  options: ["FU24_NCCT", "Last_DWI"],
  instrument: null,
  created_by: "alice",
  edit_policy: "everyone",
  edit_users: [],
};

function mockApi({ usage = {} } = {}) {
  apiGet.mockImplementation((path) => {
    if (path === "/api/me") return Promise.resolve(ME);
    if (path === "/api/instruments") return Promise.resolve([]);
    if (path.endsWith("/value-usage")) return Promise.resolve(usage);
    return Promise.resolve({});
  });
}

function renderModal(props = {}) {
  return render(
    <MemoryRouter>
      <AuthProvider>
        <LabelDefModal onClose={vi.fn()} onSaved={vi.fn()} {...props} />
      </AuthProvider>
    </MemoryRouter>,
  );
}

describe("LabelDefModal", () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  it("edit mode with an open policy allows adding a value and PATCHes options", async () => {
    mockApi();
    renderModal({ existingLabel: SELECT_LABEL });
    const input = await screen.findByPlaceholderText(
      "Type a value and press Enter",
    );
    fireEvent.change(input, { target: { value: "New_CTA" } });
    fireEvent.keyDown(input, { key: "Enter" });
    expect(screen.getByText("New_CTA")).toBeInTheDocument();

    fireEvent.click(screen.getByText("Save"));
    await waitFor(() => {
      expect(apiPatch).toHaveBeenCalledWith(
        "/api/label-definitions/7",
        expect.objectContaining({
          options: ["FU24_NCCT", "Last_DWI", "New_CTA"],
        }),
      );
    });
  });

  it("removing an unused value needs no confirmation", async () => {
    mockApi({ usage: {} });
    renderModal({ existingLabel: SELECT_LABEL });
    await screen.findByText("FU24_NCCT");
    const [removeFirst] = screen.getAllByText("×");
    fireEvent.click(removeFirst);
    expect(screen.queryByText("FU24_NCCT")).not.toBeInTheDocument();
  });

  it("removing an in-use value asks for confirmation first", async () => {
    mockApi({ usage: { FU24_NCCT: 3 } });
    renderModal({ existingLabel: SELECT_LABEL });
    await screen.findByText("FU24_NCCT");
    // Let the usage fetch land before clicking remove.
    await waitFor(() =>
      expect(apiGet).toHaveBeenCalledWith(
        "/api/labels/scan_timepoint/value-usage",
      ),
    );
    const [removeFirst] = screen.getAllByText("×");
    fireEvent.click(removeFirst);
    // Still listed — a confirmation is showing instead.
    expect(screen.getByText(/currently assigned to/)).toBeInTheDocument();
    fireEvent.click(screen.getByText("Remove"));
    await waitFor(() => {
      expect(
        screen.queryByText(/currently assigned to/),
      ).not.toBeInTheDocument();
    });
    expect(screen.queryByText("FU24_NCCT")).not.toBeInTheDocument();
  });

  it("edit mode with policy 'nobody' hides the value editor", async () => {
    mockApi();
    renderModal({
      existingLabel: { ...SELECT_LABEL, edit_policy: "nobody" },
    });
    await screen.findByText("FU24_NCCT");
    expect(
      screen.queryByPlaceholderText("Type a value and press Enter"),
    ).not.toBeInTheDocument();
    expect(screen.queryByText("×")).not.toBeInTheDocument();
    expect(
      screen.getByText(
        "You don't have permission to edit this label's values.",
      ),
    ).toBeInTheDocument();
  });

  it("create mode rejects an invalid name with an error naming the field", async () => {
    mockApi();
    renderModal();
    fireEvent.change(screen.getByPlaceholderText(/hemorrhagic/), {
      target: { value: "my label" },
    });
    fireEvent.click(screen.getByText("Create"));
    expect(
      await screen.findByText(/Name may only contain letters/),
    ).toBeInTheDocument();
    expect(apiPost).not.toHaveBeenCalledWith(
      "/api/label-definitions",
      expect.anything(),
    );
  });
});
