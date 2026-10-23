import { Badge, Card, Input, Notice, Pagination, Select, Spinner, Table } from "@llmp/ui";
import type { Column } from "@llmp/ui";
import { useEffect, useState } from "react";

import { PageHeader } from "../components/PageHeader";
import {
  AUDIT_ACTION_PREFIXES,
  AUDIT_TARGET_TYPES,
  type AuditChange,
  type AuditEvent,
  useAuditLog,
} from "../lib/adminAudit";
import { formatDateTime } from "../lib/format";
import { FILTERS, MUTED, PAGE } from "../lib/layout";
import { m } from "../paraglide/messages.js";

const PAGE_SIZE = 50;

const ACTION_PREFIX_LABELS: Record<string, () => string> = {
  "model.": m["admin_audit.prefix.model"],
  "price.": m["admin_audit.prefix.price"],
  "alias.": m["admin_audit.prefix.alias"],
  "grant.": m["admin_audit.prefix.grant"],
  "provider.": m["admin_audit.prefix.provider"],
  "limit.": m["admin_audit.prefix.limit"],
};

const TARGET_TYPE_LABELS: Record<string, () => string> = {
  model: m["admin_audit.target.model"],
  price: m["admin_audit.target.price"],
  alias: m["admin_audit.target.alias"],
  grant: m["admin_audit.target.grant"],
  provider: m["admin_audit.target.provider"],
  limit: m["admin_audit.target.limit"],
};

function tone(action: string): "ok" | "warn" | "danger" | "neutral" {
  if (action.endsWith(".create") || action.endsWith(".add")) return "ok";
  if (action.endsWith(".delete") || action.endsWith(".remove")) return "danger";
  if (action.endsWith(".update")) return "warn";
  return "neutral";
}

/** A value as the reader should see it: text as text, everything else as JSON. */
function show(value: unknown): string {
  if (value === null || value === undefined || value === "") return "—";
  return typeof value === "string" ? value : JSON.stringify(value);
}

function Changes({ changes }: { changes: Record<string, AuditChange> }) {
  const fields = Object.entries(changes);
  if (fields.length === 0) return <span className={MUTED}>—</span>;
  return (
    <details>
      <summary className="cursor-pointer">{m["admin_audit.changes"]({ count: fields.length })}</summary>
      <dl className="mt-2 flex flex-col gap-1 text-body">
        {fields.map(([field, change]) => (
          <div key={field} className="flex flex-wrap gap-x-2">
            <dt className="font-medium">{field}</dt>
            <dd className="break-all">
              {"before" in change && (
                <span className={MUTED}>
                  {m["admin_audit.before"]()} {show(change.before)}
                </span>
              )}
              {"before" in change && "after" in change && " → "}
              {"after" in change && (
                <span>
                  {m["admin_audit.after"]()} {show(change.after)}
                </span>
              )}
            </dd>
          </div>
        ))}
      </dl>
    </details>
  );
}

export function AdminAudit() {
  const [from, setFrom] = useState("");
  const [to, setTo] = useState("");
  const [actorInput, setActorInput] = useState("");
  const [actor, setActor] = useState("");
  const [action, setAction] = useState("");
  const [targetType, setTargetType] = useState("");
  const [offset, setOffset] = useState(0);

  // The person field is sent once typing pauses, not per keystroke.
  useEffect(() => {
    const timer = setTimeout(() => {
      setActor(actorInput);
      setOffset(0);
    }, 300);
    return () => clearTimeout(timer);
  }, [actorInput]);

  // A changed filter starts again from the first page.
  const filter = (set: (value: string) => void) => (value: string) => {
    set(value);
    setOffset(0);
  };

  const log = useAuditLog({
    from: from || undefined,
    to: to || undefined,
    actor: actor || undefined,
    action: action || undefined,
    targetType: targetType || undefined,
    limit: PAGE_SIZE,
    offset,
  });
  const page = log.data;

  const columns: Column<AuditEvent>[] = [
    {
      key: "time",
      header: m["admin_audit.column.time"](),
      render: (row) => formatDateTime(row.created_at),
    },
    {
      key: "actor",
      header: m["admin_audit.column.actor"](),
      hideBelow: "md",
      render: (row) => row.actor_label || <span className={MUTED}>—</span>,
    },
    {
      key: "action",
      header: m["admin_audit.column.action"](),
      render: (row) => <Badge tone={tone(row.action)}>{row.action}</Badge>,
    },
    {
      key: "target",
      header: m["admin_audit.column.target"](),
      render: (row) => (
        <span>
          {row.target_label}{" "}
          <span className={MUTED}>({(TARGET_TYPE_LABELS[row.target_type] ?? (() => row.target_type))()})</span>
        </span>
      ),
    },
    {
      key: "changes",
      header: m["admin_audit.column.changes"](),
      render: (row) => <Changes changes={row.changes} />,
    },
  ];

  return (
    <div className={PAGE}>
      <PageHeader title={m["admin_audit.title"]()} subtitle={m["admin_audit.subtitle"]()} />

      <Card title={m["admin_audit.filters"]()}>
        <div className={FILTERS}>
          <Input
            type="date"
            label={m["admin_audit.filter.from"]()}
            value={from}
            onChange={(e) => filter(setFrom)(e.target.value)}
          />
          <Input
            type="date"
            label={m["admin_audit.filter.to"]()}
            value={to}
            onChange={(e) => filter(setTo)(e.target.value)}
          />
          <Input
            label={m["admin_audit.filter.actor"]()}
            value={actorInput}
            onChange={(e) => setActorInput(e.target.value)}
          />
          <Select
            label={m["admin_audit.filter.action"]()}
            value={action}
            onChange={(e) => filter(setAction)(e.target.value)}
          >
            <option value="">{m["admin_audit.filter.all_actions"]()}</option>
            {AUDIT_ACTION_PREFIXES.map((prefix) => (
              <option key={prefix} value={prefix}>
                {ACTION_PREFIX_LABELS[prefix]?.()}
              </option>
            ))}
          </Select>
          <Select
            label={m["admin_audit.filter.target"]()}
            value={targetType}
            onChange={(e) => filter(setTargetType)(e.target.value)}
          >
            <option value="">{m["admin_audit.filter.all_targets"]()}</option>
            {AUDIT_TARGET_TYPES.map((type) => (
              <option key={type} value={type}>
                {TARGET_TYPE_LABELS[type]?.()}
              </option>
            ))}
          </Select>
        </div>
      </Card>

      <Card flush>
        {log.isPending ? (
          <Spinner label={m["admin_audit.loading"]()} />
        ) : log.error ? (
          <div className="p-5">
            <Notice tone="danger" title={m["admin_audit.error.title"]()}>
              {/* i18n: server text, the API's own error message. */}
              {log.error instanceof Error ? log.error.message : m["admin_audit.error.unknown"]()}
            </Notice>
          </div>
        ) : (
          <>
            <Table
              columns={columns}
              rows={page?.items ?? []}
              rowKey={(row) => row.id}
              empty={m["admin_audit.empty"]()}
              caption={m["admin_audit.caption"]()}
            />
            <Pagination
              total={page?.total ?? 0}
              limit={PAGE_SIZE}
              offset={offset}
              onOffsetChange={setOffset}
              noun={m["admin_audit.noun"]()}
              busy={log.isFetching}
            />
          </>
        )}
      </Card>
    </div>
  );
}
