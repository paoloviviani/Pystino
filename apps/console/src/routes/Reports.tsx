/**
 * A person's own consumption, in detail.
 *
 * The split from Overview is deliberate and is the point of this page existing:
 * Overview answers "where am I against my limits", which is a glance. This
 * answers "what did I actually spend it on", which is a sitting-down question —
 * and it is where the report's caveats belong.
 *
 * Those caveats used to sit on Overview beside the spend figure, where they
 * were the loudest thing on a screen most people open to check one number.
 * They are not decoration: "9 requests are charged from our price table"
 * changes what the total means. But they are for the person reading the
 * breakdown, not the person checking their balance.
 */

import { Button, Card, Money, Notice, Select, Spinner, Stat, SummaryStrip, Table } from "@llmp/ui";
import type { Column } from "@llmp/ui";
import { useState } from "react";

import { PageHeader } from "../components/PageHeader";
import { Disclosures } from "../components/Disclosures";
import { UsagePlots } from "../components/UsagePlots";
import { downloadCsv } from "../lib/api";
import { recentPeriods } from "../lib/periods";
import { useMyReport } from "../lib/queries";
import type { UsageReportRow } from "../lib/types";
import {
  FILTERS,
  MUTED,
  PAGE,
  STATS,
} from "../lib/layout";
import { formatNumber } from "../lib/format";
import { m } from "../paraglide/messages.js";

/**
 * No "by user" and no "by group".
 *
 * The admin report has both. Here every row would be the reader, and a
 * breakdown whose every answer is your own name is a control that looks broken.
 */
const BREAKDOWNS = [
  { value: "model", label: m["reports.breakdown.model"] },
  { value: "provider", label: m["reports.breakdown.provider"] },
  { value: "api_key", label: m["reports.breakdown.api_key"] },
  { value: "day", label: m["reports.breakdown.day"] },
  { value: "total", label: m["reports.breakdown.total"] },
];

