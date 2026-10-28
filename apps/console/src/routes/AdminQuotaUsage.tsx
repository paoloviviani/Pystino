import { Badge, Button, Card, Dialog, Input, Meter, Notice, Pagination, Spinner, Table } from "@llmp/ui";
import type { Column } from "@llmp/ui";
import { formatMoney, useExactMoney } from "@llmp/ui";
import { useState } from "react";
import { Link, useParams } from "react-router";
import { PageHeader } from "../components/PageHeader";
import { useQuotaUsage, useResetLimit, useUserQuotaUsage } from "../lib/admin";
import { formatDateTime, formatNumber } from "../lib/format";
import { MUTED, PAGE } from "../lib/layout";
import { usePaginated } from "../lib/paging";
import { useOptionalToast } from "../lib/toast";
import type { UserQuotaEntry, UserQuotaSummary } from "../lib/types";
import { m } from "../paraglide/messages.js";

/**
 * Each user's quota consumption (the page) and one user's quotas (the detail).
 *
 * The figures come from the gateway's engine with the user as the subject, so a
 * reset (whole or this user's own) is already in them: what is shown is what the
 * user's next request is judged against. An absent figure is "counter
 * unavailable", never zero.
 */
export function AdminQuotaUsage() {
  const { userId = null } = useParams();
  return userId ? <UserDetail userId={userId} /> : <UserList />;
}

function who(user: { email: string | null; display_name: string | null }): string {
  return user.display_name || user.email || m["quota_usage.detail.user_fallback"]();
}

/** The used/limit caption for one entry, money-formatted for a cost metric. */
function useCaption(): (entry: UserQuotaEntry) => string {
  const exact = useExactMoney();
  return (entry) =>
    entry.current_value === null
      ? ""
      : entry.metric === "cost"
        ? m["admin_quotas.consumption.caption"]({
            used: formatMoney(entry.current_value, "EUR", { exact }),
            limit: formatMoney(entry.limit_value, "EUR", { exact }),
          })
        : m["admin_quotas.consumption.caption"]({
            used: formatNumber(Number(entry.current_value)),
            limit: formatNumber(Number(entry.limit_value)),
          });
}

function UsageMeter({ entry }: { entry: UserQuotaEntry }) {
  const caption = useCaption();
  if (entry.current_value === null) {
    return <Badge tone="warn">{m["quota_usage.unavailable"]()}</Badge>;
  }
  return (
    <Meter
      value={Number(entry.current_value)}
      limit={Number(entry.limit_value)}
      label={m["quota_usage.meter.label"]({ name: entry.rule_name })}
      caption={caption(entry)}
    />
  );
}

function UserList() {
  const paged = usePaginated();
  const usage = useQuotaUsage(paged.page);
  const page = usage.data;

  const columns: Column<UserQuotaSummary>[] = [
    {
      key: "user",
      header: m["quota_usage.column.user"](),
      render: (row) => (
        <>
          <div>
            <Link className="underline" to={`/admin/quota-usage/${row.user_id}`}>
              {who(row)}
            </Link>
            {!row.is_active && (
              <>
                {" "}
                <Badge tone="neutral">{m["quota_usage.disabled"]()}</Badge>
              </>
            )}
          </div>
          {row.email && row.display_name && <div className={MUTED}>{row.email}</div>}
        </>
      ),
    },
    {
      key: "constraint",
      header: m["quota_usage.column.constraint"](),
      render: (row) =>
        row.most_constrained ? (
          <>
            <div>{row.most_constrained.rule_name}</div>
            <div className={MUTED}>{row.most_constrained.window_label}</div>
          </>
        ) : (
          <span className={MUTED}>{m["quota_usage.no_rules"]()}</span>
        ),
    },
    {
      key: "usage",
      header: m["quota_usage.column.usage"](),
      render: (row) => (row.most_constrained ? <UsageMeter entry={row.most_constrained} /> : null),
    },
    { key: "rules", header: m["quota_usage.column.rules"](), render: (row) => row.rules },
  ];

  return (
    <div className={PAGE}>
      <PageHeader title={m["quota_usage.title"]()} subtitle={m["quota_usage.subtitle"]()} />
      <Card>
        <Input
          label={m["quota_usage.search"]()}
          value={paged.search}
          onChange={(event) => paged.setSearch(event.target.value)}
          placeholder={m["quota_usage.search_placeholder"]()}
        />
      </Card>
      <Card flush>
        {usage.isPending ? (
          <Spinner label={m["quota_usage.loading"]()} />
        ) : usage.error ? (
          <Notice tone="danger" title={m["quota_usage.load_error"]()}>
            {/* i18n: server text — the API's own error message. */}
            {usage.error instanceof Error ? usage.error.message : m["quota_usage.unknown_error"]()}
          </Notice>
        ) : (
          <>
            <Table
              columns={columns}
              rows={page?.items ?? []}
              rowKey={(row) => row.user_id}
              empty={m["quota_usage.empty"]()}
              caption={m["quota_usage.caption"]()}
            />
            <Pagination
              total={page?.total ?? 0}
              limit={paged.limit}
              offset={paged.offset}
              onOffsetChange={paged.setOffset}
              noun={m["quota_usage.noun"]()}
              busy={usage.isFetching}
            />
          </>
        )}
      </Card>
    </div>
  );
}

