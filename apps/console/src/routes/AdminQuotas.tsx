import {
  Badge,
  Button,
  Card,
  Dialog,
  Input,
  Meter,
  Notice,
  Pagination,
  Select,
  Spinner,
  SummaryStrip,
  Table,
  RowActions,
} from "@llmp/ui";
import type { Column } from "@llmp/ui";
import { formatMoney, useExactMoney } from "@llmp/ui";
import { useState } from "react";
import {
  useCreateLimit,
  useDeleteLimit,
  useGroups,
  useLimits,
  useQuotaHealth,
  useReconcileQuota,
  useResetLimit,
  useResets,
  useUsers,
} from "../lib/admin";
import { unitFor } from "../lib/metrics";
import { formatNumber, formatShortDate, formatDateTime } from "../lib/format";
import { m } from "../paraglide/messages.js";
import { usePaginated } from "../lib/paging";
import type { LimitRule, QuotaWindowHealth } from "../lib/types";
import { PageHeader } from "../components/PageHeader";
import { CHIPS, FORM_ROW, MUTED, NOWRAP, PAGE, ROW_ACTIONS } from "../lib/layout";
import { useOptionalToast } from "../lib/toast";


const PERIODS = ["day", "week", "month", "quarter", "year"];

export function AdminQuotas() {
  const limits = useLimits();
  const groups = useGroups();
  const users = useUsers();
  const remove = useDeleteLimit();
  const toast = useOptionalToast();

  const [creating, setCreating] = useState(false);
  const [resetting, setResetting] = useState<LimitRule | null>(null);
  const [history, setHistory] = useState<LimitRule | null>(null);

  const nameFor = (rule: LimitRule): string => {
    if (rule.scope === "global") return "everyone";
    if (rule.scope === "group") {
      return (
        groups.data?.items.find((group) => group.id === rule.scope_id)?.name ??
        shortId(rule.scope_id)
      );
    }
    if (rule.scope === "user") {
      const user = users.data?.items.find((entry) => entry.id === rule.scope_id);
      return user?.email ?? user?.display_name ?? shortId(rule.scope_id);
    }
    return shortId(rule.scope_id);
  };

  // Both money cells honour the reader's precision preference. `formatMoney` is
  // called for a string here rather than rendered as <Money>, because these
  // figures sit inside a sentence — so the context has to be read by hand.
  const exact = useExactMoney();

  const columns: Column<LimitRule>[] = [
    {
      key: "rule",
      header: m["admin_quotas.column.rule"](),
      render: (rule) => (
        <>
          <div>{rule.name || <em className={MUTED}>{m["admin_quotas.unnamed"]()}</em>}</div>
          <div className={MUTED}>
            {rule.scope} · {nameFor(rule)}
          </div>
        </>
      ),
    },
    {
      key: "limit",
      header: m["admin_quotas.column.limit"](),
      render: (rule) => (
        <>
          <div>
            {rule.metric === "cost"
              ? formatMoney(rule.limit_value, "EUR", { exact })
              : `${formatNumber(Number(rule.limit_value))} ${unitFor(rule.metric)}`}
          </div>
          <div className={`${MUTED} ${NOWRAP}`}>
            {rule.applies_to === "each_member"
              ? m["admin_quotas.window.per_member"]({ window: describeWindow(rule) })
              : rule.applies_to === "each_user"
                ? m["admin_quotas.window.per_user"]({ window: describeWindow(rule) })
                : m["admin_quotas.window.per"]({ window: describeWindow(rule) })}
          </div>
        </>
      ),
    },
    {
      key: "usage",
      header: m["admin_quotas.column.used"](),
      render: (rule) => <Consumption rule={rule} />,
    },
    {
      key: "status",
      header: m["admin_quotas.column.status"](),
      render: (rule) => (
        <div className={CHIPS}>
          {!rule.is_active && <Badge>{m["admin_quotas.badge.inactive"]()}</Badge>}
          {/* The state someone scanning this page is looking for. Without it a
              rule at 200% of its cap still showed a green "Active", which is
              true and useless — the row's meaning is that requests are being
              refused right now. */}
          {rule.is_active && exhausted(rule) && (
            <Badge tone="danger">{m["admin_quotas.badge.over_budget"]()}</Badge>
          )}
          {rule.is_active && !exhausted(rule) && nearLimit(rule) && (
            <Badge tone="warn">{m["admin_quotas.badge.nearly_spent"]()}</Badge>
          )}
          {rule.is_active && !exhausted(rule) && !nearLimit(rule) && (
            <Badge tone="ok">{m["admin_quotas.badge.active"]()}</Badge>
          )}
          {rule.overrides_each_user && (
            <Badge tone="accent">{m["admin_quotas.badge.overrides"]()}</Badge>
          )}
          {rule.last_reset_at && (
            <Badge tone="accent">
              {m["admin_quotas.badge.reset_date"]({ date: formatShortDate(rule.last_reset_at) })}
            </Badge>
          )}
        </div>
      ),
    },
    {
      key: "actions",
      header: "",
      render: (rule) => (
        <div className={ROW_ACTIONS}>
          <RowActions
            label={m["table.row_actions"]({ name: rule.name || rule.scope })}
            actions={[
              {
                key: "history",
                label: m["admin_quotas.action.history"](),
                onSelect: () => setHistory(rule),
              },
              { key: "reset", label: m["admin_quotas.action.reset"](), onSelect: () => setResetting(rule) },
              {
                key: "delete",
                label: m["admin_quotas.action.delete"](),
                onSelect: () =>
                  remove.mutate(rule.id, {
                    onSuccess: () =>
                      toast?.add({ title: m["admin_quotas.toast.deleted"](), type: "success" }),
                    onError: () =>
                      toast?.add({ title: m["admin_quotas.toast.delete_failed"](), type: "error" }),
                  }),
                destructive: true,
                disabled: remove.isPending && remove.variables === rule.id,
              },
            ]}
          />
        </div>
      ),
    },
  ];

  return (
    <div className={PAGE}>
      <PageHeader
        title={m["admin_quotas.title"]()}
        subtitle={m["admin_quotas.subtitle"]()}
        actions={
          <Button variant="primary" onClick={() => setCreating(true)}>
            {m["admin_quotas.new_rule"]()}
          </Button>
        }
      />

      {remove.error ? (
        <Notice tone="danger" title={m["admin_quotas.toast.delete_failed"]()}>
          {/* i18n: server text — the API's own error message. */}
          {remove.error instanceof Error ? remove.error.message : m["admin_quotas.unknown_error"]()}
        </Notice>
      ) : null}

      <Card flush>
        {limits.isPending ? (
          <Spinner label={m["admin_quotas.loading"]()} />
        ) : limits.error ? (
          <Notice tone="danger" title={m["admin_quotas.load_error.title"]()}>
            {/* i18n: server text — the API's own error message. */}
            {limits.error instanceof Error
              ? limits.error.message
              : m["admin_quotas.unknown_error"]()}
          </Notice>
        ) : (
          <>
            {/* Count and state before the rows: how many ceilings, and whether
                any of them is currently refusing requests. No actions — "New
                rule" stays in the page header, and a second one here would be
                the button duplicated rather than relocated. */}
            <div className="p-5">
              <SummaryStrip
                headline={m["admin_quotas.count"]({ count: (limits.data?.items ?? []).length })}
                detail={quotaState(limits.data?.items ?? [])}
                active={(limits.data?.items ?? []).length > 0}
              />
            </div>
            <Table
              columns={columns}
              rows={limits.data?.items ?? []}
              rowKey={(rule) => rule.id}
              empty={m["admin_quotas.empty"]()}
              caption={m["admin_quotas.caption"]()}
            />
          </>
        )}
      </Card>

      <QuotaHealthPanel />

      <CreateRuleDialog open={creating} onClose={() => setCreating(false)} />
      <ResetDialog rule={resetting} onClose={() => setResetting(null)} />
      <HistoryDialog rule={history} onClose={() => setHistory(null)} />
    </div>
  );
}

