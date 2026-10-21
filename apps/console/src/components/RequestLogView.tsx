/**
 * The request log, for the administrator and for one person.
 *
 * One component for both so the two cannot drift: the only differences are
 * the endpoint, and that the administrator can narrow by person. The person's
 * own view has no user filter, and the server pins the user regardless.
 */

import { Badge, Button, Card, Money, Notice, Pagination, Select, Spinner, SummaryStrip, Table } from "@llmp/ui";
import type { Column } from "@llmp/ui";
import { useState } from "react";

import { PageHeader } from "./PageHeader";
import { useGroups, useModels, useUsers } from "../lib/admin";
import { downloadCsv } from "../lib/api";
import { formatDateTime, formatNumber } from "../lib/format";
import { FILTERS, MUTED, PAGE } from "../lib/layout";
import { recentPeriods } from "../lib/periods";
import {
  REQUEST_STATUSES,
  type RequestLogRow,
  type RequestLogScope,
  requestLogCsvPath,
  useRequestLog,
} from "../lib/requestLog";
import { m } from "../paraglide/messages.js";

const PAGE_SIZE = 50;

const STATUS_LABELS: Record<string, () => string> = {
  completed: m["request_log.status.completed"],
  blocked: m["request_log.status.blocked"],
  upstream_error: m["request_log.status.upstream_error"],
  client_disconnected: m["request_log.status.client_disconnected"],
  in_progress: m["request_log.status.in_progress"],
};

function statusTone(status: string): "ok" | "warn" | "danger" | "neutral" {
  if (status === "completed") return "ok";
  if (status === "upstream_error") return "danger";
  if (status === "blocked") return "warn";
  return "neutral";
}