export function Reports() {
  const periods = recentPeriods();
  const [period, setPeriod] = useState(periods[0]?.value ?? "");
  const [groupBy, setGroupBy] = useState("model");

  const report = useMyReport(period, groupBy);
  const currency = report.data?.currency ?? "EUR";

  const columns: Column<UsageReportRow>[] = [
    { key: "label", header: headingFor(groupBy), render: (row) => row.label },
    {
      key: "requests",
      header: m["reports.column.requests"](),
      numeric: true,
      render: (row) => formatNumber(row.requests),
    },
    {
      key: "tokens",
      header: m["reports.column.tokens"](),
      numeric: true,
      render: (row) => formatNumber(row.total_tokens),
    },
    {
      key: "images",
      header: m["reports.column.images"](),
      numeric: true,
      // A dash, not a zero: most rows are not image rows, and a column of
      // zeroes reads as "none were generated" rather than "not applicable".
      render: (row) =>
        row.images ? formatNumber(row.images) : <span className={MUTED}>—</span>,
    },
    {
      key: "audio",
      header: m["reports.column.audio"](),
      numeric: true,
      // A dash, not a zero, for the reason the images column has one. Seconds
      // as billed (a minute rate is applied to them), so the figure reconciles
      // with the provider's invoice.
      hideBelow: "md",
      render: (row) =>
        Number(row.audio_seconds) > 0 ? (
          formatNumber(Number(row.audio_seconds))
        ) : (
          <span className={MUTED}>—</span>
        ),
    },
    {
      key: "searches",
      header: m["reports.column.searches"](),
      numeric: true,
      // Same dash rule as images, and the column exists for a sharper reason:
      // this charge lands on ordinary chat rows, so without it part of the
      // spend has no visible cause at all (ADR 0058).
      hideBelow: "md",
      render: (row) =>
        row.searches ? formatNumber(row.searches) : <span className={MUTED}>—</span>,
    },
    {
      key: "own_searches",
      header: m["reports.column.own_searches"](),
      numeric: true,
      // Beside that column rather than folded into it, and the header says
      // "own" for the same reason: one is the provider searching while it
      // serves a request, the other is this deployment calling a search API.
      // Added together they reconcile against neither invoice.
      //
      // These are billed per request at the price set for their backend, and
      // that is in Cost. The ones no price covered sit in Cost as zero, so the
      // cell names them: a count with a silent gap in the money beside it would
      // read as free. The disclosures say the rest in words.
      hideBelow: "md",
      render: (row) =>
        row.own_searches ? (
          <>
            {formatNumber(row.own_searches)}
            {row.unpriced_own_searches > 0 && (
              <span className={MUTED}>
                {" "}
                {m["reports.own_searches_unpriced"]({ count: row.unpriced_own_searches })}
              </span>
            )}
          </>
        ) : (
          <span className={MUTED}>—</span>
        ),
    },
    {
      key: "cost",
      header: m["reports.column.cost"](),
      numeric: true,
      // A model row with a native figure reports it in the price's currency
      // (ADR 0054) — that is what the counterparty charges — while the total
      // and every aggregate stay in the billing currency.
      render: (row) =>
        row.native_cost !== null && row.native_currency ? (
          <Money amount={row.native_cost} currency={row.native_currency} />
        ) : (
          <Money amount={row.cost} currency={currency} />
        ),
    },
  ];

  return (
    <div className={PAGE}>
      <PageHeader title={m["reports.title"]()} subtitle={m["reports.subtitle"]()} />

      <Card>
        <div className={FILTERS}>
          <Select
            label={m["reports.filter.period"]()}
            value={period}
            onChange={(event) => setPeriod(event.target.value)}
          >
            {/* i18n: server text — the period labels are built by lib/periods.ts,
                which is outside the conversion. */}
            {periods.map((option) => (
              <option key={option.value} value={option.value}>
                {option.label}
              </option>
            ))}
          </Select>
          <Select
            label={m["reports.filter.breakdown"]()}
            value={groupBy}
            onChange={(event) => setGroupBy(event.target.value)}
          >
            {BREAKDOWNS.map((option) => (
              <option key={option.value} value={option.value}>
                {option.label()}
              </option>
            ))}
          </Select>
        </div>
      </Card>

      <Card>
        {report.isPending && <Spinner label={m["reports.running"]()} />}
        {report.error ? (
          <Notice tone="danger" title={m["reports.error.title"]()}>
            {/* i18n: server text — the API's own error message. */}
            {report.error instanceof Error ? report.error.message : m["reports.error.unknown"]()}
          </Notice>
        ) : null}
        {report.data && (
          <>
            <div className={STATS}>
              <Stat
                label={m["reports.stat.spend"]({ period: report.data.period.label })}
                value={<Money amount={report.data.totals.cost} currency={currency} />}
                detail={m["reports.stat.period_detail"]({ timezone: report.data.period.timezone })}
              />
              <Stat
                label={m["reports.stat.requests"]()}
                value={formatNumber(report.data.totals.requests)}
                detail={m["reports.stat.tokens_detail"]({ tokens: report.data.totals.total_tokens })}
              />
              <Stat
                label={m["reports.stat.measured"]()}
                value={m["reports.stat.measured_value"]({ percent: measuredPercent(report.data.totals) })}
                detail={
                  measuredPercent(report.data.totals) < 100
                    ? m["reports.stat.measured_detail.incomplete"]()
                    : m["reports.stat.measured_detail.complete"]()
                }
                tone={measuredPercent(report.data.totals) < 100 ? "warn" : "ok"}
              />
            </div>

            {/* The caveats collapse to one line; the component keeps the
                API's wording verbatim and remembers the choice per page. */}
            <Disclosures notes={report.data.disclosures} storageKey="llmp.console.disclosures.reports" />
          </>
        )}
      </Card>

      {/* Pinned to the caller server-side (/api/me), like the tables. No
          "user" breakdown: it could only ever be one series. */}
      <UsagePlots
        splits={["model", "provider", "group"]}
        useChartReport={(splitBy) => useMyReport(period, "day", splitBy)}
      />

      <Card title={m["reports.breakdown_card"]()} flush>
        {report.isPending ? (
          <Spinner />
        ) : (
          <>
            {report.data && (
              <div className="p-5">
                <SummaryStrip
                  headline={m["reports.row_count"]({ count: report.data.rows.length })}
                  detail={m["reports.summary_detail"]({
                    period: report.data.period.label,
                    breakdown: headingLowerFor(groupBy),
                  })}
                  active={report.data.rows.length > 0}
                  actions={
                    <Button
                      onClick={() =>
                        downloadCsv(
                          `/api/me/reports/usage.csv?period=${encodeURIComponent(
                            period,
                          )}&group_by=${encodeURIComponent(groupBy)}`,
                        )
                      }
                    >
                      {m["reports.export_csv"]()}
                    </Button>
                  }
                />
              </div>
            )}
            <Table
              columns={columns}
              rows={report.data?.rows ?? []}
              rowKey={(row, index) => row.key ?? `row-${index}`}
              footer={report.data?.totals}
              empty={m["reports.empty"]()}
              caption={m["reports.caption"]({
                period: report.data?.period.label ?? m["reports.caption_period_fallback"](),
                breakdown: headingLowerFor(groupBy),
              })}
            />
          </>
        )}
      </Card>
    </div>
  );
}

function headingFor(groupBy: string): string {
  return (
    {
      model: m["reports.heading.model"](),
      provider: m["reports.heading.provider"](),
      api_key: m["reports.heading.api_key"](),
      day: m["reports.heading.day"](),
      total: m["reports.heading.total"](),
    }[groupBy] ?? m["reports.heading.other"]()
  );
}

/** The same nouns in the case the sentences need; see AdminReports.tsx. */
function headingLowerFor(groupBy: string): string {
  return (
    {
      model: m["reports.heading_lower.model"](),
      provider: m["reports.heading_lower.provider"](),
      api_key: m["reports.heading_lower.api_key"](),
      day: m["reports.heading_lower.day"](),
      total: m["reports.heading_lower.total"](),
    }[groupBy] ?? m["reports.heading_lower.other"]()
  );
}

/** How much of the spend rests on usage the provider actually reported. */
function measuredPercent(totals: UsageReportRow): number {
  const unmeasured = totals.estimated_requests + totals.unavailable_requests;
  if (totals.requests === 0) return 100;
  return Math.round(((totals.requests - unmeasured) / totals.requests) * 100);
}
