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
  Table,
  } from "@llmp/ui";
import type { Column } from "@llmp/ui";
import { useState } from "react";
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
  const [minting, setMinting] = useState(false);
  const [revoking, setRevoking] = useState<ApiKey | null>(null);
  const [deleting, setDeleting] = useState<ApiKey | null>(null);

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
      <PageHeader        title="Overview"
        subtitle="Where you stand this period, and the keys you spend with."
        /* "Overview", not "Your usage" — that name now belongs to the reports
            page next to it in the header, and two screens wearing it is worse
            than either name being imperfect. */
        actions={
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
        }
      />
      {/* Which source tree this UI was built from, muted under the header so a
          stale deploy is visible where administrators look. Baked at build
          time (Dockerfile CONSOLE_BUILD_SHA); "unknown" means built by hand
          without it, and the gateway's startup log still says which files are
          actually served. */}
      <p className={`${MUTED} -mt-2 text-xs`}>
        Console build {import.meta.env.VITE_CONSOLE_BUILD_SHA ?? "unknown"}
      </p>

      <Card>
        {report.isPending && <Spinner label="Loading your usage" />}
        {report.error ? (
          <Notice tone="danger" title="Could not load your usage">
            {report.error instanceof Error ? report.error.message : "Unknown error."}
          </Notice>
        ) : null}
        {report.data && (
          <>
            <div className={STATS}>
              <Stat
                label={`Spend · ${report.data.period.label}`}
                value={
                  <Money
                    amount={report.data.totals.cost}
                    currency={report.data.currency}
                    maxDecimals={SPEND_DECIMALS}
                  />
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

          </>
        )}
      </Card>

      <QuotaCard currency={report.data?.currency ?? "EUR"} />

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

      <Card
        title="API keys"
        flush
        description="The secret is shown once, at creation."
        actions={
          <Button variant="primary" onClick={() => setMinting(true)}>
            New key
          </Button>
        }
      >
        {keys.isPending ? (
          <Spinner />
        ) : (
          <Table
            columns={keyColumns(setRevoking, setDeleting)}
            rows={keys.data?.items ?? []}
            rowKey={(key) => key.id}
            empty="No API keys."
          />
        )}
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
      <Card title="Quotas">
        <Notice tone="danger" title="Could not load your quotas">
          {limits.error instanceof Error ? limits.error.message : "Unknown error."}
        </Notice>
      </Card>
    );
  }

  if (limits.isPending) {
    return (
      <Card title="Quotas">
        <Spinner label="Loading your quotas" />
      </Card>
    );
  }

  const rules = limits.data?.items ?? [];
  if (rules.length === 0) {
    return (
      <Card title="Quotas" description="No limit applies to you.">
        {/* Said plainly rather than left blank: an empty card reads as a screen
            that failed to load, and "no limit" is a fact worth stating. */}
      </Card>
    );
  }

  return (
    <Card title="Quotas" description="Every ceiling that applies to you. All of them must pass.">
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
        <Badge>{SCOPE_LABEL[rule.scope] ?? rule.scope}</Badge>
      </div>
      {used === null ? (
        <p className={QUOTA_DETAIL}>
          Consumption is unavailable — the counter store could not be reached.
        </p>
      ) : (
        <>
          <Meter value={used} limit={limit} label={`${rule.name}, ${rule.window_label}`} />
          <p className={QUOTA_DETAIL}>
            {rule.metric === "cost" ? (
              <>
                <Money amount={rule.current_value ?? "0"} currency={currency} /> of{" "}
                <Money amount={rule.limit_value} currency={currency} />
              </>
            ) : (
              `${used.toLocaleString()} of ${limit.toLocaleString()} ${unitFor(rule.metric)}`
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
                      toast?.add({ title: "Notices saved", type: "success" });
                      setEditing(false);
                    },
                    onError: () =>
                      toast?.add({ title: "Could not save the notices", type: "error" }),
                  },
                );
              }}
            >
              <input
                className="w-40 rounded-md border border-line bg-surface px-2 py-1 text-sm"
                value={value}
                onChange={(event) => setValue(event.target.value)}
                placeholder="e.g. 50, 80, 95"
                aria-label={`Notice thresholds for ${rule.name}`}
                autoFocus
              />
              <Button variant="ghost" onClick={() => setEditing(false)}>
                Cancel
              </Button>
              <Button type="submit" busy={thresholds.isPending}>
                Save
              </Button>
            </form>
          ) : (
            <button
              type="button"
              onClick={() => setEditing(true)}
              className="cursor-pointer text-left text-xs text-ink-faint underline-offset-2 hover:text-ink-muted hover:underline"
            >
              {(rule.notification_thresholds ?? []).length > 0
                ? `Email me at ${(
                    rule.notification_thresholds ?? []
                  ).join("%, ")}% — change`
                : "Email me when this reaches…"}
            </button>
          )}
        </>
      )}
    </div>
  );
}

