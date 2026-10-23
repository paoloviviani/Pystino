/**
 * Spend, tokens and requests over time, broken down by one dimension.
 *
 * Reads the report endpoint — `group_by=day` with a `split_by` — through the
 * hook the page passes in, so the chart and the tables above it are the same
 * aggregation and cannot disagree. The page also hands in its period, so the
 * period picker over the tables is the chart's range selector too.
 *
 * Drawn as inline SVG: the console has no charting dependency and a stacked bar
 * per day needs none. Accessible by construction — the figure has a name and a
 * description, every bar segment has a text alternative, the colours come from
 * a palette that survives the common colour-vision deficiencies, and a table of
 * exactly the plotted numbers is one button away.
 */

import { Card, EmptyState, Notice, Select, Spinner, formatMoney } from "@llmp/ui";
import type { UseQueryResult } from "@tanstack/react-query";
import { useState } from "react";

import { formatNumber } from "../lib/format";
import { buildPlot, niceScale } from "../lib/plots";
import type { Metric, PlotData, Series } from "../lib/plots";
import type { UsageReport } from "../lib/types";
import { m } from "../paraglide/messages.js";

export type PlotSplit = "group" | "user" | "model" | "provider";

const METRICS: { value: Metric; label: () => string }[] = [
  { value: "cost", label: m["plots.metric.cost"] },
  { value: "tokens", label: m["plots.metric.tokens"] },
  { value: "requests", label: m["plots.metric.requests"] },
];

const SPLIT_LABELS: Record<PlotSplit, () => string> = {
  group: m["plots.split.group"],
  user: m["plots.split.user"],
  model: m["plots.split.model"],
  provider: m["plots.split.provider"],
};

export interface UsagePlotsProps {
  /** The breakdowns this audience may see. A person's own page leaves out
   * "user": pinned to the caller it could only ever be one series. */
  splits: PlotSplit[];
  /** Fetches the daily report for a breakdown (period and filters are the
   * page's). A hook, called unconditionally on every render. */
  useChartReport: (split: PlotSplit) => UseQueryResult<UsageReport, unknown>;
}

export function UsagePlots({ splits, useChartReport }: UsagePlotsProps) {
  const [metric, setMetric] = useState<Metric>("cost");
  const [split, setSplit] = useState<PlotSplit>(splits[0] ?? "model");
  const report = useChartReport(split);

  return (
    <Card title={m["plots.title"]()} description={m["plots.subtitle"]()}>
      <div className="grid gap-4 [grid-template-columns:repeat(auto-fit,minmax(11rem,1fr))]">
        <Select
          label={m["plots.filter.metric"]()}
          value={metric}
          onChange={(event) => setMetric(event.target.value as Metric)}
        >
          {METRICS.map((option) => (
            <option key={option.value} value={option.value}>
              {option.label()}
            </option>
          ))}
        </Select>
        <Select
          label={m["plots.filter.split"]()}
          value={split}
          onChange={(event) => setSplit(event.target.value as PlotSplit)}
        >
          {splits.map((value) => (
            <option key={value} value={value}>
              {SPLIT_LABELS[value]()}
            </option>
          ))}
        </Select>
      </div>

      <div className="mt-5">
        {report.isPending && <Spinner label={m["plots.running"]()} />}
        {report.error ? (
          <Notice tone="danger" title={m["plots.error.title"]()}>
            {/* i18n: server text — the API's own error message. */}
            {report.error instanceof Error ? report.error.message : m["plots.error.unknown"]()}
          </Notice>
        ) : null}
        {report.data && (
          <PlotBody report={report.data} metric={metric} split={split} />
        )}
      </div>
    </Card>
  );
}

function PlotBody({
  report,
  metric,
  split,
}: {
  report: UsageReport;
  metric: Metric;
  split: PlotSplit;
}) {
  const [showTable, setShowTable] = useState(false);
  const plot = buildPlot(report, metric, m["plots.other"]());
  const currency = report.currency;

  if (plot.series.length === 0) {
    return (
      <EmptyState
        title={m["plots.empty.title"]()}
        detail={m["plots.empty.detail"]({ period: report.period.label })}
      />
    );
  }

  const label = m["plots.chart_label"]({
    metric: METRICS.find((option) => option.value === metric)?.label() ?? "",
    split: SPLIT_LABELS[split]().toLowerCase(),
    period: report.period.label,
  });
  const show = (value: number | string) => formatValue(value, metric, currency);

  return (
    <div className="flex flex-col gap-4" data-testid="usage-plot">
      <StackedBars plot={plot} metric={metric} currency={currency} label={label} />

      <ul className="m-0 flex list-none flex-wrap gap-x-5 gap-y-2 p-0 text-body" aria-label={m["plots.legend"]()}>
        {plot.series.map((series) => (
          <li key={series.key} className="flex items-center gap-2">
            <span
              aria-hidden="true"
              className="inline-block size-3 rounded-sm"
              style={{ background: swatch(series) }}
            />
            <span className="text-ink">{series.label}</span>
            <span className="text-ink-faint tabular-nums">{show(series.total)}</span>
          </li>
        ))}
      </ul>

      <div>
        <button
          type="button"
          className="text-body text-accent underline underline-offset-2"
          aria-expanded={showTable}
          onClick={() => setShowTable((open) => !open)}
        >
          {showTable ? m["plots.table.hide"]() : m["plots.table.show"]()}
        </button>
        {showTable && <PlotTable plot={plot} show={show} label={label} />}
      </div>
    </div>
  );
}