const ZERO = /^-?0+(\.0+)?$/;

/**
 * Counter against ledger, per rule, with the repair beside it.
 *
 * The ledger is the truth and the counter a cache of it, so a non-zero
 * difference is drift. It is shown as a string comparison, never parsed: money
 * is a string end to end here.
 */
function QuotaHealthPanel() {
  const health = useQuotaHealth();
  const reconcile = useReconcileQuota();
  const toast = useOptionalToast();
  const exact = useExactMoney();
  const users = useUsers();

  const memberName = (memberId: string): string => {
    const user = users.data?.items.find((entry) => entry.id === memberId);
    return user?.email ?? user?.display_name ?? shortId(memberId);
  };

  const rows = Array.isArray(health.data?.windows) ? health.data.windows : [];

  const show = (row: QuotaWindowHealth, value: string | null): string => {
    if (value === null) return m["admin_quotas.health.unavailable"]();
    return row.metric === "cost"
      ? formatMoney(value, "EUR", { exact })
      : formatNumber(Number(value));
  };

  const columns: Column<QuotaWindowHealth>[] = [
    {
      key: "rule",
      header: m["admin_quotas.health.column.rule"](),
      render: (row) => (
        <>
          <div>{row.rule_name || <em className={MUTED}>{m["admin_quotas.unnamed"]()}</em>}</div>
          <div className={MUTED}>
            {m["admin_quotas.health.rule_line"]({
              scope: row.scope,
              metric: row.metric,
              window: row.window_label,
              // Separators travel with the facts they join; the word itself is
              // the message below.
              member: row.member_id ? ` · ${memberName(row.member_id)}` : "",
              inactive: row.is_active ? "" : ` · ${m["admin_quotas.health.inactive"]()}`,
            })}
          </div>
        </>
      ),
    },
    {
      key: "counter",
      header: m["admin_quotas.health.column.counter"](),
      render: (row) => show(row, row.counter_value),
    },
    {
      key: "ledger",
      header: m["admin_quotas.health.column.ledger"](),
      render: (row) => show(row, row.ledger_total),
    },
    {
      key: "difference",
      header: m["admin_quotas.health.column.difference"](),
      render: (row) =>
        row.difference === null ? (
          <Badge tone="warn">{m["admin_quotas.health.counter_unavailable"]()}</Badge>
        ) : ZERO.test(row.difference) ? (
          <span className={MUTED}>{m["admin_quotas.health.none"]()}</span>
        ) : (
          <Badge tone="warn">{show(row, row.difference)}</Badge>
        ),
    },
    {
      key: "ttl",
      header: m["admin_quotas.health.column.ttl"](),
      render: (row) =>
        row.counter_ttl_seconds === null ? (
          <span className={MUTED}>{m["admin_quotas.health.no_counter"]()}</span>
        ) : (
          m["admin_quotas.health.ttl"]({ hours: Math.round(row.counter_ttl_seconds / 3600) })
        ),
    },
    {
      key: "stale",
      header: m["admin_quotas.health.column.stale"](),
      render: (row) =>
        row.stale_in_progress > 0 ? (
          formatNumber(row.stale_in_progress)
        ) : (
          <span className={MUTED}>0</span>
        ),
    },
  ];

  return (
    <Card flush>
      <div className="flex items-center justify-between gap-4 p-5">
        <div>
          <h2 className="font-medium">{m["admin_quotas.health.title"]()}</h2>
          <p className={MUTED}>{m["admin_quotas.health.body"]()}</p>
        </div>
        <Button
          busy={reconcile.isPending}
          onClick={() =>
            reconcile.mutate(undefined, {
              onSuccess: (result) =>
                toast?.add({
                  title:
                    result.corrected === 0
                      ? m["admin_quotas.health.toast.match"]()
                      : m["admin_quotas.health.toast.corrected"]({ count: result.corrected }),
                  type: "success",
                }),
              onError: () =>
                toast?.add({ title: m["admin_quotas.health.toast.failed"](), type: "error" }),
            })
          }
        >
          {m["admin_quotas.health.reconcile"]()}
        </Button>
      </div>
      {health.isPending ? (
        <Spinner label={m["admin_quotas.health.loading"]()} />
      ) : health.error ? (
        <Notice tone="danger" title={m["admin_quotas.health.load_error.title"]()}>
          {/* i18n: server text — the API's own error message. */}
          {health.error instanceof Error ? health.error.message : m["admin_quotas.unknown_error"]()}
        </Notice>
      ) : (
        <Table
          columns={columns}
          rows={rows}
          rowKey={(row) => `${row.rule_id}:${row.member_id ?? "-"}:${row.window_id}`}
          empty={m["admin_quotas.health.empty"]()}
          caption={m["admin_quotas.health.caption"]()}
        />
      )}
    </Card>
  );
}

