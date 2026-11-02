import { Badge, Button, Card, Dialog, Input, Meter, Notice, Pagination, Spinner, Table } from "@llmp/ui";
import type { Column } from "@llmp/ui";
import { formatMoney, useExactMoney } from "@llmp/ui";
import { useState } from "react";
import { Link, useParams } from "react-router";
import { PageHeader } from "../components/PageHeader";
import { useGroups, useLimits, useQuotaUsage, useResetLimit, useUserQuotaUsage } from "../lib/admin";
import { formatDateTime, formatNumber } from "../lib/format";
import { MUTED, PAGE } from "../lib/layout";
import { usePaginated } from "../lib/paging";
import { useOptionalToast } from "../lib/toast";
import type { LimitRule, UserQuotaEntry, UserQuotaSummary } from "../lib/types";
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
      <SharedQuotas />
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
  const [resetting, setResetting] = useState<ResetTarget | null>(null);
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
      // Every row that can be reset is reset from here. A refused row (the user
      // is held to their own rule instead) has no button: its usage cell says why.
      key: "actions",
      header: "",
      render: (entry) => {
        if (entry.replaced_by_rule_id) return null;
        const target = (mode: ResetTarget["mode"]): ResetTarget => ({
          ruleId: entry.rule_id,
          ruleName: entry.rule_name,
          mode,
          userId,
          userName: name,
          sharedWith: entry.scope === "group" ? entry.scope_label : null,
        });
        if (entry.kind === "own") {
          return (
            <Button onClick={() => setResetting(target("own"))}>
              {m["quota_usage.action.reset_own"]()}
            </Button>
          );
        }
        if (entry.can_reset_for_user) {
          return (
            <Button onClick={() => setResetting(target("user"))}>
              {m["quota_usage.action.reset"]()}
            </Button>
          );
        }
        return (
          <>
            <Button onClick={() => setResetting(target("shared"))}>
              {m["quota_usage.action.reset_shared"]()}
            </Button>
            <div className={MUTED}>{sharedWho(entry.scope_label)}</div>
          </>
        );
      },
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
      <ResetDialog target={resetting} onClose={() => setResetting(null)} />
    </div>
  );
}

/** What a reset is applied to; the one dialog below serves every kind of row. */
interface ResetTarget {
  ruleId: string;
  ruleName: string;
  /** "user": one user's count of a per-user rule (sends user_id). "own": a
   *  rule scoped to that user, reset whole. "shared": a total, reset for
   *  everyone it counts. */
  mode: "user" | "own" | "shared";
  userId?: string;
  userName?: string;
  /** The group a shared quota counts, or null for the whole deployment. */
  sharedWith?: string | null;
}

/** Everyone a shared quota counts, named. */
function sharedWho(group: string | null | undefined): string {
  return group
    ? m["quota_usage.reset.scope_group"]({ group })
    : m["quota_usage.reset.scope_all"]();
}

/**
 * The single place a quota is reset: confirmed, with an optional reason, and
 * plain about whose counter moves. The Quotas page links here and no longer
 * resets anything itself.
 */
function ResetDialog({ target, onClose }: { target: ResetTarget | null; onClose: () => void }) {
  const reset = useResetLimit();
  const toast = useOptionalToast();
  const [reason, setReason] = useState("");
  const mode = target?.mode ?? "user";

  const submit = () => {
    if (!target) return;
    reset.mutate(
      { id: target.ruleId, reason, userId: mode === "user" ? target.userId : undefined },
      {
        onSuccess: () => {
          toast?.add({
            title:
              mode === "user"
                ? m["quota_usage.reset.toast.done"]()
                : m["quota_usage.reset.toast.done_rule"](),
            type: "success",
          });
          setReason("");
          onClose();
        },
        onError: () => toast?.add({ title: m["quota_usage.reset.toast.failed"](), type: "error" }),
      },
    );
  };

  const title =
    mode === "user"
      ? m["quota_usage.reset.title"]({ rule: target?.ruleName ?? "", user: target?.userName ?? "" })
      : mode === "own"
        ? m["quota_usage.reset.title_own"]({ rule: target?.ruleName ?? "" })
        : m["quota_usage.reset.title_shared"]({ rule: target?.ruleName ?? "" });
  const noticeTitle =
    mode === "user"
      ? m["quota_usage.reset.notice_title"]()
      : mode === "own"
        ? m["quota_usage.reset.notice_title_own"]()
        : m["quota_usage.reset.notice_title_shared"]();
  const noticeBody =
    mode === "user"
      ? m["quota_usage.reset.notice_body"]()
      : mode === "own"
        ? m["quota_usage.reset.notice_body_own"]({ user: target?.userName ?? "" })
        : m["quota_usage.reset.notice_body_shared"]({ who: sharedWho(target?.sharedWith) });
  const submitLabel =
    mode === "user"
      ? m["quota_usage.reset.submit"]()
      : mode === "own"
        ? m["quota_usage.reset.submit_own"]()
        : m["quota_usage.reset.submit_shared"]();

  return (
    <Dialog
      open={target !== null}
      title={title}
      onClose={onClose}
      footer={
        <>
          <Button onClick={onClose}>{m["quota_usage.reset.cancel"]()}</Button>
          <Button variant="primary" busy={reset.isPending} onClick={submit}>
            {submitLabel}
          </Button>
        </>
      }
    >
      <Notice tone={mode === "shared" ? "warn" : "info"} title={noticeTitle}>
        {noticeBody}
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

/**
 * The shared quotas (a group's total, the deployment's total), each with its
 * whole-quota reset. A shared quota is reachable from every user it counts, but
 * not from a group with no members or a global total on a deployment with no
 * users yet, so this list makes every rule reachable from Quota usage.
 */
function SharedQuotas() {
  const limits = useLimits();
  const groups = useGroups();
  const [resetting, setResetting] = useState<ResetTarget | null>(null);
  const groupName = (id: string | null) =>
    (groups.data?.items ?? []).find((group) => group.id === id)?.name ?? null;
  const shared = (limits.data?.items ?? []).filter(
    (rule: LimitRule) =>
      rule.is_active &&
      rule.applies_to === "aggregate" &&
      (rule.scope === "global" || rule.scope === "group"),
  );
  if (shared.length === 0) return null;

  const columns: Column<LimitRule>[] = [
    { key: "rule", header: m["quota_usage.column.rule"](), render: (rule) => rule.name },
    {
      key: "counts",
      header: m["quota_usage.shared_list.counts"](),
      render: (rule) => sharedWho(rule.scope === "group" ? groupName(rule.scope_id) : null),
    },
    {
      key: "actions",
      header: "",
      render: (rule) => (
        <Button
          onClick={() =>
            setResetting({
              ruleId: rule.id,
              ruleName: rule.name,
              mode: "shared",
              sharedWith: rule.scope === "group" ? groupName(rule.scope_id) : null,
            })
          }
        >
          {m["quota_usage.action.reset_shared"]()}
        </Button>
      ),
    },
  ];

  return (
    <Card title={m["quota_usage.shared_list.title"]()} description={m["quota_usage.shared_list.hint"]()} flush>
      <Table
        columns={columns}
        rows={shared}
        rowKey={(rule) => rule.id}
        empty=""
        caption={m["quota_usage.shared_list.title"]()}
      />
      <ResetDialog target={resetting} onClose={() => setResetting(null)} />
    </Card>
  );
}
