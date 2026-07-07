import { describe, it, expect, vi } from "vitest";
import { render, screen, fireEvent, waitFor } from "@testing-library/react";

// The select dropdown must render in a portal on <body>: inline in the cell it
// gets clipped by the sub-tables' overflow-auto scroll wrappers
// (.dt__child-scroll / .dt__gc-scroll), hiding it behind the outer table.
const SERIES_ENTITY = {
  seriesinstanceuid: "1.2.3.4.5.6",
  studyinstanceuid: "1.2.3.4.5",
  patient_id: "P-0001",
};

vi.mock("../api/client", () => ({
  apiGet: vi.fn().mockImplementation((path) => {
    if (path === "/api/me") {
      return Promise.resolve({ username: "tester", is_admin: false });
    }
    if (path.startsWith("/api/labels/")) return Promise.resolve([]);
    return Promise.resolve({});
  }),
  apiPost: vi.fn().mockResolvedValue({ ok: true, json: () => Promise.resolve({}) }),
  apiDelete: vi.fn().mockResolvedValue({ ok: true }),
  markApiActivity: vi.fn(),
  getLastApiActivityAt: vi.fn(() => Date.now()),
}));

import { apiPost } from "../api/client";
import { AuthProvider } from "../context/AuthContext";
import InlineEdit from "../components/InlineEdit";

async function renderSelectEdit(onMutated = vi.fn()) {
  const { container } = render(
    <AuthProvider>
      <InlineEdit
        level="series"
        entity={SERIES_ENTITY}
        labelName="timepoint"
        datatype="select"
        defOptions={["baseline", "followup"]}
        annotations={[]}
        onMutated={onMutated}
      />
    </AuthProvider>,
  );
  // The trigger renders once auth resolves (logged-out mode is read-only).
  fireEvent.click(await screen.findByRole("button", { name: /select/i }));
  return { container, onMutated };
}

describe("SelectEdit dropdown portal", () => {
  it("renders the open dropdown on <body>, outside the component tree", async () => {
    const { container } = await renderSelectEdit();
    const dropdown = document.body.querySelector(".select-edit__dropdown");
    expect(dropdown).not.toBeNull();
    expect(container.contains(dropdown)).toBe(false);
  });

  it("selecting an option posts the annotation and closes the dropdown", async () => {
    const { onMutated } = await renderSelectEdit();
    fireEvent.click(screen.getByRole("button", { name: "baseline" }));
    await waitFor(() => expect(onMutated).toHaveBeenCalled());
    expect(apiPost).toHaveBeenCalledWith(
      "/api/annotations",
      expect.objectContaining({ level: "series", label: "timepoint", value: "baseline" }),
    );
    expect(document.body.querySelector(".select-edit__dropdown")).toBeNull();
  });

  it("closes when an ancestor container scrolls (fixed dropdown would detach)", async () => {
    await renderSelectEdit();
    expect(document.body.querySelector(".select-edit__dropdown")).not.toBeNull();
    fireEvent.scroll(document.body);
    expect(document.body.querySelector(".select-edit__dropdown")).toBeNull();
  });
});
