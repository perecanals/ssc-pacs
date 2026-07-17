import { describe, it, expect, vi } from "vitest";
import { render, screen } from "@testing-library/react";

vi.mock("../context/AuthContext", () => ({
  useAuth: () => ({ currentUser: "tester", isAdmin: false }),
}));
vi.mock("../api/client", () => ({
  apiGet: vi.fn().mockResolvedValue([]),
  apiPost: vi.fn().mockResolvedValue({ ok: true }),
  apiDelete: vi.fn().mockResolvedValue({ ok: true }),
}));

import InlineEdit from "../components/InlineEdit";

const ENTITY = {
  seriesinstanceuid: "1.2.3",
  studyinstanceuid: "1.2",
  patient_id: "P-1",
};
const ANN = [{ id: 1, label: "note", value: "recorded" }];

function renderCell(labelDef, datatype = "text") {
  return render(
    <InlineEdit
      level="series"
      entity={ENTITY}
      labelName="note"
      datatype={datatype}
      annotations={ANN}
      onMutated={() => {}}
      labelDef={labelDef}
    />,
  );
}

const LOCKED = { name: "note", edit_policy: "nobody", edit_users: [] };

// The server is the real gate (403 on POST/DELETE); this only checks we do not
// offer an editor that would fail.
describe("InlineEdit honours a label's edit policy", () => {
  it("offers an editor when the policy is everyone", () => {
    renderCell({ name: "note", edit_policy: "everyone", edit_users: [] });
    expect(screen.getByRole("textbox")).toBeInTheDocument();
  });

  it("offers an editor when no labelDef is supplied", () => {
    renderCell(undefined);
    expect(screen.getByRole("textbox")).toBeInTheDocument();
  });

  it("renders a read-only value when the policy is nobody", () => {
    renderCell(LOCKED);
    expect(screen.queryByRole("textbox")).not.toBeInTheDocument();
    expect(screen.getByText("recorded")).toBeInTheDocument();
  });

  it("renders a read-only value when the user is not in edit_users", () => {
    renderCell({
      name: "note",
      edit_policy: "users",
      edit_users: ["someone_else"],
    });
    expect(screen.queryByRole("textbox")).not.toBeInTheDocument();
    expect(screen.getByText("recorded")).toBeInTheDocument();
  });

  it("offers an editor when the user IS in edit_users", () => {
    renderCell({ name: "note", edit_policy: "users", edit_users: ["tester"] });
    expect(screen.getByRole("textbox")).toBeInTheDocument();
  });

  it("explains why a locked value is not editable", () => {
    renderCell(LOCKED);
    expect(screen.getByTitle(/locked/i)).toBeInTheDocument();
  });

  // A pill hash-colours by value so equal values group by eye. That is
  // meaningful for a select vocabulary and a lie for free text, where every
  // value is distinct (e.g. a femoral_sheath_time timestamp).
  it("renders a locked text value as plain text, with no pill", () => {
    const { container } = renderCell(LOCKED, "text");
    expect(container.querySelector(".auto-pill")).toBeNull();
    expect(container.querySelector(".select-pill")).toBeNull();
    const el = screen.getByText("recorded");
    expect(el.tagName).toBe("SPAN");
    expect(el).not.toHaveAttribute("style");
  });

  it("renders a locked select value as a muted outlined pill", () => {
    const { container } = renderCell(LOCKED, "select");
    expect(container.querySelector(".auto-pill")).not.toBeNull();
    // The editable pill, not the read-only one, would mean it looks clickable.
    expect(container.querySelector(".select-pill")).toBeNull();
  });

  it("renders an editable select value as the normal clickable pill", () => {
    const { container } = renderCell(
      { name: "note", edit_policy: "everyone", edit_users: [] },
      "select",
    );
    expect(container.querySelector(".auto-pill")).toBeNull();
  });
});
