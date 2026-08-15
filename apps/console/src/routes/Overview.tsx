import { Badge, Button, Card, Money, Notice, Select, Spinner, Stat, Table } from "@llmp/ui";
import type { Column } from "@llmp/ui";
import { useState } from "react";
import { downloadCsv } from "../lib/api";
import { useMyKeys, useMyReport } from "../lib/queries";
import type { ApiKey, Me, UsageReportRow } from "../lib/types";
import { recentPeriods } from "../lib/periods";
import styles from "./Overview.module.css";

export interface OverviewProps {
  me: Me;
}

const BREAKDOWNS = [
  { value: "model", label: "By model" },
  { value: "day", label: "By day" },
  { value: "group", label: "By group" },
  { value: "api_key", label: "By API key" },
];

export function Overview({ me }: OverviewProps) {
  const periods = recentPeriods();
  const [period, setPeriod] = useState(periods[0]?.value ?? "");
  const [groupBy, setGroupBy] = useState("model");

  const report = useMyReport(period, groupBy);
  const keys = useMyKeys();

  const columns: Column<UsageReportRow>[] = [
    { key: "label", header: breakdownNoun(groupBy), render: (row) => row.label },
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
      key: "cost",
      header: "Spend",
      numeric: true,
      render: (row) => <Money amount={row.cost} currency={report.data?.currency ?? "EUR"} />,
    },
  ];

  return (
    <div className={styles.page}>
      <div className={styles.pageHeader}>
        <div>
          <h1 className={styles.title}>Your usage</h1>
          <p className={styles.subtitle}>
            What you have spent, and the keys you spent it with. Only your own
            activity is shown here.
          </p>
        </div>
        <Select
          label="Period"
          hideLabel
          value={period}
          onChange={(event) => setPeriod(event.target.value)}
        >
          {periods.map((option) => (
            <option key={option.value} value={option.value}>
              {option.label}
            </option>
          ))}
        </Select>
      </div>

      <Card>
        {report.isPending && <Spinner label="Loading your usage" />}
        {report.error ? (
          <Notice tone="danger" title="Could not load your usage">
            {report.error instanceof Error ? report.error.message : "Unknown error."}
          </Notice>
        ) : null}
        {report.data && (
          <>
            <div className={styles.stats}>
              <Stat
                label={`Spend · ${report.data.period.label}`}
                value={
                  <Money amount={report.data.totals.cost} currency={report.data.currency} />
                }
                detail={`${report.data.period.timezone} calendar period`}
              />
              <Stat
                label="Requests"
                value={report.data.totals.requests.toLocaleString()}
                detail={`${report.data.totals.total_tokens.toLocaleString()} tokens`}
              />
              <Stat
                label="Groups"
                value={me.groups.length.toLocaleString()}
                detail={
                  me.default_billing_group
                    ? `Billing to ${me.default_billing_group.name}`
                    : "No default billing group"
                }
              />
            </div>

            {/* The API's own wording, rendered verbatim. Restating a caveat in
                the UI is how the two end up disagreeing about the number. */}
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
          <>
            <Select
              label="Breakdown"
              hideLabel
              value={groupBy}
              onChange={(event) => setGroupBy(event.target.value)}
            >
              {BREAKDOWNS.map((option) => (
                <option key={option.value} value={option.value}>
                  {option.label}
                </option>
              ))}
            </Select>
            <Button
              onClick={() =>
                downloadCsv(
                  `/api/me/reports/usage.csv?period=${encodeURIComponent(period)}` +
                    `&group_by=${encodeURIComponent(groupBy)}`,
                )
              }
            >
              Export CSV
            </Button>
          </>
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
            caption={`Your spend ${describePeriod(report.data?.period.label)}, ${labelFor(groupBy)}.`}
          />
        )}
      </Card>

      <Card title="API keys" flush description="Keys you have minted. The secret is shown once, at creation.">
        {keys.isPending ? (
          <Spinner />
        ) : (
          <Table
            columns={keyColumns}
            rows={keys.data ?? []}
            rowKey={(key) => key.id}
            empty="You have not minted any API keys."
          />
        )}
      </Card>
    </div>
  );
}

const keyColumns: Column<ApiKey>[] = [
  { key: "name", header: "Name", render: (key) => key.name || <em>unnamed</em> },
  {
    key: "prefix",
    header: "Prefix",
    render: (key) => <code className={styles.code}>{key.prefix}</code>,
  },
  {
    key: "group",
    header: "Bills to",
    render: (key) => key.billing_group?.name ?? "default group at request time",
  },
  {
    key: "status",
    header: "Status",
    render: (key) =>
      key.revoked_at ? (
        <Badge tone="danger">Revoked</Badge>
      ) : isExpired(key) ? (
        <Badge tone="warn">Expired</Badge>
      ) : (
        <Badge tone="ok">Active</Badge>
      ),
  },
  {
    key: "used",
    header: "Last used",
    render: (key) => (key.last_used_at ? formatDate(key.last_used_at) : "never"),
  },
];

function isExpired(key: ApiKey): boolean {
  return key.expires_at !== null && new Date(key.expires_at) < new Date();
}

function formatDate(iso: string): string {
  return new Date(iso).toLocaleDateString(undefined, {
    year: "numeric",
    month: "short",
    day: "numeric",
  });
}

function breakdownNoun(groupBy: string): string {
  return { model: "Model", day: "Day", group: "Group", api_key: "API key" }[groupBy] ?? "Item";
}

function labelFor(groupBy: string): string {
  return BREAKDOWNS.find((option) => option.value === groupBy)?.label.toLowerCase() ?? groupBy;
}

function describePeriod(label: string | undefined): string {
  return label ? `for ${label}` : "";
}