/**
 * How much of a rule is used.
 *
 * `current_value` being absent is *not* zero — it means the counter store could
 * not be reached — and the difference matters enough to say out loud rather than
 * render an empty bar that reads as "plenty of budget left".
 */
function Consumption({ rule }: { rule: LimitRule }) {
  const exact = useExactMoney();
  if (rule.applies_to === "each_member") {
    // No single figure exists for a per-member rule — one budget per member,
    // and the per-member consumption lives in the health table below. Say so
    // plainly rather than borrowing the "counter unavailable" badge, which
    // means the store could not be read and would be the wrong fact.
    return <span className={MUTED}>{m["admin_quotas.consumption.per_member_note"]()}</span>;
  }
  if (rule.applies_to === "each_user") {
    // Same reason: one budget per person, no single figure.
    return <span className={MUTED}>{m["admin_quotas.consumption.per_user_note"]()}</span>;
  }
  if (rule.current_value === null) {
    // A status, so a pill in the warn tone — but the words stay exactly as
    // they were ("counter unavailable", lowercase): the route tests pin that
    // wording, and rewording it here would be behaviour change dressed as
    // styling.
    return <Badge tone="warn">{m["admin_quotas.consumption.counter_unavailable"]()}</Badge>;
  }

  const used = Number(rule.current_value);
  const limit = Number(rule.limit_value);
  const caption =
    rule.metric === "cost"
      ? m["admin_quotas.consumption.caption"]({
          used: formatMoney(rule.current_value, "EUR", { exact }),
          limit: formatMoney(rule.limit_value, "EUR", { exact }),
        })
      : m["admin_quotas.consumption.caption"]({
          used: formatNumber(used),
          limit: formatNumber(limit),
        });

  return (
    <Meter
      value={used}
      limit={limit}
      label={m["admin_quotas.consumption.label"]({ name: rule.name || rule.scope })}
      caption={caption}
    />
  );
}

