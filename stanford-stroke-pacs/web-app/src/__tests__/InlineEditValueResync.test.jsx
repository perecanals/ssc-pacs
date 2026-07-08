import { describe, it, expect, vi } from "vitest";
import { render, screen, fireEvent } from "@testing-library/react";

vi.mock("../context/AuthContext", () => ({
  useAuth: () => ({ currentUser: "tester", isAdmin: false }),
}));
vi.mock("../api/client", () => ({
  apiGet: vi.fn().mockResolvedValue([]),
  apiPost: vi.fn().mockResolvedValue({ ok: true }),
  apiDelete: vi.fn().mockResolvedValue({ ok: true }),
}));

import InlineEdit from "../components/InlineEdit";

const ENTITY = { seriesinstanceuid: "1.2.3", studyinstanceuid: "1.2", patient_id: "P-1" };

function renderCell(annotations) {
  return render(
    <InlineEdit
      level="series"
      entity={ENTITY}
      labelName="note"
      datatype="text"
      annotations={annotations}
      onMutated={() => {}}
    />,
  );
}

describe("ValueEdit external-edit resync", () => {
  it("updates the input when another user's value arrives via reload", () => {
    const { rerender } = renderCell([]);
    const input = screen.getByRole("textbox");
    expect(input.value).toBe("");

    rerender(
      <InlineEdit
        level="series"
        entity={ENTITY}
        labelName="note"
        datatype="text"
        annotations={[{ id: 1, label: "note", value: "from-elsewhere" }]}
        onMutated={() => {}}
      />,
    );
    expect(input.value).toBe("from-elsewhere");
  });

  it("does not clobber a locally edited (dirty) field", () => {
    const { rerender } = renderCell([]);
    const input = screen.getByRole("textbox");
    fireEvent.change(input, { target: { value: "my draft" } });

    rerender(
      <InlineEdit
        level="series"
        entity={ENTITY}
        labelName="note"
        datatype="text"
        annotations={[{ id: 1, label: "note", value: "from-elsewhere" }]}
        onMutated={() => {}}
      />,
    );
    expect(input.value).toBe("my draft");
  });
});