const SCOPE_LABEL: Record<string, string> = {
  global: "Everyone",
  group: "Your group",
  user: "You",
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
          toast?.add({ title: "API key created", type: "success" });
        },
        onError: () => toast?.add({ title: "Could not create the key", type: "error" }),
      },
    );

  return (
    <Dialog
      open={open}
      title={minted ? "Your new API key" : "New API key"}
      onClose={close}
      footer={
        minted ? (
          <Button variant="primary" onClick={close}>
            Done
          </Button>
        ) : (
          <>
            <Button onClick={close}>Cancel</Button>
            <Button variant="primary" busy={mint.isPending} onClick={submit}>
              Create
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
              {mint.error instanceof Error ? mint.error.message : "Unknown error."}
            </Notice>
          ) : null}

          <Input
            label="Name"
            value={name}
            onChange={(event) => setName(event.target.value)}
            placeholder="laptop, CI, notebook"
            hint="For your own reference. Usage is reported per key."
          />

          <Select
            label="Billing group"
            value={groupId}
            onChange={(event) => setGroupId(event.target.value)}
            hint="Which group's budget this key spends from."
          >
            {/* Empty means "resolve at request time", so the key follows a later
                change of default rather than pinning today's answer. */}
            <option value="">
              My default group
              {me.default_billing_group ? ` — currently ${me.default_billing_group.name}` : ""}
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
            <Notice tone="warn">
              You have no default billing group. Choose a group above.
            </Notice>
          )}

          <Select
            label="Expires"
            value={expiry}
            onChange={(event) => setExpiry(event.target.value)}
            hint="An expiring key fails closed on its own."
          >
            <option value="">Never</option>
            <option value="30">In 30 days</option>
            <option value="90">In 90 days</option>
            <option value="365">In a year</option>
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
      <Notice tone="warn" title="Copy it now">
        The gateway keeps a hash, not the key, so it cannot be shown again. If you lose
        it, revoke this one and mint another.
      </Notice>

      <div className={SECRET_ROW}>
        <code className={SECRET}>{minted.secret}</code>
        <Button onClick={copy}>{copied ? "Copied" : "Copy"}</Button>
      </div>
      {copied === false && (
        // Warn ink, small — a caveat about the fallback, not the failure itself.
        <p className="text-sm text-warn">
          Could not reach the clipboard. Select the key and copy it by hand.
        </p>
      )}

      <p className={SECRET_DETAIL}>
        Send it as <code className={CODE}>Authorization: Bearer …</code>. Billed to{" "}
        {minted.billing_group?.name ?? "your default group at request time"}
        {minted.expires_at ? `, expires ${formatDate(minted.expires_at)}` : ", never expires"}.
      </p>
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
      title={`Revoke ${apiKey?.name || "this key"}`}
      onClose={onClose}
      footer={
        <>
          <Button onClick={onClose}>Cancel</Button>
          <Button
            variant="danger"
            busy={revoke.isPending}
            onClick={() =>
              apiKey &&
              revoke.mutate(apiKey.id, {
                onSuccess: () => {
                  toast?.add({ title: "Key revoked", type: "success" });
                  onClose();
                },
                onError: () =>
                  toast?.add({ title: "Could not revoke the key", type: "error" }),
              })
            }
          >
            Revoke key
          </Button>
        </>
      }
    >
      {revoke.error ? (
        <Notice tone="danger">
          {revoke.error instanceof Error ? revoke.error.message : "Unknown error."}
        </Notice>
      ) : null}
      <p>
        <code className={CODE}>{apiKey?.prefix}</code> stops working immediately and
        cannot be restored. Anything still using it starts failing to authenticate.
      </p>
      <p className={SECRET_DETAIL}>
        Usage history is unaffected: the key is revoked, never deleted.
      </p>
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
      title={`Delete ${apiKey?.name || "this key"}`}
      onClose={onClose}
      footer={
        <>
          <Button onClick={onClose}>Cancel</Button>
          <Button
            variant="danger"
            busy={del.isPending}
            onClick={() =>
              apiKey &&
              del.mutate(apiKey.id, {
                onSuccess: () => {
                  toast?.add({ title: "Key deleted", type: "success" });
                  onClose();
                },
                onError: () =>
                  toast?.add({ title: "Could not delete the key", type: "error" }),
              })
            }
          >
            Delete permanently
          </Button>
        </>
      }
    >
      {del.error ? (
        <Notice tone="danger">
          {del.error instanceof Error ? del.error.message : "Unknown error."}
        </Notice>
      ) : null}
      <p>
        <code className={CODE}>{apiKey?.prefix}</code> is removed from this list and stops
        working immediately. This cannot be undone — revoking is the reversible-looking cousin,
        and even it cannot be restored.
      </p>
      <p className={SECRET_DETAIL}>
        Past usage stays in the ledger, still attributed to you and your billing group. Those rows
        simply stop carrying this key's name in the by-key breakdown.
      </p>
    </Dialog>
  );
}

const keyColumns = (
  onRevoke: (key: ApiKey) => void,
  onDelete: (key: ApiKey) => void,
): Column<ApiKey>[] => [
  { key: "name", header: "Name", render: (key) => key.name || <em>unnamed</em> },
  {
    key: "prefix",
    header: "Prefix",
    render: (key) => <code className={CODE}>{key.prefix}</code>,
  },
  {
    key: "group",
    header: "Billing group",
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
  {
    key: "actions",
    header: "",
    // Revoke is offered only while it means something; delete is offered
    // always, because pruning dead keys out of the list is the point of
    // having it — a revoked entry is exactly the clutter in question.
    render: (key) => (
      <div className={ROW_ACTIONS}>
        {key.revoked_at ? null : (
          <Button variant="ghost" onClick={() => onRevoke(key)}>
            Revoke
          </Button>
        )}
        <Button variant="ghost" onClick={() => onDelete(key)}>
          Delete
        </Button>
      </div>
    ),
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
