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

} from "@llmp/ui";
import type { Column } from "@llmp/ui";
import { formatMoney, useExactMoney } from "@llmp/ui";
import { useState } from "react";
import {
  useCreateLimit,
  useDeleteLimit,
  useGroups,
  useLimits,
  useResetLimit,
  useResets,
  useUsers,
} from "../lib/admin";
import { unitFor } from "../lib/metrics";
import { usePaginated } from "../lib/paging";
import type { LimitRule } from "../lib/types";
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
      header: "Rule",
      render: (rule) => (
        <>
          <div>{rule.name || <em className={MUTED}>unnamed</em>}</div>
          <div className={MUTED}>
            {rule.scope} · {nameFor(rule)}
          </div>
        </>
      ),
    },
    {
      key: "limit",
      header: "Limit",
      render: (rule) => (
        <>
          <div>
            {rule.metric === "cost"
              ? formatMoney(rule.limit_value, "EUR", { exact })
              : `${Number(rule.limit_value).toLocaleString()} ${unitFor(rule.metric)}`}
          </div>
          <div className={`${MUTED} ${NOWRAP}`}>per {describeWindow(rule)}</div>
        </>
      ),
    },
    {
      key: "usage",
      header: "Used",
      render: (rule) => <Consumption rule={rule} />,
    },
    {
      key: "status",
      header: "Status",
      render: (rule) => (
        <div className={CHIPS}>
          {!rule.is_active && <Badge>Inactive</Badge>}
          {/* The state someone scanning this page is looking for. Without it a
              rule at 200% of its cap still showed a green "Active", which is
              true and useless — the row's meaning is that requests are being
              refused right now. */}
          {rule.is_active && exhausted(rule) && <Badge tone="danger">Over budget</Badge>}
          {rule.is_active && !exhausted(rule) && nearLimit(rule) && (
            <Badge tone="warn">Nearly spent</Badge>
          )}
          {rule.is_active && !exhausted(rule) && !nearLimit(rule) && (
            <Badge tone="ok">Active</Badge>
          )}
          {rule.last_reset_at && (
            <Badge tone="accent">{`Reset ${formatDate(rule.last_reset_at)}`}</Badge>
          )}
        </div>
      ),
    },
    {
      key: "actions",
      header: "",
      render: (rule) => (
        <div className={ROW_ACTIONS}>
          <Button onClick={() => setHistory(rule)}>History</Button>
          <Button onClick={() => setResetting(rule)}>Reset</Button>
          <Button
            variant="ghost"
            className="text-danger"
            busy={remove.isPending && remove.variables === rule.id}
            onClick={() =>
              remove.mutate(rule.id, {
                onSuccess: () => toast?.add({ title: "Quota rule deleted", type: "success" }),
                onError: () =>
                  toast?.add({ title: "Could not delete the rule", type: "error" }),
              })
            }
          >
            Delete
          </Button>
        </div>
      ),
    },
  ];

  return (
    <div className={PAGE}>
      <PageHeader
        title="Quotas"
        subtitle="All matching rules must pass, so adding one can only tighten a budget."
        actions={
          <Button variant="primary" onClick={() => setCreating(true)}>
            New rule
          </Button>
        }
      />

      {remove.error ? (
        <Notice tone="danger" title="Could not delete the rule">
          {remove.error instanceof Error ? remove.error.message : "Unknown error."}
        </Notice>
      ) : null}

      <Card flush>
        {limits.isPending ? (
          <Spinner label="Loading quota rules" />
        ) : limits.error ? (
          <Notice tone="danger" title="Could not load quota rules">
            {limits.error instanceof Error ? limits.error.message : "Unknown error."}
          </Notice>
        ) : (
          <>
            {/* Count and state before the rows: how many ceilings, and whether
                any of them is currently refusing requests. No actions — "New
                rule" stays in the page header, and a second one here would be
                the button duplicated rather than relocated. */}
            <div className="p-5">
              <SummaryStrip
                headline={`${(limits.data?.items ?? []).length} ${(limits.data?.items ?? []).length === 1 ? "rule" : "rules"}`}
                detail={quotaState(limits.data?.items ?? [])}
                active={(limits.data?.items ?? []).length > 0}
              />
            </div>
            <Table
              columns={columns}
              rows={limits.data?.items ?? []}
              rowKey={(rule) => rule.id}
              empty="No quota rules. Nothing is capped."
              caption="Quota rules and their live consumption."
            />
          </>
        )}
      </Card>

      <CreateRuleDialog open={creating} onClose={() => setCreating(false)} />
      <ResetDialog rule={resetting} onClose={() => setResetting(null)} />
      <HistoryDialog rule={history} onClose={() => setHistory(null)} />
    </div>
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
  if (rule.current_value === null) {
    // A status, so a pill in the warn tone — but the words stay exactly as
    // they were ("counter unavailable", lowercase): the route tests pin that
    // wording, and rewording it here would be behaviour change dressed as
    // styling.
    return <Badge tone="warn">counter unavailable</Badge>;
  }

  const used = Number(rule.current_value);
  const limit = Number(rule.limit_value);
  const caption =
    rule.metric === "cost"
      ? `${formatMoney(rule.current_value, "EUR", { exact })} of ${formatMoney(
          rule.limit_value,
          "EUR",
          { exact },
        )}`
      : `${used.toLocaleString()} of ${limit.toLocaleString()}`;

  return (
    <Meter
      value={used}
      limit={limit}
      label={`${rule.name || rule.scope} consumption`}
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
        metric,
        window_seconds: kind === "rolling" ? Math.round(Number(hours) * 3600) : null,
        period: kind === "period" ? period : null,
        limit_value: limitValue,
      },
      {
        onSuccess: () => {
          toast?.add({ title: "Quota rule created", type: "success" });
          onClose();
        },
        onError: () => toast?.add({ title: "Could not create the rule", type: "error" }),
      },
    );
  };

  return (
    <Dialog
      open={open}
      title="New quota rule"
      onClose={onClose}
      footer={
        <>
          <Button onClick={onClose}>Cancel</Button>
          <Button variant="primary" busy={create.isPending} onClick={submit}>
            Create
          </Button>
        </>
      }
    >
      {create.error ? (
        <Notice tone="danger">
          {create.error instanceof Error ? create.error.message : "Unknown error."}
        </Notice>
      ) : null}

      <Input label="Name" value={name} onChange={(e) => setName(e.target.value)} placeholder="research monthly budget" />

      <div className={FORM_ROW}>
        <Select label="Scope" value={scope} onChange={(e) => { setScope(e.target.value); setScopeId(""); }}>
          <option value="global">Everyone</option>
          <option value="group">A group</option>
          <option value="user">A user</option>
        </Select>

        {scope === "group" && (
          <Select label="Group" value={scopeId} onChange={(e) => setScopeId(e.target.value)}>
            <option value="">Choose…</option>
            {(groups.data?.items ?? []).map((group) => (
              <option key={group.id} value={group.id}>
                {group.name}
              </option>
            ))}
          </Select>
        )}

        {scope === "user" && (
          <Select label="User" value={scopeId} onChange={(e) => setScopeId(e.target.value)}>
            <option value="">Choose…</option>
            {(users.data?.items ?? []).map((user) => (
              <option key={user.id} value={user.id}>
                {user.email ?? user.display_name ?? user.subject}
              </option>
            ))}
          </Select>
        )}
      </div>

      <div className={FORM_ROW}>
        <Select label="Metric" value={metric} onChange={(e) => setMetric(e.target.value)}>
          <option value="cost">Cost</option>
          <option value="tokens">Tokens</option>
          <option value="requests">Requests</option>
          <option value="own_search_requests">Web searches (ours)</option>
        </Select>

        <Select
          label="Window"
          value={kind}
          onChange={(e) => setKind(e.target.value as "period" | "rolling")}
        >
          <option value="period">Calendar period</option>
          <option value="rolling">Rolling window</option>
        </Select>

        {kind === "period" ? (
          <Select label="Period" value={period} onChange={(e) => setPeriod(e.target.value)}>
            {PERIODS.map((option) => (
              <option key={option} value={option}>
                Every {option}
              </option>
            ))}
          </Select>
        ) : (
          <Input
            label="Hours"
            type="number"
            min="0.1"
            step="0.5"
            value={hours}
            onChange={(e) => setHours(e.target.value)}
            hint="Rolling, from now backwards"
          />
        )}
      </div>

      <Input
        label={metric === "cost" ? "Limit (EUR)" : `Limit (${unitFor(metric)})`}
        type="number"
        min="0"
        step="0.01"
        value={limitValue}
        onChange={(e) => setLimitValue(e.target.value)}
        hint={
          kind === "period"
            ? "Resets on the calendar boundary, in the billing timezone."
            : "Counted over the rolling window."
        }
      />

      {/* Said here rather than left to be discovered from an invoice. Our
          search backends charge per request at rates that differ by more than
          ten times between tiers, so this ceiling is a count of searches and
          says nothing about what they cost. */}
      {metric === "own_search_requests" && (
        <Notice tone="warn" title="This limits volume, not spend">
          Counts calls to our own web-search backends. Their per-request prices
          differ by more than ten times between tiers, so a search budget does
          not bound what searches cost. Use a cost rule for that.
        </Notice>
      )}
    </Dialog>
  );
}

