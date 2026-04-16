import { describe, it, expect } from "vitest";
import { hashStr, valueColor, NOTION_COLORS } from "../colors";

describe("hashStr", () => {
  it("returns a non-negative integer", () => {
    expect(hashStr("hello")).toBeGreaterThanOrEqual(0);
    expect(Number.isInteger(hashStr("hello"))).toBe(true);
  });

  it("is deterministic", () => {
    expect(hashStr("test")).toBe(hashStr("test"));
  });

  it("returns 0 for empty string", () => {
    expect(hashStr("")).toBe(0);
  });

  it("differs for different strings", () => {
    expect(hashStr("a")).not.toBe(hashStr("b"));
  });
});

describe("valueColor", () => {
  it("returns an object with bg and text keys", () => {
    const c = valueColor("hello");
    expect(c).toHaveProperty("bg");
    expect(c).toHaveProperty("text");
  });

  it("returns a color from NOTION_COLORS", () => {
    const c = valueColor("anything");
    expect(NOTION_COLORS).toContainEqual(c);
  });

  it("is deterministic", () => {
    expect(valueColor("x")).toEqual(valueColor("x"));
  });
});