function CreateRuleDialog({ open, onClose }: { open: boolean; onClose: () => void }) {
  const create = useCreateLimit();
  const groups = useGroups();
  const users = useUsers();
  const toast = useOptionalToast();

  const [name, setName] = useState("");
  const [scope, setScope] = useState("group");
  const [scopeId, setScopeId] = useState("");
  const [appliesTo, setAppliesTo] = useState("aggregate");
  const [overrides, setOverrides] = useState(false);
  const [metric, setMetric] = useState("cost");
  const [kind, setKind] = useState<"period" | "rolling">("period");
  const [period, setPeriod] = useState("month");
  const [hours, setHours] = useState("6");
  const [limitValue, setLimitValue] = useState("10");

  const submit = () => {
    create.mutate(
      {
        name,
        scope,
        // A non-global rule needs a target; the API refuses a null one, which is
        // deliberate — an accidentally global cap is far too easy otherwise.
        scope_id: scope === "global" ? null : scopeId || null,
        // Per-member only means something for a group and per-user only for
        // everyone; the choice is offered there alone, and anything else goes
        // out as the aggregate default.
        applies_to: scope === "group" || scope === "global" ? appliesTo : "aggregate",
        // Only a rule for one person can stand in for everyone's ceiling.
        overrides_each_user: scope === "user" ? overrides : false,
        metric,
        window_seconds: kind === "rolling" ? Math.round(Number(hours) * 3600) : null,
        period: kind === "period" ? period : null,
        limit_value: limitValue,
      },
      {
        onSuccess: () => {
          toast?.add({ title: m["admin_quotas.toast.created"](), type: "success" });
          onClose();
        },
        onError: () =>
          toast?.add({ title: m["admin_quotas.toast.create_failed"](), type: "error" }),
      },
    );
  };

  return (
    <Dialog
      open={open}
      title={m["admin_quotas.create.title"]()}
      onClose={onClose}
      footer={
        <>
          <Button onClick={onClose}>{m["admin_quotas.create.cancel"]()}</Button>
          <Button variant="primary" busy={create.isPending} onClick={submit}>
            {m["admin_quotas.create.submit"]()}
          </Button>
        </>
      }
    >
      {create.error ? (
        <Notice tone="danger">
          {/* i18n: server text — the API's own error message. */}
          {create.error instanceof Error ? create.error.message : m["admin_quotas.unknown_error"]()}
        </Notice>
      ) : null}

      <Input
        label={m["admin_quotas.create.name"]()}
        value={name}
        onChange={(e) => setName(e.target.value)}
        placeholder={m["admin_quotas.create.name_placeholder"]()}
      />

      <div className={FORM_ROW}>
        <Select
          label={m["admin_quotas.create.scope"]()}
          value={scope}
          onChange={(e) => {
            setScope(e.target.value);
            setScopeId("");
            setAppliesTo("aggregate");
            setOverrides(false);
          }}
        >
          <option value="global">{m["admin_quotas.create.scope.global"]()}</option>
          <option value="group">{m["admin_quotas.create.scope.group"]()}</option>
          <option value="user">{m["admin_quotas.create.scope.user"]()}</option>
        </Select>

        {scope === "group" && (
          <Select
            label={m["admin_quotas.create.applies_to"]()}
            value={appliesTo}
            onChange={(e) => setAppliesTo(e.target.value)}
          >
            <option value="aggregate">{m["admin_quotas.create.applies.aggregate"]()}</option>
            <option value="each_member">{m["admin_quotas.create.applies.each_member"]()}</option>
          </Select>
        )}

        {scope === "global" && (
          <Select
            label={m["admin_quotas.create.applies_to"]()}
            value={appliesTo}
            onChange={(e) => setAppliesTo(e.target.value)}
          >
            <option value="aggregate">{m["admin_quotas.create.applies.aggregate_global"]()}</option>
            <option value="each_user">{m["admin_quotas.create.applies.each_user"]()}</option>
          </Select>
        )}

        {scope === "group" && (
          <Select
            label={m["admin_quotas.create.group"]()}
            value={scopeId}
            onChange={(e) => setScopeId(e.target.value)}
          >
            <option value="">{m["admin_quotas.create.choose"]()}</option>
            {(groups.data?.items ?? []).map((group) => (
              <option key={group.id} value={group.id}>
                {group.name}
              </option>
            ))}
          </Select>
        )}

        {scope === "user" && (
          <Select
            label={m["admin_quotas.create.user"]()}
            value={scopeId}
            onChange={(e) => setScopeId(e.target.value)}
          >
            <option value="">{m["admin_quotas.create.choose"]()}</option>
            {(users.data?.items ?? []).map((user) => (
              <option key={user.id} value={user.id}>
                {user.email ?? user.display_name ?? user.subject}
              </option>
            ))}
          </Select>
        )}
      </div>

      {scope === "user" && (
        <>
          <label className="flex cursor-pointer items-baseline gap-2">
            <input
              type="checkbox"
              checked={overrides}
              onChange={(event) => setOverrides(event.target.checked)}
            />
            <span>{m["admin_quotas.create.overrides"]()}</span>
          </label>
          <div className={MUTED}>{m["admin_quotas.create.overrides_help"]()}</div>
        </>
      )}

      <div className={FORM_ROW}>
        <Select
          label={m["admin_quotas.create.metric"]()}
          value={metric}
          onChange={(e) => setMetric(e.target.value)}
        >
          <option value="cost">{m["admin_quotas.create.metric.cost"]()}</option>
          <option value="tokens">{m["admin_quotas.create.metric.tokens"]()}</option>
          <option value="requests">{m["admin_quotas.create.metric.requests"]()}</option>
          <option value="own_search_requests">
            {m["admin_quotas.create.metric.own_search"]()}
          </option>
        </Select>

        <Select
          label={m["admin_quotas.create.window"]()}
          value={kind}
          onChange={(e) => setKind(e.target.value as "period" | "rolling")}
        >
          <option value="period">{m["admin_quotas.create.window.period"]()}</option>
          <option value="rolling">{m["admin_quotas.create.window.rolling"]()}</option>
        </Select>

        {kind === "period" ? (
          <Select
            label={m["admin_quotas.create.period"]()}
            value={period}
            onChange={(e) => setPeriod(e.target.value)}
          >
            {PERIODS.map((option) => (
              <option key={option} value={option}>
                {m["admin_quotas.create.period.every"]({ period: option })}
              </option>
            ))}
          </Select>
        ) : (
          <Input
            label={m["admin_quotas.create.hours"]()}
            type="number"
            min="0.1"
            step="0.5"
            value={hours}
            onChange={(e) => setHours(e.target.value)}
            hint={m["admin_quotas.create.hours_hint"]()}
          />
        )}
      </div>

      <Input
        label={
          metric === "cost"
            ? m["admin_quotas.create.limit_eur"]()
            : m["admin_quotas.create.limit_unit"]({ unit: unitFor(metric) })
        }
        type="number"
        min="0"
        step="0.01"
        value={limitValue}
        onChange={(e) => setLimitValue(e.target.value)}
        hint={
          kind === "period"
            ? m["admin_quotas.create.limit_hint.period"]()
            : m["admin_quotas.create.limit_hint.rolling"]()
        }
      />

      {/* Said here rather than left to be discovered from an invoice. Our
          search backends charge per request at rates that differ by more than
          ten times between tiers, so this ceiling is a count of searches and
          says nothing about what they cost. */}
      {metric === "own_search_requests" && (
        <Notice tone="warn" title={m["admin_quotas.create.volume_title"]()}>
          {m["admin_quotas.create.volume_body"]()}
        </Notice>
      )}
    </Dialog>
  );
}

