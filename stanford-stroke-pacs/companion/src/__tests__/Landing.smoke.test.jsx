import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import Landing from "../pages/Landing";

// Landing doesn't need auth, but wrapping in MemoryRouter for <Link>.
describe("Landing page", () => {
  it("renders without crashing and shows the title", () => {
    render(
      <MemoryRouter>
        <Landing />
      </MemoryRouter>,
    );
    expect(
      screen.getByText("Stanford Stroke Center PACS"),
    ).toBeInTheDocument();
  });

  it("renders all three navigation cards", () => {
    render(
      <MemoryRouter>
        <Landing />
      </MemoryRouter>,
    );
    expect(screen.getByText("Companion")).toBeInTheDocument();
    expect(screen.getByText("Orthanc Explorer")).toBeInTheDocument();
    expect(screen.getByText("OHIF Viewer")).toBeInTheDocument();
  });
});
