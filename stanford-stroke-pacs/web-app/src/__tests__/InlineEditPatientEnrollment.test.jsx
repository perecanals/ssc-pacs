import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, fireEvent } from "@testing-library/react";

vi.mock("../context/AuthContext", () => ({
  useAuth: () => ({ currentUser: "tester", isAdmin: false }),
}));
vi.mock("../api/client", () => ({
  apiGet: vi.fn().mockResolvedValue([]),
  apiPost: vi.fn().mockResolvedValue({ ok: true }),
  apiDelete: vi.fn().mockResolvedValue({ ok: true }),
}));

import { apiPost } from "../api/client";
import InlineEdit from "../components/InlineEdit";

// A patient label on a study row: the person is enrolled in two datasets, each
// enrollment with its own value (Alembic 0026).
const INHERITED = [
  {
    id: 1,
    level: "patient",
    label: "note",
    value: "from-lvo",
    patient_key: "lvo__P-1",
    patient_id: "P-1",
    dataset: "lvo",
  },
  {
    id: 2,
    level: "patient",
    label: "note",
    value: "from-crisp",
    patient_key: "crisp2__P-1",
    patient_id: "OL-9",
    dataset: "crisp2",
  },
];

function renderCell(entity, annotations = INHERITED) {
  return render(
    <InlineEdit
      level="patient"
      entity={{ studyinstanceuid: "1.2", ...entity }}
      labelName="note"
      datatype="text"
      annotations={annotations}
      onMutated={() => {}}
    />,
  );
}

describe("patient labels on study/series rows are per enrollment", () => {
  beforeEach(() => apiPost.mockClear());

  it("edits the one enrollment in view and shows its value", () => {
    renderCell({ edit_patient_key: "crisp2__P-1" });
    const box = screen.getByRole("textbox");
    expect(box).toHaveValue("from-crisp");
    fireEvent.change(box, { target: { value: "changed" } });
    fireEvent.blur(box);
    expect(apiPost).toHaveBeenCalledWith("/api/annotations", {
      level: "patient",
      label: "note",
      value: "changed",
      patient_key: "crisp2__P-1",
    });
  });

  it("is read-only when several enrollments are in view", () => {
    renderCell({ edit_patient_key: null });
    expect(screen.queryByRole("textbox")).not.toBeInTheDocument();
    const cell = screen.getByText("from-lvo | from-crisp");
    expect(cell.title).toMatch(/P-1 \(lvo\): from-lvo/);
    expect(cell.title).toMatch(/OL-9 \(crisp2\): from-crisp/);
  });

  it("shows nothing when no enrollment has a value", () => {
    const { container } = renderCell({ edit_patient_key: null }, []);
    expect(container).toBeEmptyDOMElement();
  });
});
