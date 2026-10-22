/**
 * User sync, from a provider row (ADR 0088) — optional, and external-only.
 *
 * Keeps this console's user list in step with an identity provider: an
 * adapter is chosen and credentialed here, the forced first dry run is looked
 * at and confirmed here, and later runs are read here — each run's own list
 * of changes, because "12 updated" is not something to approve. Preassigning
 * groups and deactivating people the directory no longer lists are the same
 * decision, not a separate screen.
 *
 * The bundled Authelia never opens this dialog: its people are created,
 * managed and removed on the Users page, so there is nothing for an adapter
 * to pull or confirm — its row carries the seeded `authelia_file` adapter
 * purely as inert state, which the Providers row therefore does not show.
 */

import { Badge, Button, Dialog, Input, Notice, Select, Spinner } from "@llmp/ui";
import { useState } from "react";
import {
  useConfirmSync,
  useDirectory,
  useMintScimToken,
  usePreassignGroups,
  useRunSync,
  useSetSyncConfig,
  useSyncRuns,
  useTestSync,
} from "../lib/admin";
import type { IdentityProvider, SyncAdapter, SyncRun } from "../lib/types";
import { useOptionalToast } from "../lib/toast";
import { CODE, DETAIL_LABEL, FORM } from "../lib/layout";
import { formatDateTime } from "../lib/format";
import { Message } from "../components/Message";
import { m } from "../paraglide/messages.js";

const ADAPTER_LABEL: Record<SyncAdapter, () => string> = {
  none: m["identity_directory.adapter.none"],
  authelia_file: m["identity_directory.adapter.authelia_file"],
  scim: m["identity_directory.adapter.scim"],
};

function errorText(caught: unknown, fallback: string): string {
  return caught instanceof Error ? caught.message : fallback;
}

function RunSummary({ run }: { run: SyncRun }) {
  const tone = run.status === "ok" ? "ok" : run.status === "failed" ? "danger" : "warn";
  return (
    <div className="rounded-md border border-line p-3 text-sm">
      <div className="flex flex-wrap items-center gap-2">
        {/* i18n: server text — `status` and `trigger` are the API's enum values. */}
        <Badge tone={tone}>{run.status.replace("_", " ")}</Badge>
        {run.dry_run && <Badge tone="neutral">{m["identity_directory.run.dry_run"]()}</Badge>}
        <Badge tone="neutral">{run.trigger}</Badge>
        <span className="text-ink-faint">{run.started_at ? formatDateTime(run.started_at) : ""}</span>
      </div>
      <div className="mt-1 text-ink-muted">
        {m["identity_directory.run.counts"]({
          seen: run.seen,
          created: run.created,
          linked: run.linked,
          updated: run.updated,
          deactivated: run.deactivated,
          reactivated: run.reactivated,
        })}
      </div>
      {/* i18n: server text — the run's own error message. */}
      {run.error && <div className="mt-1 text-danger">{run.error}</div>}
      {run.changes.length > 0 && (
        <details className="mt-2">
          <summary className="cursor-pointer text-ink-muted">
            {m["identity_directory.run.change_count"]({ count: run.changes.length })}
          </summary>
          <ul className="mt-1 max-h-48 overflow-auto font-mono text-xs">
            {/* i18n: server text — the change kind, the person and the field
                names come from the API, which is where an operator looks them up. */}
            {run.changes.map((change, index) => (
              <li key={index}>
                {change.change} · {change.who}
                {Object.entries(change)
                  .filter(([key]) => key !== "change" && key !== "who")
                  .map(([key, value]) => ` · ${key}: ${Array.isArray(value) ? value.join(", ") : String(value)}`)
                  .join("")}
              </li>
            ))}
          </ul>
        </details>
      )}
    </div>
  );
}

