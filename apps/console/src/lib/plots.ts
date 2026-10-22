/**
 * Turning a daily report into chart series.
 *
 * The chart reads the same `UsageReport` the tables read — `group_by=day` with
 * a `split_by` — and does no aggregation of its own beyond folding the smallest
 * series into "Other", which is a presentation choice (how many colours a
 * person can tell apart) and not a second source of figures. Money stays a
 * decimal string everywhere it is *shown*: the folded "Other" cell is summed as
 * integers of a billionth-of-a-billionth, never as floats. Floats are used for
 * one thing only, the position of a bar.
 */

import type { UsageReport } from "./types";

export type Metric = "cost" | "tokens" | "requests";

/** Series drawn individually before the rest are folded into "Other". */
export const TOP_SERIES = 6;

/** The key an "Other" series carries; never a real subject's key. */
export const OTHER_KEY = "__other__";

export interface Series {
  key: string;
  label: string;
  /** One value per entry of `PlotData.days`; decimal strings for `cost`. */
  values: string[];
  total: string;
  /** Position in the palette, or -1 for "Other" (the neutral swatch). */
  colour: number;
  isOther: boolean;
}

export interface PlotData {
  days: string[];
  series: Series[];
  /** Per-day column totals, same units as the series values. */
  dayTotals: string[];
}

const SCALE = 12;

/** Decimal string -> integer count of 10^-12, exactly. */
export function toUnits(value: string): bigint {
  const match = /^(-?)(\d+)(?:\.(\d+))?$/.exec(value.trim());
  if (!match) return 0n;
  const [, sign = "", whole = "0", fraction = ""] = match;
  const units = BigInt(whole) * 10n ** BigInt(SCALE) + BigInt(fraction.padEnd(SCALE, "0").slice(0, SCALE));
  return sign === "-" ? -units : units;
}

export function fromUnits(units: bigint): string {
  const negative = units < 0n;
  const abs = negative ? -units : units;
  const whole = abs / 10n ** BigInt(SCALE);
  const fraction = (abs % 10n ** BigInt(SCALE)).toString().padStart(SCALE, "0");
  return `${negative ? "-" : ""}${whole}.${fraction}`;
}

function cellValue(row: UsageReport["rows"][number], metric: Metric): bigint {
  if (metric === "cost") return toUnits(row.cost);
  // Counts are scaled into the same integer space so one code path sums both.
  const count = metric === "tokens" ? row.total_tokens : row.requests;
  return BigInt(Math.round(count)) * 10n ** BigInt(SCALE);
}

function render(units: bigint, metric: Metric): string {
  return metric === "cost" ? fromUnits(units) : (units / 10n ** BigInt(SCALE)).toString();
}

/** `YYYY-MM-DD` of an instant, in a named timezone. */
export function localDay(instant: Date, timeZone: string): string {
  const parts = new Intl.DateTimeFormat("en-CA", {
    timeZone,
    year: "numeric",
    month: "2-digit",
    day: "2-digit",
  }).formatToParts(instant);
  const get = (type: string) => parts.find((part) => part.type === type)?.value ?? "";
  return `${get("year")}-${get("month")}-${get("day")}`;
}

function addDay(day: string): string {
  const [year = 0, month = 1, date = 1] = day.split("-").map(Number);
  const next = new Date(Date.UTC(year, month - 1, date + 1));
  return next.toISOString().slice(0, 10);
}

/**
 * Every calendar day of the period up to today (or the last day with data, if
 * later), so a quiet day is a visible gap and not a missing bar. Capped so a
 * multi-year range cannot ask for thousands of bars.
 */
export function dayRange(report: UsageReport, now: Date = new Date()): string[] {
  const zone = report.period.timezone || "UTC";
  const observed = report.rows.map((row) => row.key ?? "").filter(Boolean).sort();
  const start = localDay(new Date(report.period.start), zone);
  const lastInPeriod = localDay(new Date(new Date(report.period.end).getTime() - 1), zone);
  const today = localDay(now, zone);
  let last = today < lastInPeriod ? today : lastInPeriod;
  const lastObserved = observed[observed.length - 1];
  if (lastObserved && lastObserved > last) last = lastObserved;
  const first = observed[0] && observed[0] < start ? observed[0] : start;

  const days: string[] = [];
  for (let day = first; day <= last && days.length < 400; day = addDay(day)) days.push(day);
  return days;
}

/** Series for one metric: the top N by the metric's total, then "Other". */
export function buildPlot(
  report: UsageReport,
  metric: Metric,
  otherLabel: string,
  now: Date = new Date(),
  top: number = TOP_SERIES,
): PlotData {
  const days = dayRange(report, now);
  const dayIndex = new Map(days.map((day, index) => [day, index]));

  const bySplit = new Map<string, { label: string; cells: bigint[] }>();
  for (const row of report.rows) {
    const index = dayIndex.get(row.key ?? "");
    if (index === undefined) continue;
    const key = row.split_key ?? "";
    const entry = bySplit.get(key) ?? {
      label: row.split_label ?? row.label,
      cells: days.map(() => 0n),
    };
    entry.cells[index] = (entry.cells[index] ?? 0n) + cellValue(row, metric);
    bySplit.set(key, entry);
  }

  const total = (cells: bigint[]) => cells.reduce((sum, cell) => sum + cell, 0n);
  const ranked = [...bySplit.entries()]
    .map(([key, entry]) => ({ key, label: entry.label, cells: entry.cells, sum: total(entry.cells) }))
    .filter((entry) => entry.sum > 0n)
    // Ties broken by label so the colours are stable between renders.
    .sort((a, b) => (a.sum === b.sum ? a.label.localeCompare(b.label) : a.sum > b.sum ? -1 : 1));

  const shown = ranked.slice(0, top);
  const rest = ranked.slice(top);
  const series: Series[] = shown.map((entry, colour) => ({
    key: entry.key,
    label: entry.label,
    values: entry.cells.map((cell) => render(cell, metric)),
    total: render(entry.sum, metric),
    colour,
    isOther: false,
  }));
  if (rest.length > 0) {
    const cells = days.map((_, index) =>
      rest.reduce((sum, entry) => sum + (entry.cells[index] ?? 0n), 0n),
    );
    series.push({
      key: OTHER_KEY,
      label: otherLabel,
      values: cells.map((cell) => render(cell, metric)),
      total: render(total(cells), metric),
      colour: -1,
      isOther: true,
    });
  }

  const dayTotals = days.map((_, index) =>
    render(
      ranked.reduce((sum, entry) => sum + (entry.cells[index] ?? 0n), 0n),
      metric,
    ),
  );
  return { days, series, dayTotals };
}

/** A round upper bound and four evenly spaced ticks for a value axis. */
export function niceScale(max: number): { top: number; ticks: number[] } {
  if (!(max > 0)) return { top: 1, ticks: [0, 0.25, 0.5, 0.75, 1] };
  const rough = max / 4;
  const magnitude = 10 ** Math.floor(Math.log10(rough));
  const step = ([1, 2, 2.5, 5, 10].find((n) => n * magnitude >= rough) ?? 10) * magnitude;
  const top = step * 4;
  return { top, ticks: [0, 1, 2, 3, 4].map((i) => Number((i * step).toPrecision(12))) };
}
