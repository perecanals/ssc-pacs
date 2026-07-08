import { describe, it, expect, vi } from "vitest";
import { render, screen } from "@testing-library/react";

import WarmButton from "../components/DataTable/WarmButton";

// Regression: React 19 ignores defaultProps on function components, so the
// old `WarmButton.defaultProps = { baseClass: "link-btn" }` rendered
// className="undefined dt__warm-btn ..." — the default must be a parameter.
describe("WarmButton default baseClass", () => {
  it("falls back to link-btn when baseClass is not provided", () => {
    render(<WarmButton status="cold" onWarm={vi.fn()} />);
    const btn = screen.getByRole("button", { name: "Decompress" });
    expect(btn.className).toBe("link-btn dt__warm-btn dt__warm-btn--cold");
    expect(btn.className).not.toContain("undefined");
  });

  it("uses an explicit baseClass when provided", () => {
    render(<WarmButton status="hot" onWarm={vi.fn()} baseClass="my-btn" />);
    const btn = screen.getByRole("button", { name: "Ready" });
    expect(btn.className).toBe("my-btn dt__warm-btn dt__warm-btn--hot");
  });
});