function formatValue(value: number | string, metric: Metric, currency: string): string {
  // Cents on a chart: its sums and axis read at a glance; the tables keep the
  // console's usual precision for exact figures.
  if (metric === "cost") return formatMoney(String(value), currency, { maxDecimals: 2 });
  return formatNumber(Number(value));
}

/** The palette lives in index.css as `--plot-N`, so it follows the theme. */
function swatch(series: Series): string {
  return series.isOther ? "var(--plot-other)" : `var(--plot-${series.colour + 1})`;
}

const WIDTH = 720;
const HEIGHT = 280;
const PAD = { top: 12, right: 8, bottom: 28, left: 56 };

function StackedBars({
  plot,
  metric,
  currency,
  label,
}: {
  plot: PlotData;
  metric: Metric;
  currency: string;
  label: string;
}) {
  const totals = plot.dayTotals.map(Number);
  const scale = niceScale(Math.max(0, ...totals));
  const innerWidth = WIDTH - PAD.left - PAD.right;
  const innerHeight = HEIGHT - PAD.top - PAD.bottom;
  const slot = innerWidth / plot.days.length;
  const barWidth = Math.max(1, Math.min(28, slot * 0.72));
  const y = (value: number) => PAD.top + innerHeight - (value / scale.top) * innerHeight;
  // Thin the day labels so they do not collide on a long range.
  const every = Math.max(1, Math.ceil(plot.days.length / 10));

  return (
    // A floor on the width: scaled into a phone's 300px the axis text is 5px
    // tall. Below it the chart scrolls sideways inside its own box instead.
    <div className="overflow-x-auto">
    <svg
      role="img"
      aria-label={label}
      viewBox={`0 0 ${WIDTH} ${HEIGHT}`}
      className="h-auto w-full min-w-[34rem] text-ink-muted"
    >
      <title>{label}</title>
      {scale.ticks.map((tick) => (
        <g key={tick}>
          <line
            x1={PAD.left}
            x2={WIDTH - PAD.right}
            y1={y(tick)}
            y2={y(tick)}
            stroke="var(--colour-border)"
            strokeWidth={1}
          />
          <text
            x={PAD.left - 6}
            y={y(tick)}
            textAnchor="end"
            dominantBaseline="middle"
            fontSize={11}
            fill="currentColor"
          >
            {metric === "cost"
              ? formatMoney(String(tick), currency, { maxDecimals: 2 })
              : new Intl.NumberFormat(undefined, { notation: "compact" }).format(tick)}
          </text>
        </g>
      ))}
      {plot.days.map((day, index) => {
        const x = PAD.left + slot * index + (slot - barWidth) / 2;
        let base = 0;
        return (
          <g key={day}>
            {plot.series.map((series) => {
              const value = Number(series.values[index] ?? 0);
              if (!(value > 0)) return null;
              const top = y(base + value);
              const height = y(base) - top;
              base += value;
              return (
                <rect
                  key={series.key}
                  x={x}
                  y={top}
                  width={barWidth}
                  height={Math.max(height, 0.5)}
                  fill={swatch(series)}
                  stroke="var(--colour-surface)"
                  strokeWidth={0.75}
                >
                  <title>
                    {day} · {series.label} · {formatValue(series.values[index] ?? 0, metric, currency)}
                  </title>
                </rect>
              );
            })}
            {index % every === 0 && (
              <text
                x={x + barWidth / 2}
                y={HEIGHT - 8}
                textAnchor="middle"
                fontSize={11}
                fill="currentColor"
              >
                {day.slice(5)}
              </text>
            )}
          </g>
        );
      })}
    </svg>
    </div>
  );
}

function PlotTable({
  plot,
  show,
  label,
}: {
  plot: PlotData;
  show: (value: number | string) => string;
  label: string;
}) {
  return (
    <div className="mt-3 max-h-80 overflow-auto rounded-md border border-line-quiet">
      <table className="w-full border-collapse text-body tabular-nums">
        <caption className="sr-only">{label}</caption>
        <thead className="sticky top-0 bg-sunken text-left">
          <tr>
            <th scope="col" className="px-3 py-2 font-medium">
              {m["plots.table.day"]()}
            </th>
            {plot.series.map((series) => (
              <th key={series.key} scope="col" className="px-3 py-2 text-right font-medium">
                {series.label}
              </th>
            ))}
            <th scope="col" className="px-3 py-2 text-right font-medium">
              {m["plots.table.total"]()}
            </th>
          </tr>
        </thead>
        <tbody>
          {plot.days.map((day, index) => (
            <tr key={day} className="border-t border-line-quiet">
              <th scope="row" className="px-3 py-1.5 text-left font-normal">
                {day}
              </th>
              {plot.series.map((series) => (
                <td key={series.key} className="px-3 py-1.5 text-right">
                  {show(series.values[index] ?? 0)}
                </td>
              ))}
              <td className="px-3 py-1.5 text-right font-medium">{show(plot.dayTotals[index] ?? 0)}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}
