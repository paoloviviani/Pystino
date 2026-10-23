import {
  Badge,
  Button,
  Card,
  Dialog,
  Input,
  Meter,
  Money,
  Notice,
  Select,
  Spinner,
  Stat,
  SummaryStrip,
  Table,
  } from "@llmp/ui";
import type { Column } from "@llmp/ui";
import { useState } from "react";
import { OpencodeSetup } from "../components/OpencodeSetup";
import { PageHeader } from "../components/PageHeader";
import { downloadCsv } from "../lib/api";
import {
  CODE,
  MUTED,
  PAGE,
  QUOTA,
  QUOTA_DETAIL,
  QUOTA_HEAD,
  QUOTA_LIST,
  QUOTA_NAME,
  ROW_ACTIONS,
  SECRET,
  SECRET_DETAIL,
  SECRET_ROW,
  STATS,
} from "../lib/layout";
import { unitFor } from "../lib/metrics";
import { formatDate, formatNumber } from "../lib/format";
import { Message } from "../components/Message";
import { m } from "../paraglide/messages.js";
import { recentPeriods } from "../lib/periods";
import {
  useDeleteKey,
  useMintKey,
  useMyKeys,
  useMyLimits,
  useMyReport,
  useRevokeKey,
  useSetNotificationThresholds,
} from "../lib/queries";
import type { ApiKey, Me, MintedApiKey, MyLimit, UsageReportRow } from "../lib/types";
import { useOptionalToast } from "../lib/toast";

export interface OverviewProps {
  me: Me;
}

/**
 * Milli-EUR on this screen.
 *
 * This is the page a person opens to see what they spent, and the ledger's
 * twelve decimal places are noise there — `€0.001497172` is harder to read than
 * `€0.001` and answers a question nobody asked. Full precision belongs on the
 * admin screens, where it is being reconciled against a provider's invoice.
 *
 * The formatter says `< €0.001` rather than `€0.000` for a real amount below
 * that, so capping the decimals cannot tell somebody they spent nothing.
 */
const SPEND_DECIMALS = 3;

/** Message functions, not strings: this list is built once, at import. */
const BREAKDOWNS = [
  { value: "model", label: m["overview.breakdown.model"] },
  { value: "day", label: m["overview.breakdown.day"] },
  { value: "group", label: m["overview.breakdown.group"] },
  { value: "api_key", label: m["overview.breakdown.api_key"] },
];