/** Resetting requires a reason, and says plainly what a reset does not do. */
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
          toast?.add({ title: "Consumption reset", type: "success" });
          setReason("");
          onClose();
        },
        onError: () => toast?.add({ title: "Could not reset consumption", type: "error" }),
      },
    );
  };

  return (
    <Dialog
      open={rule !== null}
      title={`Reset ${rule?.name || "this rule"}`}
      onClose={onClose}
      footer={
        <>
          <Button onClick={onClose}>Cancel</Button>
          <Button
            variant="primary"
            busy={reset.isPending}
            disabled={reason.trim().length < 3}
            onClick={submit}
          >
            Reset consumption
          </Button>
        </>
      }
    >
      <Notice tone="info" title="This does not change anyone's bill">
        Only what the quota counts moves; usage records are untouched. The reset is
        immediate and cannot be scheduled.
      </Notice>

      {reset.error ? (
        <Notice tone="danger">
          {reset.error instanceof Error ? reset.error.message : "Unknown error."}
        </Notice>
      ) : null}

      <Input
        label="Reason"
        value={reason}
        onChange={(e) => setReason(e.target.value)}
        placeholder="grant extension approved"
        hint="Required and kept permanently."
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
    <Dialog open={rule !== null} title={`Resets · ${rule?.name || "rule"}`} onClose={onClose}>
      {resets.isPending ? (
        <Spinner />
      ) : (resets.data?.total ?? 0) === 0 ? (
        <p className={MUTED}>This rule has never been reset.</p>
      ) : (
        <>
        <Table
          columns={[
            { key: "when", header: "When", render: (row) => formatDateTime(row.effective_at) },
            {
              key: "who",
              header: "Who",
              // Null once the account is erased; the reset row survives, which is
              // why created_by is ON DELETE SET NULL rather than CASCADE.
              render: (row) => row.created_by_email ?? "(erased user)",
            },
            { key: "why", header: "Reason", render: (row) => row.reason },
          ]}
          rows={resets.data?.items ?? []}
          rowKey={(row) => row.id}
        />
        <Pagination
          total={resets.data?.total ?? 0}
          limit={paged.limit}
          offset={paged.offset}
          onOffsetChange={paged.setOffset}
          noun="resets"
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
    const days = seconds / 86_400;
    return days === 1 ? "24 hours" : `${days} days`;
  }
  if (seconds % 3600 === 0 && seconds >= 3600) {
    const hours = seconds / 3600;
    return hours === 1 ? "hour" : `${hours} hours`;
  }
  if (seconds % 60 === 0 && seconds >= 60) {
    const minutes = seconds / 60;
    return minutes === 1 ? "minute" : `${minutes} minutes`;
  }
  return `${seconds}s`;
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
  if (rules.length === 0) return "nothing is capped";
  const over = rules.filter(exhausted).length;
  if (over > 0) return `${over} over budget`;
  const near = rules.filter(nearLimit).length;
  if (near > 0) return `${near} nearly spent`;
  return "all within budget";
}

function nearLimit(rule: LimitRule): boolean {
  if (rule.current_value === null) return false;
  const limit = Number(rule.limit_value);
  return limit > 0 && Number(rule.current_value) / limit >= 0.8;
}

function shortId(id: string | null): string {
  return id ? `${id.slice(0, 8)}…` : "—";
}

function formatDate(iso: string): string {
  return new Date(iso).toLocaleDateString(undefined, { month: "short", day: "numeric" });
}

function formatDateTime(iso: string): string {
  return new Date(iso).toLocaleString(undefined, {
    year: "numeric",
    month: "short",
    day: "numeric",
    hour: "2-digit",
    minute: "2-digit",
  });
}
