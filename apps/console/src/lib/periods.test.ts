import { describe, expect, it } from "vitest";
import { recentPeriods } from "./periods";

/**
 * The period picker offers *named* periods, and the values it produces are the
 * ones the gateway parses. A label that does not round-trip is a 400 the reader
 * cannot do anything about, so the format matters more than it looks.
 */
describe("recentPeriods", () => {
  it("offers the current month first", () => {
    const options = recentPeriods(new Date(2026, 7, 15));
    expect(options[0]).toEqual({ value: "2026-08", label: "August 2026 (current)" });
  });

  it("pads the month so the value matches the gateway's format", () => {
    // `2026-3` is not a period the API accepts; `2026-03` is.
    const options = recentPeriods(new Date(2026, 2, 15));
    expect(options[0]?.value).toBe("2026-03");
  });

  it("walks back across a year boundary", () => {
    const values = recentPeriods(new Date(2026, 1, 10), 4).map((option) => option.value);
    expect(values.slice(0, 4)).toEqual(["2026-02", "2026-01", "2025-12", "2025-11"]);
  });

  it("does not skip a month when run on the 31st", () => {
    // Subtracting 30 days from 31 March lands in March again, and the naive
    // version of this silently offered the same month twice.
    const values = recentPeriods(new Date(2026, 2, 31), 3).map((option) => option.value);
    expect(values.slice(0, 3)).toEqual(["2026-03", "2026-02", "2026-01"]);
  });

  it("offers the current quarter and year", () => {
    const values = recentPeriods(new Date(2026, 7, 15)).map((option) => option.value);
    expect(values).toContain("2026-Q3");
    expect(values).toContain("2026");
  });

  it("puts a January date in Q1", () => {
    const values = recentPeriods(new Date(2026, 0, 5)).map((option) => option.value);
    expect(values).toContain("2026-Q1");
  });

  it("produces no duplicate values", () => {
    const values = recentPeriods(new Date(2026, 11, 31), 6).map((option) => option.value);
    expect(new Set(values).size).toBe(values.length);
  });
});
