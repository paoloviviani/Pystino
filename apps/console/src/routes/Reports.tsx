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

import { Button, Card, Money, Notice, Select, Spinner, Stat, Table } from "@llmp/ui";
import type { Column } from "@llmp/ui";
import { useState } from "react";

import { PageHeader } from "../components/PageHeader";
import { downloadCsv } from "../lib/api";
import { recentPeriods } from "../lib/periods";
import { useMyReport } from "../lib/queries";
import type { UsageReportRow } from "../lib/types";
import {
  DISCLOSURES,
  FILTERS,
  MUTED,
  PAGE,
  STATS,
} from "../lib/layout";

/**
 * No "by user" and no "by group".
 *
 * The admin report has both. Here every row would be the reader, and a
 * breakdown whose every answer is your own name is a control that looks broken.
 */
const BREAKDOWNS = [
  { value: "model", label: "By model" },
  { value: "api_key", label: "By API key" },
  { value: "day", label: "By day" },
  { value: "total", label: "Total only" },
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
      header: "Requests",
      numeric: true,
      render: (row) => row.requests.toLocaleString(),
    },
    {
      key: "tokens",
      header: "Tokens",
      numeric: true,
      render: (row) => row.total_tokens.toLocaleString(),
    },
    {
      key: "images",
      header: "Images",
      numeric: true,
      // A dash, not a zero: most rows are not image rows, and a column of
      // zeroes reads as "none were generated" rather than "not applicable".
      render: (row) =>
        row.images ? row.images.toLocaleString() : <span className={MUTED}>—</span>,
    },
    {
      key: "cost",
      header: "Spend",
      numeric: true,
      render: (row) => <Money amount={row.cost} currency={currency} />,
    },
  ];

  return (
    <div className={PAGE}>
      <PageHeader title="Your usage" subtitle="What you spent, and what the figures rest on." />

      <Card>
        <div className={FILTERS}>
          <Select
            label="Period"
            value={period}
            onChange={(event) => setPeriod(event.target.value)}
          >
            {periods.map((option) => (
              <option key={option.value} value={option.value}>
                {option.label}
              </option>
            ))}
          </Select>
          <Select
            label="Breakdown"
            value={groupBy}
            onChange={(event) => setGroupBy(event.target.value)}
          >
            {BREAKDOWNS.map((option) => (
              <option key={option.value} value={option.value}>
                {option.label}
              </option>
            ))}
          </Select>
        </div>
      </Card>

      <Card>
        {report.isPending && <Spinner label="Running the report" />}
        {report.error ? (
          <Notice tone="danger" title="Could not run the report">
            {report.error instanceof Error ? report.error.message : "Unknown error."}
          </Notice>
        ) : null}
        {report.data && (
          <>
            <div className={STATS}>
              <Stat
                label={`Spend · ${report.data.period.label}`}
                value={<Money amount={report.data.totals.cost} currency={currency} />}
                detail={`${report.data.period.timezone} calendar period`}
              />
              <Stat
                label="Requests"
                value={report.data.totals.requests.toLocaleString()}
                detail={`${report.data.totals.total_tokens.toLocaleString()} tokens`}
              />
              <Stat
                label="Measured"
                value={`${measuredPercent(report.data.totals)}%`}
                detail={
                  measuredPercent(report.data.totals) < 100
                    ? "the rest is estimated or missing"
                    : "exact usage from the provider"
                }
                tone={measuredPercent(report.data.totals) < 100 ? "warn" : "ok"}
              />
            </div>

            {/* The API's own wording, verbatim. Restating a caveat in the UI is
                how the two end up disagreeing about what the number means. */}
            {report.data.disclosures.length > 0 && (
              <div className={DISCLOSURES}>
                {report.data.disclosures.map((note) => (
                  <Notice key={note} tone="warn">
                    {note}
                  </Notice>
                ))}
              </div>
            )}
          </>
        )}
      </Card>

      <Card
        title="Breakdown"
        flush
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
            Export CSV
          </Button>
        }
      >
        {report.isPending ? (
          <Spinner />
        ) : (
          <Table
            columns={columns}
            rows={report.data?.rows ?? []}
            rowKey={(row, index) => row.key ?? `row-${index}`}
            footer={report.data?.totals}
            empty="No usage in this period."
            caption={`Your spend for ${report.data?.period.label ?? "the period"}, ${headingFor(
              groupBy,
            ).toLowerCase()}.`}
          />
        )}
      </Card>
    </div>
  );
}

function headingFor(groupBy: string): string {
  return { model: "Model", api_key: "API key", day: "Day", total: "All" }[groupBy] ?? "Label";
}

/** How much of the spend rests on usage the provider actually reported. */
function measuredPercent(totals: UsageReportRow): number {
  const unmeasured = totals.estimated_requests + totals.unavailable_requests;
  if (totals.requests === 0) return 100;
  return Math.round(((totals.requests - unmeasured) / totals.requests) * 100);
}
