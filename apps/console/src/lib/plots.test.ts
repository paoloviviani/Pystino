import { describe, expect, it } from "vitest";

import { buildPlot, dayRange, fromUnits, niceScale, toUnits } from "./plots";
import type { UsageReport, UsageReportRow } from "./types";

function row(day: string, split: string, cost: string, requests = 1, tokens = 10): UsageReportRow {
  return {
    key: day,
    label: day,
    split_key: split,
    split_label: split.toUpperCase(),
    requests,
    prompt_tokens: 0,
    completion_tokens: 0,
    total_tokens: tokens,
    images: 0,
    searches: 0,
    own_searches: 0,
    cost,
    native_cost: null,
    native_currency: null,
    estimated_requests: 0,
    unavailable_requests: 0,
  };
}

function report(rows: UsageReportRow[]): UsageReport {
  return {
    period: {
      kind: "month",
      label: "2026-08",
      start: "2026-07-31T22:00:00Z",
      end: "2026-08-31T22:00:00Z",
      timezone: "Europe/Rome",
    },
    group_by: "day",
    split_by: "provider",
    currency: "EUR",
    rows,
    totals: row("", "", "0"),
    disclosures: [],
  };
}

const NOW = new Date("2026-08-05T12:00:00Z");

describe("dayRange", () => {
  it("runs from the period's first local day to today", () => {
    const days = dayRange(report([]), NOW);
    expect(days[0]).toBe("2026-08-01");
    expect(days.at(-1)).toBe("2026-08-05");
  });

  it("covers a finished period in full, in the billing timezone", () => {
    const days = dayRange(report([]), new Date("2026-10-01T00:00:00Z"));
    expect(days).toHaveLength(31);
    expect(days.at(-1)).toBe("2026-08-31");
  });
});

describe("buildPlot", () => {
  it("folds everything past the top N into Other, and keeps the totals", () => {
    const rows = ["a", "b", "c", "d"].map((split, i) => row("2026-08-02", split, String(10 - i)));
    const plot = buildPlot(report(rows), "cost", "Other", NOW, 2);
    expect(plot.series.map((s) => s.label)).toEqual(["A", "B", "Other"]);
    const other = plot.series[2];
    expect(other?.isOther).toBe(true);
    expect(other?.total).toBe("15.000000000000");
    // The columns still add up to the report's own figure.
    expect(plot.dayTotals[1]).toBe("34.000000000000");
  });

  it("sums money exactly, never through a float", () => {
    const rows = [row("2026-08-02", "a", "0.1"), row("2026-08-02", "a", "0.2")];
    const plot = buildPlot(report(rows), "cost", "Other", NOW);
    expect(plot.series[0]?.values[1]).toBe("0.300000000000");
  });

  it("plots tokens and requests as whole numbers", () => {
    const rows = [row("2026-08-02", "a", "1", 3, 120)];
    expect(buildPlot(report(rows), "tokens", "Other", NOW).series[0]?.total).toBe("120");
    expect(buildPlot(report(rows), "requests", "Other", NOW).series[0]?.total).toBe("3");
  });

  it("has no series when nothing was spent", () => {
    expect(buildPlot(report([]), "cost", "Other", NOW).series).toEqual([]);
    expect(buildPlot(report([row("2026-08-02", "a", "0")]), "cost", "Other", NOW).series).toEqual(
      [],
    );
  });

  it("gives a quiet day a zero rather than dropping it", () => {
    const plot = buildPlot(report([row("2026-08-03", "a", "2")]), "cost", "Other", NOW);
    expect(plot.days).toHaveLength(5);
    expect(plot.series[0]?.values[0]).toBe("0.000000000000");
  });
});

describe("units and scale", () => {
  it("round-trips a twelve-place decimal", () => {
    expect(fromUnits(toUnits("1.000000000001"))).toBe("1.000000000001");
  });

  it("picks a round axis that contains the maximum", () => {
    const { top, ticks } = niceScale(37);
    expect(top).toBeGreaterThanOrEqual(37);
    expect(ticks).toHaveLength(5);
    expect(niceScale(0).top).toBe(1);
  });
});