export function Overview({ me }: OverviewProps) {
  const periods = recentPeriods();
  const [period, setPeriod] = useState(periods[0]?.value ?? "");
  const [groupBy, setGroupBy] = useState("model");

  const report = useMyReport(period, groupBy);
  const keys = useMyKeys();
  const [minting, setMinting] = useState(false);
  const [revoking, setRevoking] = useState<ApiKey | null>(null);
  const [deleting, setDeleting] = useState<ApiKey | null>(null);

  const columns: Column<UsageReportRow>[] = [
    { key: "label", header: breakdownNoun(groupBy), render: (row) => row.label },
    {
      key: "requests",
      header: m["overview.column.requests"](),
      numeric: true,
      render: (row) => formatNumber(row.requests),
    },
    {
      key: "tokens",
      header: m["overview.column.tokens"](),
      numeric: true,
      render: (row) => formatNumber(row.total_tokens),
    },
    {
      key: "cost",
      header: m["overview.column.cost"](),
      numeric: true,
      render: (row) => (
        <Money
          amount={row.cost}
          currency={report.data?.currency ?? "EUR"}
          maxDecimals={SPEND_DECIMALS}
        />
      ),
    },
  ];

  return (
    <div className={PAGE}>
      <PageHeader
        title={m["overview.title"]()}
        subtitle={m["overview.subtitle"]()}
        /* "Overview", not "Your usage" — that name now belongs to the reports
            page next to it in the header, and two screens wearing it is worse
            than either name being imperfect. */
        actions={
          <Select
            label={m["overview.filter.period"]()}
            hideLabel
            value={period}
            onChange={(event) => setPeriod(event.target.value)}
          >
            {/* i18n: server text — the period labels are built by lib/periods.ts. */}
            {periods.map((option) => (
              <option key={option.value} value={option.value}>
                {option.label}
              </option>
            ))}
          </Select>
        }
      />
      {/* Which source tree this UI was built from, muted under the header so a
          stale deploy is visible where administrators look. Baked at build
          time (Dockerfile CONSOLE_BUILD_SHA); "unknown" means built by hand
          without it, and the gateway's startup log still says which files are
          actually served. */}
      <p className={`${MUTED} -mt-2 text-dense`}>
        {m["overview.build"]({ sha: import.meta.env.VITE_CONSOLE_BUILD_SHA ?? "unknown" })}
      </p>

      <Card>
        {report.isPending && <Spinner label={m["overview.loading"]()} />}
        {report.error ? (
          <Notice tone="danger" title={m["overview.load_error.title"]()}>
            {/* i18n: server text — the API's own error message. */}
            {report.error instanceof Error ? report.error.message : m["overview.unknown_error"]()}
          </Notice>
        ) : null}
        {report.data && (
          <>
            <div className={STATS}>
              <Stat
                label={m["overview.stat.spend"]({ period: report.data.period.label })}
                value={
                  <Money
                    amount={report.data.totals.cost}
                    currency={report.data.currency}
                    maxDecimals={SPEND_DECIMALS}
                  />
                }
                detail={m["overview.stat.period_detail"]({ timezone: report.data.period.timezone })}
              />
              <Stat
                label={m["overview.stat.requests"]()}
                value={formatNumber(report.data.totals.requests)}
                detail={m["overview.stat.tokens_detail"]({ tokens: report.data.totals.total_tokens })}
              />
              <Stat
                label={m["overview.stat.groups"]()}
                value={formatNumber(me.groups.length)}
                detail={
                  me.default_billing_group
                    ? m["overview.stat.billing_to"]({ group: me.default_billing_group.name })
                    : m["overview.stat.no_billing_group"]()
                }
              />
            </div>

          </>
        )}
      </Card>

      <QuotaCard currency={report.data?.currency ?? "EUR"} />

      <Card
        title={m["overview.breakdown_card"]()}
        flush
        actions={
          <Select
            label={m["overview.filter.breakdown"]()}
            hideLabel
            value={groupBy}
            onChange={(event) => setGroupBy(event.target.value)}
          >
            {BREAKDOWNS.map((option) => (
              <option key={option.value} value={option.value}>
                {option.label()}
              </option>
            ))}
          </Select>
        }
      >
        {report.isPending ? (
          <Spinner />
        ) : (
          <>
            {/* The strip summarises the rows before they start — a count, the
                period, and the export that used to sit in the header. Moving
                it here rather than duplicating it: two "Export CSV" buttons is
                how one of them ends up working while the table loads. */}
            {report.data && (
              <div className="p-5">
                <SummaryStrip
                  headline={m["overview.row_count"]({ count: report.data.rows.length })}
                  detail={m["overview.summary_detail"]({
                    period: report.data.period.label,
                    breakdown: labelFor(groupBy),
                  })}
                  active={report.data.rows.length > 0}
                  actions={
                    <Button
                      onClick={() =>
                        downloadCsv(
                          `/api/me/reports/usage.csv?period=${encodeURIComponent(period)}` +
                            `&group_by=${encodeURIComponent(groupBy)}`,
                        )
                      }
                    >
                      {m["overview.export_csv"]()}
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
              empty={m["overview.empty"]()}
              caption={m["overview.caption"]({
                period: report.data?.period.label ?? m["overview.caption_period_fallback"](),
                breakdown: labelFor(groupBy),
              })}
            />
          </>
        )}
      </Card>

      <Card
        title={m["overview.keys_card"]()}
        flush
        description={m["overview.keys_description"]()}
      >
        {keys.isPending ? (
          <Spinner />
        ) : (
          <>
            {me.capabilities !== null && !me.capabilities.api_keys ? (
              <div className="p-5">
                <Notice
                  tone="warn"
                  title={m["overview.keys.denied_title"]()}
                >
                  {m["overview.keys.denied_body"]()}
                </Notice>
              </div>
            ) : (
              <div className="p-5">
                <SummaryStrip
                  headline={m["overview.keys.count"]({ count: (keys.data?.items ?? []).length })}
                  detail={
                    (keys.data?.items ?? []).length === 0
                      ? m["overview.keys.none_yet"]()
                      : m["overview.keys.working"]({
                          count: (keys.data?.items ?? []).filter(
                            (key) => !key.revoked_at && !isExpired(key),
                          ).length,
                        })
                  }
                  active={(keys.data?.items ?? []).length > 0}
                  actions={
                    <Button variant="primary" onClick={() => setMinting(true)}>
                      {m["overview.keys.new_key"]()}
                    </Button>
                  }
                />
              </div>
            )}

            <Table
              columns={keyColumns(setRevoking, setDeleting)}
              rows={keys.data?.items ?? []}
              rowKey={(key) => key.id}
              empty={m["overview.keys.empty"]()}
            />
          </>
        )}
      </Card>

      <Card title={m["overview.opencode_card"]()}>
        <OpencodeSetup />
      </Card>

      <MintKeyDialog open={minting} me={me} onClose={() => setMinting(false)} />
      <RevokeKeyDialog apiKey={revoking} onClose={() => setRevoking(null)} />
      <DeleteKeyDialog apiKey={deleting} onClose={() => setDeleting(null)} />
    </div>
  );
}

/**
 * Where this person stands against every ceiling that applies to them.
 *
 * Above the breakdown, because "how much is left" is the question someone opens
 * this screen to answer and the breakdown is what they read afterwards to
 * understand it.
 *
 * Quotas are *all rules must pass* (ADR 0009), so this is a list of ceilings
 * and the binding one is whichever is nearest. Nothing here computes that:
 * "nearest" across euros, tokens and requests is not a comparison that can be
 * made honestly, and a single headline number that quietly picked one metric
 * would be worse than three honest bars.
 */
function QuotaCard({ currency }: { currency: string }) {
  const limits = useMyLimits();

  if (limits.error) {
    return (
      <Card title={m["overview.quotas.title"]()}>
        <Notice tone="danger" title={m["overview.quotas.load_error.title"]()}>
          {/* i18n: server text — the API's own error message. */}
          {limits.error instanceof Error ? limits.error.message : m["overview.unknown_error"]()}
        </Notice>
      </Card>
    );
  }

  if (limits.isPending) {
    return (
      <Card title={m["overview.quotas.title"]()}>
        <Spinner label={m["overview.quotas.loading"]()} />
      </Card>
    );
  }

  const rules = limits.data?.items ?? [];
  if (rules.length === 0) {
    return (
      <Card title={m["overview.quotas.title"]()} description={m["overview.quotas.none"]()}>
        {/* Said plainly rather than left blank: an empty card reads as a screen
            that failed to load, and "no limit" is a fact worth stating. */}
      </Card>
    );
  }

  return (
    <Card title={m["overview.quotas.title"]()} description={m["overview.quotas.description"]()}>
      <div className={QUOTA_LIST}>
        {rules.map((rule) => (
          <QuotaMeter key={rule.id} rule={rule} currency={currency} />
        ))}
      </div>
    </Card>
  );
}

function QuotaMeter({ rule, currency }: { rule: MyLimit; currency: string }) {
  const limit = Number(rule.limit_value);
  // Absent is not zero. An unreachable counter store must not draw as an
  // untouched budget, so no bar is drawn at all rather than a guessed one.
  const used = rule.current_value === null ? null : Number(rule.current_value);
  const thresholds = useSetNotificationThresholds();
  const toast = useOptionalToast();
  const [editing, setEditing] = useState(false);
  const [value, setValue] = useState(
    (rule.notification_thresholds ?? []).join(", "),
  );

  return (
    <div className={QUOTA}>
      <div className={QUOTA_HEAD}>
        <span className={QUOTA_NAME}>{rule.name}</span>
        <Badge>{SCOPE_LABEL[rule.scope]?.() ?? rule.scope}</Badge>
      </div>
      {used === null ? (
        <p className={QUOTA_DETAIL}>{m["overview.quotas.unavailable"]()}</p>
      ) : (
        <>
          <Meter
            value={used}
            limit={limit}
            label={m["overview.quotas.meter_label"]({ name: rule.name, window: rule.window_label })}
          />
          <p className={QUOTA_DETAIL}>
            {rule.metric === "cost" ? (
              <>
                <Money amount={rule.current_value ?? "0"} currency={currency} />{" "}
                {m["overview.quotas.cost_of"]()}{" "}
                <Money amount={rule.limit_value} currency={currency} />
              </>
            ) : (
              m["overview.quotas.count_line"]({
                used,
                limit,
                unit: unitFor(rule.metric),
                window: rule.window_label,
              })
            )}{" "}
            · {rule.window_label}
          </p>
          {editing ? (
            <form
              className="flex items-end gap-2"
              onSubmit={(event) => {
                event.preventDefault();
                thresholds.mutate(
                  {
                    ruleId: rule.id,
                    thresholds: value
                      .split(",")
                      .map((part) => Number(part.trim()))
                      .filter((n) => Number.isInteger(n) && n >= 1 && n <= 100),
                  },
                  {
                    onSuccess: () => {
                      toast?.add({ title: m["overview.quotas.notices_saved"](), type: "success" });
                      setEditing(false);
                    },
                    onError: () =>
                      toast?.add({
                        title: m["overview.quotas.notices_failed"](),
                        type: "error",
                      }),
                  },
                );
              }}
            >
              <input
                className="w-40 rounded-md border border-line bg-surface px-2 py-1 text-body"
                value={value}
                onChange={(event) => setValue(event.target.value)}
                placeholder={m["overview.quotas.thresholds_placeholder"]()}
                aria-label={m["overview.quotas.thresholds_aria"]({ name: rule.name })}
                autoFocus
              />
              <Button variant="ghost" onClick={() => setEditing(false)}>
                {m["overview.quotas.cancel"]()}
              </Button>
              <Button type="submit" busy={thresholds.isPending}>
                {m["overview.quotas.save"]()}
              </Button>
            </form>
          ) : (
            <button
              type="button"
              onClick={() => setEditing(true)}
              className="cursor-pointer text-left text-dense text-ink-faint underline-offset-2 hover:text-ink-muted hover:underline"
            >
              {(rule.notification_thresholds ?? []).length > 0
                ? m["overview.quotas.threshold_set"]({
                    thresholds: (rule.notification_thresholds ?? []).join("%, "),
                  })
                : m["overview.quotas.threshold_unset"]()}
            </button>
          )}
        </>
      )}
    </div>
  );
}

const SCOPE_LABEL: Record<string, () => string> = {
  global: m["overview.quotas.scope.global"],
  group: m["overview.quotas.scope.group"],
  user: m["overview.quotas.scope.user"],
};


function MintKeyDialog({
  open,
  me,
  onClose,
}: {
  open: boolean;
  me: Me;
  onClose: () => void;
}) {
  const mint = useMintKey();
  const toast = useOptionalToast();
  const [name, setName] = useState("");
  const [groupId, setGroupId] = useState("");
  const [expiry, setExpiry] = useState("");
  // Held here and nowhere else. Not in the query cache, not in the key list:
  // this is the only copy that will ever exist and it dies with the dialog.
  const [minted, setMinted] = useState<MintedApiKey | null>(null);

  const close = () => {
    setMinted(null);
    setName("");
    setGroupId("");
    setExpiry("");
    mint.reset();
    onClose();
  };

  const submit = () =>
    mint.mutate(
      {
        name: name.trim(),
        billing_group_id: groupId === "" ? null : groupId,
        expires_in_days: expiry === "" ? null : Number(expiry),
      },
      {
        onSuccess: (created) => {
          setMinted(created);
          // The dialog shows the secret itself; the toast is for after "Done",
          // when the dialog is gone and the list has quietly grown.
          toast?.add({ title: m["overview.mint.toast_created"](), type: "success" });
        },
        onError: () => toast?.add({ title: m["overview.mint.toast_failed"](), type: "error" }),
      },
    );

  return (
    <Dialog
      open={open}
      title={minted ? m["overview.mint.title_minted"]() : m["overview.mint.title"]()}
      onClose={close}
      footer={
        minted ? (
          <Button variant="primary" onClick={close}>
            {m["overview.mint.done"]()}
          </Button>
        ) : (
          <>
            <Button onClick={close}>{m["overview.mint.cancel"]()}</Button>
            <Button variant="primary" busy={mint.isPending} onClick={submit}>
              {m["overview.mint.create"]()}
            </Button>
          </>
        )
      }
    >
      {minted ? (
        <MintedSecret minted={minted} />
      ) : (
        <>
          {mint.error ? (
            <Notice tone="danger">
              {/* i18n: server text — the API's own error message. */}
              {mint.error instanceof Error ? mint.error.message : m["overview.unknown_error"]()}
            </Notice>
          ) : null}

          <Input
            label={m["overview.mint.name"]()}
            value={name}
            onChange={(event) => setName(event.target.value)}
            placeholder={m["overview.mint.name_placeholder"]()}
            hint={m["overview.mint.name_hint"]()}
          />

          <Select
            label={m["overview.mint.billing_group"]()}
            value={groupId}
            onChange={(event) => setGroupId(event.target.value)}
            hint={m["overview.mint.billing_group_hint"]()}
          >
            {/* Empty means "resolve at request time", so the key follows a later
                change of default rather than pinning today's answer. */}
            <option value="">
              {me.default_billing_group
                ? m["overview.mint.default_group_current"]({ group: me.default_billing_group.name })
                : m["overview.mint.default_group"]()}
            </option>
            {me.groups.map((group) => (
              <option key={group.id} value={group.id}>
                {group.name}
              </option>
            ))}
          </Select>

          {/* The API refuses this case with a sentence worth reading, but saying
              it before the click is better than after. */}
          {groupId === "" && me.default_billing_group === null && (
            <Notice tone="warn">{m["overview.mint.no_default_warning"]()}</Notice>
          )}

          <Select
            label={m["overview.mint.expires"]()}
            value={expiry}
            onChange={(event) => setExpiry(event.target.value)}
            hint={m["overview.mint.expires_hint"]()}
          >
            <option value="">{m["overview.mint.expires.never"]()}</option>
            <option value="30">{m["overview.mint.expires.30"]()}</option>
            <option value="90">{m["overview.mint.expires.90"]()}</option>
            <option value="365">{m["overview.mint.expires.365"]()}</option>
          </Select>
        </>
      )}
    </Dialog>
  );
}

/**
 * The secret, shown once.
 *
 * The gateway stores a hash, so this cannot be re-read, re-sent or recovered —
 * losing it means minting another. That is worth stating plainly at the moment
 * it can still be acted on, rather than discovered later.
 */
function MintedSecret({ minted }: { minted: MintedApiKey }) {
  const [copied, setCopied] = useState<boolean | null>(null);

  const copy = async () => {
    try {
      await navigator.clipboard.writeText(minted.secret);
      setCopied(true);
    } catch {
      // Denied permission, or no clipboard API on an insecure origin. The
      // secret is selectable text either way, so this is a downgrade rather
      // than a failure — but it has to say so, or the reader closes the dialog
      // believing they have it.
      setCopied(false);
    }
  };

  return (
    <>
      <Notice tone="warn" title={m["overview.minted.copy_title"]()}>
        {m["overview.minted.copy_body"]()}
      </Notice>

      <div className={SECRET_ROW}>
        <code className={SECRET}>{minted.secret}</code>
        <Button onClick={copy}>
          {copied ? m["overview.minted.copied"]() : m["overview.minted.copy"]()}
        </Button>
      </div>
      {copied === false && (
        // Warn ink, small — a caveat about the fallback, not the failure itself.
        <p className="text-body text-warn">{m["overview.minted.clipboard"]()}</p>
      )}

      <p className={SECRET_DETAIL}>
        <Message
          message={minted.expires_at ? m["overview.minted.detail_expires"] : m["overview.minted.detail_never"]}
          markup={{ code: (children) => <code className={CODE}>{children}</code> }}
          inputs={{
            group: minted.billing_group?.name ?? m["overview.minted.group_fallback"](),
            date: minted.expires_at ? formatDate(minted.expires_at) : "",
          }}
        />
      </p>

      <OpencodeSetup heading={m["overview.minted.next_heading"]()} />
    </>
  );
}

/**
 * Revoking is permanent and immediate, so it asks first.
 *
 * The key is not deleted — the usage ledger references it, and removing it
 * would turn historical spend into an unattributable row. So the bill is
 * unaffected, which is the part worth saying out loud: the reader is about to
 * break something running, not about to lose their records.
 */
function RevokeKeyDialog({ apiKey, onClose }: { apiKey: ApiKey | null; onClose: () => void }) {
  const revoke = useRevokeKey();
  const toast = useOptionalToast();

  return (
    <Dialog
      open={apiKey !== null}
      title={m["overview.revoke.title"]({
        name: apiKey?.name || m["overview.revoke.title_fallback"](),
      })}
      onClose={onClose}
      footer={
        <>
          <Button onClick={onClose}>{m["overview.revoke.cancel"]()}</Button>
          <Button
            variant="danger"
            busy={revoke.isPending}
            onClick={() =>
              apiKey &&
              revoke.mutate(apiKey.id, {
                onSuccess: () => {
                  toast?.add({ title: m["overview.revoke.toast_done"](), type: "success" });
                  onClose();
                },
                onError: () =>
                  toast?.add({ title: m["overview.revoke.toast_failed"](), type: "error" }),
              })
            }
          >
            {m["overview.revoke.confirm"]()}
          </Button>
        </>
      }
    >
      {revoke.error ? (
        <Notice tone="danger">
          {/* i18n: server text — the API's own error message. */}
          {revoke.error instanceof Error ? revoke.error.message : m["overview.unknown_error"]()}
        </Notice>
      ) : null}
      <p>
        <Message
          message={m["overview.revoke.body"]}
          markup={{ code: (children) => <code className={CODE}>{children}</code> }}
          inputs={{ prefix: apiKey?.prefix ?? "" }}
        />
      </p>
      <p className={SECRET_DETAIL}>{m["overview.revoke.spend_note"]()}</p>
    </Dialog>
  );
}

/**
 * Deleting is permanent, so it asks first, and it says what is actually lost.
 *
 * Not the spend: usage rows carry the person and group they were billed to and
 * keep them. What is lost is this key's label on those rows in the per-key
 * breakdown, and any last-used date — the audit trail is the part a delete
 * trades away for a shorter list, and the dialog is where that trade is named
 * rather than discovered afterwards.
 */
function DeleteKeyDialog({ apiKey, onClose }: { apiKey: ApiKey | null; onClose: () => void }) {
  const del = useDeleteKey();
  const toast = useOptionalToast();

  return (
    <Dialog
      open={apiKey !== null}
      title={m["overview.delete.title"]({
        name: apiKey?.name || m["overview.delete.title_fallback"](),
      })}
      onClose={onClose}
      footer={
        <>
          <Button onClick={onClose}>{m["overview.delete.cancel"]()}</Button>
          <Button
            variant="danger"
            busy={del.isPending}
            onClick={() =>
              apiKey &&
              del.mutate(apiKey.id, {
                onSuccess: () => {
                  toast?.add({ title: m["overview.delete.toast_done"](), type: "success" });
                  onClose();
                },
                onError: () =>
                  toast?.add({ title: m["overview.delete.toast_failed"](), type: "error" }),
              })
            }
          >
            {m["overview.delete.confirm"]()}
          </Button>
        </>
      }
    >
      {del.error ? (
        <Notice tone="danger">
          {/* i18n: server text — the API's own error message. */}
          {del.error instanceof Error ? del.error.message : m["overview.unknown_error"]()}
        </Notice>
      ) : null}
      <p>
        <Message
          message={m["overview.delete.body"]}
          markup={{ code: (children) => <code className={CODE}>{children}</code> }}
          inputs={{ prefix: apiKey?.prefix ?? "" }}
        />
      </p>
      <p className={SECRET_DETAIL}>{m["overview.delete.history_note"]()}</p>
    </Dialog>
  );
}

const keyColumns = (
  onRevoke: (key: ApiKey) => void,
  onDelete: (key: ApiKey) => void,
): Column<ApiKey>[] => [
  {
    key: "name",
    header: m["overview.key_column.name"](),
    render: (key) => key.name || <em>{m["overview.unnamed"]()}</em>,
  },
  {
    key: "prefix",
    header: m["overview.key_column.prefix"](),
    render: (key) => <code className={CODE}>{key.prefix}</code>,
  },
  {
    key: "group",
    header: m["overview.key_column.billing_group"](),
    render: (key) => key.billing_group?.name ?? m["overview.key_column.group_fallback"](),
  },
  {
    key: "status",
    header: m["overview.key_column.status"](),
    render: (key) =>
      key.revoked_at ? (
        <Badge tone="danger">{m["overview.key_badge.revoked"]()}</Badge>
      ) : isExpired(key) ? (
        <Badge tone="warn">{m["overview.key_badge.expired"]()}</Badge>
      ) : (
        <Badge tone="ok">{m["overview.key_badge.active"]()}</Badge>
      ),
  },
  {
    key: "used",
    header: m["overview.key_column.last_used"](),
    render: (key) => (key.last_used_at ? formatDate(key.last_used_at) : m["overview.key_column.never"]()),
  },
  {
    key: "actions",
    header: "",
    // Revoke is offered only while it means something; delete is offered
    // always, because pruning dead keys out of the list is the point of
    // having it — a revoked entry is exactly the clutter in question.
    render: (key) => (
      <div className={ROW_ACTIONS}>
        {key.revoked_at ? null : (
          <Button variant="ghost" className="text-danger" onClick={() => onRevoke(key)}>
            {m["overview.key_action.revoke"]()}
          </Button>
        )}
        <Button variant="ghost" className="text-danger" onClick={() => onDelete(key)}>
          {m["overview.key_action.delete"]()}
        </Button>
      </div>
    ),
  },
];

function isExpired(key: ApiKey): boolean {
  return key.expires_at !== null && new Date(key.expires_at) < new Date();
}

function breakdownNoun(groupBy: string): string {
  return (
    {
      model: m["overview.heading.model"](),
      day: m["overview.heading.day"](),
      group: m["overview.heading.group"](),
      api_key: m["overview.heading.api_key"](),
    }[groupBy] ?? m["overview.heading.other"]()
  );
}

/** The same nouns in the case the sentences need; see AdminReports.tsx. */
function labelFor(groupBy: string): string {
  return (
    {
      model: m["overview.heading_lower.model"](),
      day: m["overview.heading_lower.day"](),
      group: m["overview.heading_lower.group"](),
      api_key: m["overview.heading_lower.api_key"](),
    }[groupBy] ?? m["overview.heading_lower.other"]()
  );
}
