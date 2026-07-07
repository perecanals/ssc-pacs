import { describe, it, expect } from "vitest";
import { compareSelectValues } from "../utils/table";

describe("compareSelectValues", () => {
  it("orders numeric strings by value, not lexicographically (ASPECTS-style)", () => {
    const values = ["0", "1", "10", "2", "3", "9"];
    expect([...values].sort(compareSelectValues)).toEqual(["0", "1", "2", "3", "9", "10"]);
  });

  it("puts non-numeric strings first (naive order), numeric strings after", () => {
    const values = ["10", "unknown", "2", "n/a", "0"];
    expect([...values].sort(compareSelectValues)).toEqual(["n/a", "unknown", "0", "2", "10"]);
  });

  it("handles decimals and negatives numerically", () => {
    const values = ["1.5", "-2", "10", "0.5"];
    expect([...values].sort(compareSelectValues)).toEqual(["-2", "0.5", "1.5", "10"]);
  });

  it("treats whitespace-only and non-finite strings as text", () => {
    const values = ["3", "Infinity", "NaN", "1"];
    expect([...values].sort(compareSelectValues)).toEqual(["Infinity", "NaN", "1", "3"]);
  });
});