export function DirectoryDialog({
  provider,
  onClose,
}: {
  provider: IdentityProvider | null;
  onClose: () => void;
}) {
  const open = provider !== null;
  const id = provider?.id ?? "";
  const toast = useOptionalToast();
  const setConfig = useSetSyncConfig();
  const test = useTestSync();
  const run = useRunSync();
  const confirm = useConfirmSync();
  const mint = useMintScimToken();
  const preassign = usePreassignGroups();
  const runs = useSyncRuns(id, open);
  const directory = useDirectory(id, open && provider?.sync_adapter !== "none");

  const [adapter, setAdapter] = useState<SyncAdapter | null>(null);
  const [intervalMinutes, setIntervalMinutes] = useState<string | null>(null);
  const [scim, setScim] = useState<{ token: string; endpoint: string } | null>(null);
  const [preassignDraft, setPreassignDraft] = useState<Record<string, string>>({});

  if (!provider) return null;
  const chosen = adapter ?? provider.sync_adapter;
  const options: SyncAdapter[] = ["none", ...provider.capabilities.adapters];
  const lastRun = runs.data?.[0];
  const pull = chosen === "authelia_file";

  const saveAdapter = () =>
    setConfig.mutate(
      {
        id,
        sync_adapter: chosen,
        sync_interval_minutes:
          intervalMinutes === null ? provider.sync_interval_minutes : Number(intervalMinutes),
      },
      {
        onSuccess: () =>
          toast?.add({ title: m["identity_directory.settings_saved"](), type: "success" }),
        onError: (caught) =>
          // i18n: server text — the gateway's own error message.
          toast?.add({ title: errorText(caught, m["identity_directory.save_failed"]()), type: "error" }),
      },
    );

  const close = () => {
    setAdapter(null);
    setIntervalMinutes(null);
    setScim(null);
    setPreassignDraft({});
    onClose();
  };

  return (
    <Dialog
      open={open}
      title={m["identity_directory.title"]({ provider: provider.name })}
      onClose={close}
      footer={<Button onClick={close}>{m["identity_directory.close"]()}</Button>}
    >
      <div className={FORM}>
        <p className="text-sm text-ink-muted">{m["identity_directory.intro"]()}</p>
        {!provider.capabilities.adapters.length && (
          <Notice tone="info" title={m["identity_directory.just_in_time.title"]()}>
            {m["identity_directory.just_in_time.body"]({ kind: provider.kind })}
          </Notice>
        )}
        <Select
          label={m["identity_directory.adapter.label"]()}
          value={chosen}
          onChange={(e) => setAdapter(e.target.value as SyncAdapter)}
        >
          {options.map((option) => (
            <option key={option} value={option}>
              {ADAPTER_LABEL[option]()}
            </option>
          ))}
        </Select>
        {pull && (
          <Input
            label={m["identity_directory.pull_every.label"]()}
            type="number"
            value={intervalMinutes ?? String(provider.sync_interval_minutes)}
            onChange={(e) => setIntervalMinutes(e.target.value)}
            hint={m["identity_directory.pull_every.hint"]()}
          />
        )}
        <div>
          <Button
            variant="primary"
            busy={setConfig.isPending}
            disabled={chosen === provider.sync_adapter && intervalMinutes === null}
            onClick={saveAdapter}
          >
            {m["identity_directory.save"]()}
          </Button>
        </div>

        {provider.sync_adapter === "scim" && (
          <div className="flex flex-col gap-2">
            <div className={DETAIL_LABEL}>{m["identity_directory.scim.detail"]()}</div>
            <p className="text-sm text-ink-muted">
              <Message
                message={m["identity_directory.scim.body"]}
                markup={{ code: (children) => <span className="font-mono">{children}</span> }}
              />
            </p>
            {scim ? (
              <Notice tone="warn" title={m["identity_directory.scim.once_title"]()}>
                <div className={CODE}>{scim.endpoint}</div>
                <div className={CODE}>{scim.token}</div>
              </Notice>
            ) : (
              <div>
                <Button busy={mint.isPending} onClick={() => mint.mutate(id, { onSuccess: setScim })}>
                  {m["identity_directory.scim.generate"]()}
                </Button>
              </div>
            )}
          </div>
        )}

        {provider.sync_adapter !== "none" && provider.sync_adapter !== "scim" && (
          <div className="flex flex-col gap-2">
            <div className={DETAIL_LABEL}>{m["identity_directory.runs.detail"]()}</div>
            {!provider.sync_confirmed && (
              <Notice tone="info" title={m["identity_directory.runs.first_run_title"]()}>
                {m["identity_directory.runs.first_run_body"]()}
              </Notice>
            )}
            <div className="flex flex-wrap gap-2">
              <Button
                busy={test.isPending}
                onClick={() =>
                  test.mutate(id, {
                    onSuccess: (result) =>
                      toast?.add({
                        title: m["identity_directory.runs.sees"]({ people: result.total }),
                        type: "success",
                      }),
                    onError: (caught) =>
                      // i18n: server text — the gateway's own error message.
                      toast?.add({
                        title: errorText(caught, m["identity_directory.runs.test_failed"]()),
                        type: "error",
                      }),
                  })
                }
              >
                {m["identity_directory.runs.test"]()}
              </Button>
              <Button busy={run.isPending} onClick={() => run.mutate({ id, dryRun: true })}>
                {m["identity_directory.runs.dry_run"]()}
              </Button>
              {!provider.sync_confirmed ? (
                <Button
                  variant="primary"
                  disabled={!lastRun || !lastRun.dry_run || lastRun.status !== "ok"}
                  busy={confirm.isPending}
                  onClick={() => confirm.mutate(id)}
                >
                  {m["identity_directory.runs.confirm"]()}
                </Button>
              ) : (
                <Button variant="primary" busy={run.isPending} onClick={() => run.mutate({ id, dryRun: false })}>
                  {m["identity_directory.runs.run_now"]()}
                </Button>
              )}
              {lastRun?.status === "needs_confirmation" && (
                <Button
                  className="text-danger"
                  onClick={() => run.mutate({ id, dryRun: false, force: true })}
                >
                  {m["identity_directory.runs.apply_anyway"]({ deactivations: lastRun.deactivated })}
                </Button>
              )}
            </div>
            {runs.isPending ? (
              <Spinner label={m["identity_directory.runs.loading"]()} />
            ) : (
              (runs.data ?? []).slice(0, 5).map((r) => <RunSummary key={r.id} run={r} />)
            )}
          </div>
        )}

        {provider.sync_adapter !== "none" && (
          <div className="flex flex-col gap-2">
            <div className={DETAIL_LABEL}>{m["identity_directory.pending.detail"]()}</div>
            {(directory.data ?? []).filter((p) => !p.user_id && p.present).length === 0 ? (
              <span className="text-sm text-ink-faint">
                {m["identity_directory.pending.all_accounted"]()}
              </span>
            ) : (
              (directory.data ?? [])
                .filter((p) => !p.user_id && p.present)
                .map((person) => (
                  <div key={person.id} className="flex flex-wrap items-end gap-2">
                    <div className="min-w-40 text-sm">
                      {person.username ?? person.external_id}
                      <div className="text-xs text-ink-faint">{person.email}</div>
                    </div>
                    <Input
                      label={m["identity_directory.pending.groups_label"]()}
                      value={preassignDraft[person.id] ?? person.preassigned_groups.join(", ")}
                      onChange={(e) => setPreassignDraft((d) => ({ ...d, [person.id]: e.target.value }))}
                    />
                    <Button
                      variant="ghost"
                      onClick={() =>
                        preassign.mutate({
                          id,
                          entryId: person.id,
                          groups: (preassignDraft[person.id] ?? "").split(",").map((g) => g.trim()).filter(Boolean),
                        })
                      }
                    >
                      {m["identity_directory.save"]()}
                    </Button>
                  </div>
                ))
            )}
          </div>
        )}
      </div>
    </Dialog>
  );
}
