import { Button, Card, Money, Notice, Select, Spinner, Stat, Table } from "@llmp/ui";
import type { Column } from "@llmp/ui";
import { useState } from "react";
import { downloadCsv } from "../lib/api";
import { reportQueryString, useAdminReport, useGroups, useModels } from "../lib/admin";
import { recentPeriods } from "../lib/periods";
import type { UsageReportRow } from "../lib/types";
import { PageHeader } from "../components/PageHeader";
import styles from "./Admin.module.css";

const BREAKDOWNS = [
  { value: "group", label: "By group" },
  { value: "user", label: "By user" },
  { value: "model", label: "By model" },
  { value: "api_key", label: "By API key" },
  { value: "day", label: "By day" },
  { value: "total", label: "Total only" },
];

export function AdminReports() {
  const periods = recentPeriods();
  const [period, setPeriod] = useState(periods[0]?.value ?? "");
  const [groupBy, setGroupBy] = useState("group");
  const [groupId, setGroupId] = useState("");
  const [model, setModel] = useState("");

  const query = { period, groupBy, groupId: groupId || undefined, model: model || undefined };
  const report = useAdminReport(query);
  const groups = useGroups();
  const models = useModels();

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
      // zeroes reads as "we generated no images" rather than "not applicable".
      render: (row) =>
        row.images ? row.images.toLocaleString() : <span className={styles.muted}>—</span>,
    },
    {
      key: "cost",
      header: "Spend",
      numeric: true,
      render: (row) => <Money amount={row.cost} currency={currency} />,
    },
  ];

  return (
    <div className={styles.page}>
      <PageHeader
        title="Reports"
        subtitle="Spend over a calendar period, for chargeback."
      />

      <Card title="Filters">
        <div className={styles.filters}>
          <Select label="Period" value={period} onChange={(e) => setPeriod(e.target.value)}>
            {periods.map((option) => (
              <option key={option.value} value={option.value}>
                {option.label}
              </option>
            ))}
          </Select>

          <Select label="Breakdown" value={groupBy} onChange={(e) => setGroupBy(e.target.value)}>
            {BREAKDOWNS.map((option) => (
              <option key={option.value} value={option.value}>
                {option.label}
              </option>
            ))}
          </Select>

          <Select label="Group" value={groupId} onChange={(e) => setGroupId(e.target.value)}>
            <option value="">All groups</option>
            {(groups.data?.items ?? []).map((group) => (
              <option key={group.id} value={group.id}>
                {group.name}
              </option>
            ))}
          </Select>

          <Select label="Model" value={model} onChange={(e) => setModel(e.target.value)}>
            <option value="">All models</option>
            {(models.data?.items ?? []).map((entry) => (
              <option key={entry.id} value={entry.name}>
                {entry.name}
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
            <div className={styles.stats}>
              <Stat
                label={`Spend · ${report.data.period.label}`}
                value={<Money amount={report.data.totals.cost} currency={currency} />}
                detail={`${report.data.period.timezone} calendar period`}
              />
              <Stat
                label="Requests"
                value={report.data.totals.requests.toLocaleString()}
                detail={
                  report.data.totals.images
                    ? `${report.data.totals.total_tokens.toLocaleString()} tokens, ` +
                      `${report.data.totals.images.toLocaleString()} images`
                    : `${report.data.totals.total_tokens.toLocaleString()} tokens`
                }
              />
              <Stat
                label="Measured"
                value={`${measuredPercent(report.data.totals)}%`}
                detail={
                  report.data.totals.estimated_requests + report.data.totals.unavailable_requests >
                  0
                    ? "the rest is estimated or missing"
                    : "exact usage from the provider"
                }
                tone={measuredPercent(report.data.totals) < 100 ? "warn" : "ok"}
              />
            </div>

            {/* The API's own words. Restating a caveat here is how the two end
                up disagreeing about what the number means. */}
            {report.data.disclosures.length > 0 && (
              <div className={styles.disclosures}>
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
            onClick={() => downloadCsv(`/api/admin/reports/usage.csv?${reportQueryString(query)}`)}
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
            caption={`Spend for ${report.data?.period.label ?? "the period"}, ${headingFor(
              groupBy,
            ).toLowerCase()}.`}
          />
        )}
      </Card>
    </div>
  );
}

function headingFor(groupBy: string): string {
  return (
    { group: "Group", user: "User", model: "Model", api_key: "API key", day: "Day", total: "All" }[
      groupBy
    ] ?? "Item"
  );
}

/** Share of requests whose usage the provider reported exactly. */
function measuredPercent(totals: UsageReportRow): number {
  if (totals.requests === 0) return 100;
  const inexact = totals.estimated_requests + totals.unavailable_requests;
  return Math.round(((totals.requests - inexact) / totals.requests) * 100);
}
