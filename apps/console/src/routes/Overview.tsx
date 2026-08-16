import {
  Badge,
  Button,
  Card,
  Dialog,
  Input,
  Money,
  Notice,
  Select,
  Spinner,
  Stat,
  Table,
} from "@llmp/ui";
import type { Column } from "@llmp/ui";
import { useState } from "react";
import { downloadCsv } from "../lib/api";
import { useMintKey, useMyKeys, useMyReport, useRevokeKey } from "../lib/queries";
import type { ApiKey, Me, MintedApiKey, UsageReportRow } from "../lib/types";
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
  const [minting, setMinting] = useState(false);
  const [revoking, setRevoking] = useState<ApiKey | null>(null);

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

      <Card
        title="API keys"
        flush
        description="Keys you have minted. The secret is shown once, at creation."
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
            columns={keyColumns(setRevoking)}
            rows={keys.data?.items ?? []}
            rowKey={(key) => key.id}
            empty="You have not minted any API keys."
          />
        )}
      </Card>

      <MintKeyDialog open={minting} me={me} onClose={() => setMinting(false)} />
      <RevokeKeyDialog apiKey={revoking} onClose={() => setRevoking(null)} />
    </div>
  );
}

/**
 * Minting, and the one moment the secret exists in a form anyone can read.
 *
 * Two states in one dialog rather than two dialogs: the form, and then the
 * secret. They are the same interaction — the secret is the *result* of the
 * form, and a reader who has just clicked Create should not have to notice a
 * second window appearing somewhere to find the thing they asked for.
 */
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
      { onSuccess: setMinted },
    );

  return (
    <Dialog
      open={open}
      title={minted ? "Your new API key" : "Mint an API key"}
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
            hint="For your own reference. Usage is reported per key, so a name you
              recognise later is worth the two seconds."
          />

          <Select
            label="Bills to"
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
              You have no default billing group, so a key that resolves one at request
              time cannot be billed. Choose a group above.
            </Notice>
          )}

          <Select
            label="Expires"
            value={expiry}
            onChange={(event) => setExpiry(event.target.value)}
            hint="An expiring key fails closed on its own. A key that never expires is
              one you have to remember to revoke."
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
        This is the only time it will be shown. The gateway keeps a hash, not the key,
        so it cannot be recovered — if you lose it, revoke this one and mint another.
      </Notice>

      <div className={styles.secretRow}>
        <code className={styles.secret}>{minted.secret}</code>
        <Button onClick={copy}>{copied ? "Copied" : "Copy"}</Button>
      </div>
      {copied === false && (
        <p className={styles.copyFallback}>
          Could not reach the clipboard. Select the key above and copy it by hand.
        </p>
      )}

      <p className={styles.secretDetail}>
        Send it as <code className={styles.code}>Authorization: Bearer …</code>. Billed to{" "}
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

  return (
    <Dialog
      open={apiKey !== null}
      title={`Revoke ${apiKey?.name || "this key"}?`}
      onClose={onClose}
      footer={
        <>
          <Button onClick={onClose}>Cancel</Button>
          <Button
            variant="danger"
            busy={revoke.isPending}
            onClick={() =>
              apiKey && revoke.mutate(apiKey.id, { onSuccess: onClose })
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
        <code className={styles.code}>{apiKey?.prefix}</code> stops working immediately, and
        this cannot be undone. Anything still using it starts failing to authenticate.
      </p>
      <p className={styles.secretDetail}>
        Your usage history is unaffected — the key is revoked, never deleted, so past
        spend stays attributed to it.
      </p>
    </Dialog>
  );
}

const keyColumns = (onRevoke: (key: ApiKey) => void): Column<ApiKey>[] => [
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
  {
    key: "actions",
    header: "",
    // Nothing to offer for a key that is already dead: revoking a revoked key
    // is a no-op the API accepts, and a button that does nothing is worse than
    // no button.
    render: (key) =>
      key.revoked_at ? null : (
        <Button variant="ghost" onClick={() => onRevoke(key)}>
          Revoke
        </Button>
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
