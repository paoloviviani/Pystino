import { Button, Card, Money, Notice, Select, Spinner, Stat, SummaryStrip, Table } from "@llmp/ui";
import type { Column } from "@llmp/ui";
import { useState } from "react";
import { downloadCsv } from "../lib/api";
import { reportQueryString, useAdminReport, useGroups, useModels } from "../lib/admin";
import { FILTERS, MUTED, PAGE, STATS } from "../lib/layout";
import { recentPeriods } from "../lib/periods";
import type { UsageReportRow } from "../lib/types";
import { formatNumber } from "../lib/format";
import { m } from "../paraglide/messages.js";
import { PageHeader } from "../components/PageHeader";
import { Disclosures } from "../components/Disclosures";
import { UsagePlots } from "../components/UsagePlots";

/** Message functions, not strings: this list is built once, at import. */
const BREAKDOWNS = [
  { value: "group", label: m["admin_reports.breakdown.group"] },
  { value: "user", label: m["admin_reports.breakdown.user"] },
  { value: "model", label: m["admin_reports.breakdown.model"] },
  { value: "provider", label: m["admin_reports.breakdown.provider"] },
  { value: "api_key", label: m["admin_reports.breakdown.api_key"] },
  { value: "day", label: m["admin_reports.breakdown.day"] },
  { value: "total", label: m["admin_reports.breakdown.total"] },
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
      header: m["admin_reports.column.requests"](),
      numeric: true,
      render: (row) => formatNumber(row.requests),
    },
    {
      key: "tokens",
      header: m["admin_reports.column.tokens"](),
      numeric: true,
      render: (row) => formatNumber(row.total_tokens),
    },
    {
      key: "images",
      header: m["admin_reports.column.images"](),
      numeric: true,
      // A dash, not a zero: most rows are not image rows, and a column of
      // zeroes reads as "we generated no images" rather than "not applicable".
      render: (row) =>
        row.images ? formatNumber(row.images) : <span className={MUTED}>—</span>,
    },
    {
      key: "cost",
      header: m["admin_reports.column.cost"](),
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
      <PageHeader title={m["admin_reports.title"]()} subtitle={m["admin_reports.subtitle"]()} />

      <Card title={m["admin_reports.filters"]()}>
        <div className={FILTERS}>
          <Select
            label={m["admin_reports.filter.period"]()}
            value={period}
            onChange={(e) => setPeriod(e.target.value)}
          >
            {/* i18n: server text — the period labels are built by lib/periods.ts,
                which is outside the conversion; they are names and dates, not prose. */}
            {periods.map((option) => (
              <option key={option.value} value={option.value}>
                {option.label}
              </option>
            ))}
          </Select>

          <Select
            label={m["admin_reports.filter.breakdown"]()}
            value={groupBy}
            onChange={(e) => setGroupBy(e.target.value)}
          >
            {BREAKDOWNS.map((option) => (
              <option key={option.value} value={option.value}>
                {option.label()}
              </option>
            ))}
          </Select>

          <Select
            label={m["admin_reports.filter.group"]()}
            value={groupId}
            onChange={(e) => setGroupId(e.target.value)}
          >
            <option value="">{m["admin_reports.filter.all_groups"]()}</option>
            {(groups.data?.items ?? []).map((group) => (
              <option key={group.id} value={group.id}>
                {group.name}
              </option>
            ))}
          </Select>

          <Select
            label={m["admin_reports.filter.model"]()}
            value={model}
            onChange={(e) => setModel(e.target.value)}
          >
            <option value="">{m["admin_reports.filter.all_models"]()}</option>
            {(models.data?.items ?? []).map((entry) => (
              <option key={entry.id} value={entry.name}>
                {entry.name}
              </option>
            ))}
          </Select>
        </div>
      </Card>

      <Card>
        {report.isPending && <Spinner label={m["admin_reports.running"]()} />}
        {report.error ? (
          <Notice tone="danger" title={m["admin_reports.error.title"]()}>
            {/* i18n: server text — the API's own error message. */}
            {report.error instanceof Error ? report.error.message : m["admin_reports.error.unknown"]()}
          </Notice>
        ) : null}
        {report.data && (
          <>
            <div className={STATS}>
              <Stat
                label={m["admin_reports.stat.spend"]({ period: report.data.period.label })}
                value={<Money amount={report.data.totals.cost} currency={currency} />}
                detail={m["admin_reports.stat.period_detail"]({ timezone: report.data.period.timezone })}
              />
              <Stat
                label={m["admin_reports.stat.requests"]()}
                value={formatNumber(report.data.totals.requests)}
                detail={
                  report.data.totals.images
                    ? m["admin_reports.stat.tokens_and_images_detail"]({
                        tokens: report.data.totals.total_tokens,
                        images: report.data.totals.images,
                      })
                    : m["admin_reports.stat.tokens_detail"]({ tokens: report.data.totals.total_tokens })
                }
              />
              <Stat
                label={m["admin_reports.stat.measured"]()}
                value={m["admin_reports.stat.measured_value"]({
                  percent: measuredPercent(report.data.totals),
                })}
                detail={
                  report.data.totals.estimated_requests + report.data.totals.unavailable_requests >
                  0
                    ? m["admin_reports.stat.measured_detail.incomplete"]()
                    : m["admin_reports.stat.measured_detail.complete"]()
                }
                tone={measuredPercent(report.data.totals) < 100 ? "warn" : "ok"}
              />
            </div>

            {/* The caveats collapse to one line; the component keeps the
                API's wording verbatim and remembers the choice per page. */}
            <Disclosures
              notes={report.data.disclosures}
              storageKey="llmp.console.disclosures.adminReports"
            />
          </>
        )}
      </Card>

      <UsagePlots
        splits={["group", "user", "model", "provider"]}
        useChartReport={(splitBy) =>
          useAdminReport({
            period,
            groupBy: "day",
            splitBy,
            groupId: groupId || undefined,
            model: model || undefined,
          })
        }
      />

      <Card title={m["admin_reports.breakdown_card"]()} flush>
        {report.isPending ? (
          <Spinner />
        ) : (
          <>
            {report.data && (
              <div className="p-5">
                <SummaryStrip
                  headline={m["admin_reports.row_count"]({ count: report.data.rows.length })}
                  detail={m["admin_reports.summary_detail"]({
                    period: report.data.period.label,
                    breakdown: headingLowerFor(groupBy),
                  })}
                  active={report.data.rows.length > 0}
                  actions={
                    <Button
                      onClick={() => downloadCsv(`/api/admin/reports/usage.csv?${reportQueryString(query)}`)}
                    >
                      {m["admin_reports.export_csv"]()}
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
              empty={m["admin_reports.empty"]()}
              caption={m["admin_reports.caption"]({
                period: report.data?.period.label ?? m["admin_reports.caption_period_fallback"](),
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
      group: m["admin_reports.heading.group"](),
      user: m["admin_reports.heading.user"](),
      model: m["admin_reports.heading.model"](),
      provider: m["admin_reports.heading.provider"](),
      api_key: m["admin_reports.heading.api_key"](),
      day: m["admin_reports.heading.day"](),
      total: m["admin_reports.heading.total"](),
    }[groupBy] ?? m["admin_reports.heading.other"]()
  );
}

/**
 * The same nouns, in the case the sentences need them in.
 *
 * Two sets of messages rather than `.toLowerCase()` on one: lower-casing a
 * translated word is a rule about English, and in a language with more than two
 * cases the sentence wants the form that reads correctly in it rather than the
 * one that came out of a table header.
 */
function headingLowerFor(groupBy: string): string {
  return (
    {
      group: m["admin_reports.heading_lower.group"](),
      user: m["admin_reports.heading_lower.user"](),
      model: m["admin_reports.heading_lower.model"](),
      provider: m["admin_reports.heading_lower.provider"](),
      api_key: m["admin_reports.heading_lower.api_key"](),
      day: m["admin_reports.heading_lower.day"](),
      total: m["admin_reports.heading_lower.total"](),
    }[groupBy] ?? m["admin_reports.heading_lower.other"]()
  );
}

/** Share of requests whose usage the provider reported exactly. */
function measuredPercent(totals: UsageReportRow): number {
  if (totals.requests === 0) return 100;
  const inexact = totals.estimated_requests + totals.unavailable_requests;
  return Math.round(((totals.requests - inexact) / totals.requests) * 100);
}