/** Resetting takes an optional reason, and says plainly what a reset does not do. */
function ResetDialog({ rule, onClose }: { rule: LimitRule | null; onClose: () => void }) {
  const reset = useResetLimit();
  const toast = useOptionalToast();
  const [reason, setReason] = useState("");

  const submit = () => {
    if (!rule) return;
    reset.mutate(
      { id: rule.id, reason },
      {
        onSuccess: () => {
          toast?.add({ title: m["admin_quotas.reset.toast.done"](), type: "success" });
          setReason("");
          onClose();
        },
        onError: () =>
          toast?.add({ title: m["admin_quotas.reset.toast.failed"](), type: "error" }),
      },
    );
  };

  return (
    <Dialog
      open={rule !== null}
      title={m["admin_quotas.reset.title"]({
        name: rule?.name || m["admin_quotas.reset.title_fallback"](),
      })}
      onClose={onClose}
      footer={
        <>
          <Button onClick={onClose}>{m["admin_quotas.reset.cancel"]()}</Button>
          <Button
            variant="primary"
            busy={reset.isPending}
            onClick={submit}
          >
            {m["admin_quotas.reset.submit"]()}
          </Button>
        </>
      }
    >
      <Notice tone="info" title={m["admin_quotas.reset.notice_title"]()}>
        {m["admin_quotas.reset.notice_body"]()}
      </Notice>

      {reset.error ? (
        <Notice tone="danger">
          {/* i18n: server text — the API's own error message. */}
          {reset.error instanceof Error ? reset.error.message : m["admin_quotas.unknown_error"]()}
        </Notice>
      ) : null}

      <Input
        label={m["admin_quotas.reset.reason"]()}
        value={reason}
        onChange={(e) => setReason(e.target.value)}
        placeholder={m["admin_quotas.reset.reason_placeholder"]()}
        hint={m["admin_quotas.reset.reason_hint"]()}
      />
    </Dialog>
  );
}