function kindLabel(entry: UserQuotaEntry): string {
  switch (entry.kind) {
    case "own":
      return m["quota_usage.kind.own"]();
    case "each_user":
      return m["quota_usage.kind.each_user"]();
    case "each_member":
      return entry.scope_label
        ? m["quota_usage.kind.each_member"]({ group: entry.scope_label })
        : m["quota_usage.kind.each_member_fallback"]();
    default:
      return entry.scope === "group" && entry.scope_label
        ? m["quota_usage.shared.group"]({ group: entry.scope_label })
        : m["quota_usage.shared.global"]();
  }
}

function UserDetail({ userId }: { userId: string }) {
  const usage = useUserQuotaUsage(userId);
  const [resetting, setResetting] = useState<UserQuotaEntry | null>(null);
  const name = usage.data ? who(usage.data) : m["quota_usage.detail.user_fallback"]();

  const columns: Column<UserQuotaEntry>[] = [
    {
      key: "rule",
      header: m["quota_usage.column.rule"](),
      render: (entry) => (
        <>
          <div>{entry.rule_name}</div>
          <div className={MUTED}>
            {entry.kind === "shared" && (
              <>
                <Badge tone="neutral">{m["quota_usage.kind.shared"]()}</Badge>{" "}
              </>
            )}
            {kindLabel(entry)}
          </div>
        </>
      ),
    },
    {
      key: "usage",
      header: m["quota_usage.column.usage"](),
      render: (entry) =>
        entry.replaced_by_rule_id ? (
          <>
            <Badge tone="neutral">
              {m["quota_usage.replaced_by"]({ name: entry.replaced_by_name ?? "" })}
            </Badge>
            <div className={MUTED}>{m["quota_usage.replaced_note"]()}</div>
          </>
        ) : (
          <UsageMeter entry={entry} />
        ),
    },
    {
      key: "window",
      header: m["quota_usage.column.window"](),
      render: (entry) =>
        entry.period ? entry.window_label : m["quota_usage.window.rolling"]({ window: entry.window_label }),
    },
    {
      key: "resets",
      header: m["quota_usage.column.resets"](),
      render: (entry) =>
        entry.replaced_by_rule_id ? null : entry.resets_at ? (
          !entry.resets_at_is_estimate ? (
            m["quota_usage.resets_at"]({ date: formatDateTime(entry.resets_at) })
          ) : (
            m["quota_usage.resets_rolling"]({ date: formatDateTime(entry.resets_at) })
          )
        ) : (
          <span className={MUTED}>{m["quota_usage.resets_none"]()}</span>
        ),
    },
    {
      key: "last_reset",
      header: m["quota_usage.column.last_reset"](),
      render: (entry) =>
        entry.last_reset_at ? (
          <>
            <div>
              {(entry.last_reset_for_user ? m["quota_usage.last_reset.user"] : m["quota_usage.last_reset.whole"])({
                date: formatDateTime(entry.last_reset_at),
              })}
            </div>
            {entry.last_reset_by_email && (
              <div className={MUTED}>{m["quota_usage.last_reset.by"]({ who: entry.last_reset_by_email })}</div>
            )}
          </>
        ) : (
          <span className={MUTED}>{m["quota_usage.last_reset.never"]()}</span>
        ),
    },
    {
      key: "actions",
      header: "",
      render: (entry) =>
        entry.can_reset_for_user ? (
          <Button onClick={() => setResetting(entry)}>{m["quota_usage.action.reset"]()}</Button>
        ) : null,
    },
  ];

  return (
    <div className={PAGE}>
      <PageHeader
        title={m["quota_usage.detail.title"]({ name })}
        subtitle={
          <>
            <Link className="underline" to="/admin/quota-usage">
              {m["quota_usage.detail.back"]()}
            </Link>
            {" · "}
            {m["quota_usage.detail.subtitle"]()}
          </>
        }
      />
      <Card flush>
        {usage.isPending ? (
          <Spinner label={m["quota_usage.loading"]()} />
        ) : usage.error ? (
          <Notice tone="danger" title={m["quota_usage.detail.load_error"]()}>
            {/* i18n: server text — the API's own error message. */}
            {usage.error instanceof Error ? usage.error.message : m["quota_usage.unknown_error"]()}
          </Notice>
        ) : (
          <Table
            columns={columns}
            rows={usage.data?.entries ?? []}
            rowKey={(entry) => entry.rule_id}
            empty={m["quota_usage.detail.empty"]()}
            caption={m["quota_usage.detail.caption"]()}
          />
        )}
      </Card>
      <ResetForUserDialog
        entry={resetting}
        userId={userId}
        userName={name}
        onClose={() => setResetting(null)}
      />
    </div>
  );
}