export function RequestLogView({ scope }: { scope: RequestLogScope }) {
  const admin = scope === "admin";
  const periods = recentPeriods();
  const [period, setPeriod] = useState(periods[0]?.value ?? "");
  const [userId, setUserId] = useState("");
  const [groupId, setGroupId] = useState("");
  const [model, setModel] = useState("");
  const [status, setStatus] = useState("");
  const [offset, setOffset] = useState(0);

  // A changed filter starts again from the first page: an operator on page
  // three who narrows the filter would otherwise see an empty table.
  const filter = (set: (value: string) => void) => (value: string) => {
    set(value);
    setOffset(0);
  };

  const query = {
    period,
    userId: admin ? userId || undefined : undefined,
    groupId: groupId || undefined,
    model: model || undefined,
    status: status || undefined,
    limit: PAGE_SIZE,
    offset,
  };
  const log = useRequestLog(scope, query);
  const users = useUsers({ limit: 200 }, admin);
  const groups = useGroups();
  const models = useModels();
  const page = log.data;

  const columns: Column<RequestLogRow>[] = [
    {
      key: "time",
      header: m["request_log.column.time"](),
      render: (row) => formatDateTime(row.created_at),
    },
    ...(admin
      ? [
          {
            key: "user",
            header: m["request_log.column.user"](),
            hideBelow: "md" as const,
            render: (row: RequestLogRow) => row.user_label ?? <span className={MUTED}>—</span>,
          },
        ]
      : []),
    {
      key: "group",
      header: m["request_log.column.group"](),
      hideBelow: "lg",
      render: (row) => row.group_name ?? <span className={MUTED}>—</span>,
    },
    {
      key: "model",
      header: m["request_log.column.model"](),
      render: (row) =>
        row.requested_model ? (
          <span>
            {row.requested_model} <span className={MUTED}>→ {row.model}</span>
            {/* Which try this was: a request that fell back has one line per
                attempt under one request id, and the failed ones read as errors. */}
            {row.route_attempt ? (
              <>
                {" "}
                <Badge>{m["request_log.attempt"]({ n: row.route_attempt })}</Badge>
              </>
            ) : null}
          </span>
        ) : (
          row.model
        ),
    },
    {
      key: "tokens",
      header: m["request_log.column.tokens"](),
      numeric: true,
      hideBelow: "sm",
      render: (row) =>
        m["request_log.tokens_cell"]({
          prompt: formatNumber(row.prompt_tokens),
          completion: formatNumber(row.completion_tokens),
          cached: formatNumber(row.cached_prompt_tokens),
        }),
    },
    {
      key: "cost",
      header: m["request_log.column.cost"](),
      numeric: true,
      render: (row) => <Money amount={row.cost} currency={row.currency} />,
    },
    {
      key: "latency",
      header: m["request_log.column.latency"](),
      numeric: true,
      hideBelow: "lg",
      render: (row) =>
        row.latency_ms === null ? (
          <span className={MUTED}>—</span>
        ) : (
          m["request_log.latency_cell"]({ ms: formatNumber(row.latency_ms) })
        ),
    },
    {
      key: "status",
      header: m["request_log.column.status"](),
      render: (row) => (
        <Badge tone={statusTone(row.status)}>
          {(STATUS_LABELS[row.status] ?? m["request_log.status.other"])()}
        </Badge>
      ),
    },
    {
      key: "source",
      header: m["request_log.column.source"](),
      hideBelow: "md",
      render: (row) =>
        row.source === "api_key" ? m["request_log.source.api_key"]() : m["request_log.source.token"](),
    },
  ];

  return (
    <div className={PAGE}>
      <PageHeader
        title={admin ? m["request_log.admin.title"]() : m["request_log.me.title"]()}
        subtitle={admin ? m["request_log.admin.subtitle"]() : m["request_log.me.subtitle"]()}
      />

      <Card title={m["request_log.filters"]()}>
        <div className={FILTERS}>
          <Select
            label={m["request_log.filter.period"]()}
            value={period}
            onChange={(e) => filter(setPeriod)(e.target.value)}
          >
            {/* i18n: server text: period labels are built by lib/periods.ts. */}
            {periods.map((option) => (
              <option key={option.value} value={option.value}>
                {option.label}
              </option>
            ))}
          </Select>

          {admin && (
            <Select
              label={m["request_log.filter.user"]()}
              value={userId}
              onChange={(e) => filter(setUserId)(e.target.value)}
            >
              <option value="">{m["request_log.filter.all_users"]()}</option>
              {(users.data?.items ?? []).map((user) => (
                <option key={user.id} value={user.id}>
                  {user.email ?? user.display_name ?? user.id}
                </option>
              ))}
            </Select>
          )}

          <Select
            label={m["request_log.filter.group"]()}
            value={groupId}
            onChange={(e) => filter(setGroupId)(e.target.value)}
          >
            <option value="">{m["request_log.filter.all_groups"]()}</option>
            {(groups.data?.items ?? []).map((group) => (
              <option key={group.id} value={group.id}>
                {group.name}
              </option>
            ))}
          </Select>

          <Select
            label={m["request_log.filter.model"]()}
            value={model}
            onChange={(e) => filter(setModel)(e.target.value)}
          >
            <option value="">{m["request_log.filter.all_models"]()}</option>
            {(models.data?.items ?? []).map((entry) => (
              <option key={entry.id} value={entry.name}>
                {entry.name}
              </option>
            ))}
          </Select>

          <Select
            label={m["request_log.filter.status"]()}
            value={status}
            onChange={(e) => filter(setStatus)(e.target.value)}
          >
            <option value="">{m["request_log.filter.all_statuses"]()}</option>
            {REQUEST_STATUSES.map((value) => (
              <option key={value} value={value}>
                {STATUS_LABELS[value]?.()}
              </option>
            ))}
          </Select>
        </div>
      </Card>

      <Card flush>
        {log.isPending ? (
          <Spinner label={m["request_log.loading"]()} />
        ) : log.error ? (
          <div className="p-5">
            <Notice tone="danger" title={m["request_log.error.title"]()}>
              {/* i18n: server text, the API's own error message. */}
              {log.error instanceof Error ? log.error.message : m["request_log.error.unknown"]()}
            </Notice>
          </div>
        ) : (
          <>
            <div className="p-5">
              <SummaryStrip
                headline={m["request_log.row_count"]({ count: page?.total ?? 0 })}
                detail={m["request_log.no_content"]()}
                active={(page?.total ?? 0) > 0}
                actions={
                  <Button onClick={() => downloadCsv(requestLogCsvPath(scope, query))}>
                    {m["request_log.export_csv"]()}
                  </Button>
                }
              />
            </div>
            <Table
              columns={columns}
              rows={page?.items ?? []}
              rowKey={(row, index) => `${row.request_id}-${index}`}
              empty={m["request_log.empty"]()}
              caption={m["request_log.caption"]()}
            />
            <Pagination
              total={page?.total ?? 0}
              limit={PAGE_SIZE}
              offset={offset}
              onOffsetChange={setOffset}
              noun={m["request_log.noun"]()}
              busy={log.isFetching}
            />
          </>
        )}
      </Card>
    </div>
  );
}