function HistoryDialog({ rule, onClose }: { rule: LimitRule | null; onClose: () => void }) {
  // Nothing prunes the reset trail, so this is the one dialog whose contents
  // grow forever. Newest first, a page at a time.
  const paged = usePaginated(10);
  const resets = useResets(rule?.id ?? null, paged.page);

  return (
    <Dialog
      open={rule !== null}
      title={m["admin_quotas.history.title"]({
        name: rule?.name || m["admin_quotas.history.title_fallback"](),
      })}
      onClose={onClose}
    >
      {resets.isPending ? (
        <Spinner />
      ) : (resets.data?.total ?? 0) === 0 ? (
        <p className={MUTED}>{m["admin_quotas.history.empty"]()}</p>
      ) : (
        <>
        <Table
          columns={[
            {
              key: "when",
              header: m["admin_quotas.history.column.when"](),
              render: (row) => formatDateTime(row.effective_at),
            },
            {
              key: "who",
              header: m["admin_quotas.history.column.who"](),
              // Null once the account is erased; the reset row survives, which is
              // why created_by is ON DELETE SET NULL rather than CASCADE.
              render: (row) => row.created_by_email ?? m["admin_quotas.history.erased"](),
            },
            { key: "why", header: m["admin_quotas.history.column.why"](), render: (row) => row.reason },
          ]}
          rows={resets.data?.items ?? []}
          rowKey={(row) => row.id}
        />
        <Pagination
          total={resets.data?.total ?? 0}
          limit={paged.limit}
          offset={paged.offset}
          onOffsetChange={paged.setOffset}
          noun={m["admin_quotas.history.noun"]()}
          busy={resets.isFetching}
        />
        </>
      )}
    </Dialog>
  );
}