/** Resetting one user's consumption: confirmed, with an optional reason. */
function ResetForUserDialog({
  entry,
  userId,
  userName,
  onClose,
}: {
  entry: UserQuotaEntry | null;
  userId: string;
  userName: string;
  onClose: () => void;
}) {
  const reset = useResetLimit();
  const toast = useOptionalToast();
  const [reason, setReason] = useState("");

  const submit = () => {
    if (!entry) return;
    reset.mutate(
      { id: entry.rule_id, reason, userId },
      {
        onSuccess: () => {
          toast?.add({ title: m["quota_usage.reset.toast.done"](), type: "success" });
          setReason("");
          onClose();
        },
        onError: () => toast?.add({ title: m["quota_usage.reset.toast.failed"](), type: "error" }),
      },
    );
  };

  return (
    <Dialog
      open={entry !== null}
      title={m["quota_usage.reset.title"]({ rule: entry?.rule_name ?? "", user: userName })}
      onClose={onClose}
      footer={
        <>
          <Button onClick={onClose}>{m["quota_usage.reset.cancel"]()}</Button>
          <Button variant="primary" busy={reset.isPending} onClick={submit}>
            {m["quota_usage.reset.submit"]()}
          </Button>
        </>
      }
    >
      <Notice tone="info" title={m["quota_usage.reset.notice_title"]()}>
        {m["quota_usage.reset.notice_body"]()}
      </Notice>
      {reset.error ? (
        <Notice tone="danger">
          {/* i18n: server text — the API's own error message. */}
          {reset.error instanceof Error ? reset.error.message : m["quota_usage.unknown_error"]()}
        </Notice>
      ) : null}
      <Input
        label={m["quota_usage.reset.reason"]()}
        value={reason}
        onChange={(event) => setReason(event.target.value)}
        placeholder={m["quota_usage.reset.reason_placeholder"]()}
        hint={m["quota_usage.reset.reason_hint"]()}
      />
    </Dialog>
  );
}