/**
 * The window, in words.
 *
 * The API reports a rolling window as raw seconds because that is what it
 * stores; "per 86400s" is not something to put in front of a person. A calendar
 * period already reads correctly and is passed through.
 */
function describeWindow(rule: LimitRule): string {
  if (rule.period) return rule.period;
  const seconds = rule.window_seconds ?? 0;
  if (seconds % 86_400 === 0 && seconds >= 86_400) {
    // A one-day window reads "24 hours", not "1 day": the figure a reader
    // checks against a provider's own rolling day is 24 hours.
    return m["admin_quotas.window.days"]({ count: seconds / 86_400 });
  }
  if (seconds % 3600 === 0 && seconds >= 3600) {
    return m["admin_quotas.window.hours"]({ count: seconds / 3600 });
  }
  if (seconds % 60 === 0 && seconds >= 60) {
    return m["admin_quotas.window.minutes"]({ count: seconds / 60 });
  }
  return m["admin_quotas.window.seconds"]({ count: seconds });
}

/** At or past the cap: the next request against this rule is refused. */
function exhausted(rule: LimitRule): boolean {
  if (rule.current_value === null) return false;
  return Number(rule.current_value) >= Number(rule.limit_value);
}

/**
 * The strip's state line: whether any ceiling is currently refusing requests,
 * else whether any is close, else the quiet fact that everything is within
 * budget. Reuses the row's own predicates so the strip can never disagree
 * with the badges below it about what "over" means.
 */
function quotaState(rules: LimitRule[]): string {
  if (rules.length === 0) return m["admin_quotas.state.empty"]();
  const over = rules.filter(exhausted).length;
  if (over > 0) return m["admin_quotas.state.over"]({ count: over });
  const near = rules.filter(nearLimit).length;
  if (near > 0) return m["admin_quotas.state.near"]({ count: near });
  return m["admin_quotas.state.ok"]();
}

function nearLimit(rule: LimitRule): boolean {
  if (rule.current_value === null) return false;
  const limit = Number(rule.limit_value);
  return limit > 0 && Number(rule.current_value) / limit >= 0.8;
}

function shortId(id: string | null): string {
  return id ? `${id.slice(0, 8)}…` : "—";
}


