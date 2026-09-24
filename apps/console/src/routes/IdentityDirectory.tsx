/**
 * Directory sync and the bundled Authelia's users (ADR 0088).
 *
 * Two dialogs opened from a provider row. The Directory dialog is where an
 * adapter is chosen and credentialed, where the forced first dry run is looked
 * at and confirmed, and where later runs are read — each run's own list of
 * changes, because "12 updated" is not something to approve. The Users dialog
 * is the admin interface Authelia 4.39 does not have: the gateway edits its
 * users file, and a new or reset password is shown once and stored nowhere.
 */

import { Badge, Button, Dialog, Input, Notice, Select, Spinner } from "@llmp/ui";
import { useState } from "react";
import {
  useAutheliaUsers,
  useConfirmSync,
  useCreateAutheliaUser,
  useDeleteAutheliaUser,
  useDirectory,
  useMintScimToken,
  usePreassignGroups,
  useResetAutheliaPassword,
  useRunSync,
  useSetSyncConfig,
  useSyncRuns,
  useTestSync,
  useUpdateAutheliaUser,
  useUpdateIdentityProvider,
} from "../lib/admin";
import type { IdentityProvider, SyncAdapter, SyncRun } from "../lib/types";
import { useOptionalToast } from "../lib/toast";
import { CODE, DETAIL_LABEL, FORM } from "../lib/layout";

const ADAPTER_LABEL: Record<SyncAdapter, string> = {
  none: "None — people appear at their first sign-in",
  authelia_file: "Authelia users file (the bundled directory)",
  keycloak_admin: "Keycloak admin API (a service-account client)",
  scim: "SCIM 2.0 push — the identity provider sends changes here",
};

function errorText(caught: unknown, fallback: string): string {
  return caught instanceof Error ? caught.message : fallback;
}

function RunSummary({ run }: { run: SyncRun }) {
  const tone = run.status === "ok" ? "ok" : run.status === "failed" ? "danger" : "warn";
  return (
    <div className="rounded-md border border-line p-3 text-sm">
      <div className="flex flex-wrap items-center gap-2">
        <Badge tone={tone}>{run.status.replace("_", " ")}</Badge>
        {run.dry_run && <Badge tone="neutral">dry run</Badge>}
        <Badge tone="neutral">{run.trigger}</Badge>
        <span className="text-ink-faint">{run.started_at ? new Date(run.started_at).toLocaleString() : ""}</span>
      </div>
      <div className="mt-1 text-ink-muted">
        seen {run.seen} · created {run.created} · linked {run.linked} · groups changed {run.updated} ·
        deactivated {run.deactivated} · reactivated {run.reactivated}
      </div>
      {run.error && <div className="mt-1 text-danger">{run.error}</div>}
      {run.changes.length > 0 && (
        <details className="mt-2">
          <summary className="cursor-pointer text-ink-muted">{run.changes.length} change(s)</summary>
          <ul className="mt-1 max-h-48 overflow-auto font-mono text-xs">
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
  const update = useUpdateIdentityProvider();
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
  const [kcClientId, setKcClientId] = useState("");
  const [kcSecret, setKcSecret] = useState("");
  const [scim, setScim] = useState<{ token: string; endpoint: string } | null>(null);
  const [preassignDraft, setPreassignDraft] = useState<Record<string, string>>({});

  if (!provider) return null;
  const chosen = adapter ?? provider.sync_adapter;
  const options: SyncAdapter[] = ["none", ...provider.capabilities.adapters];
  const lastRun = runs.data?.[0];
  const pull = chosen === "authelia_file" || chosen === "keycloak_admin";

  const saveAdapter = () =>
    update.mutate(
      {
        id,
        sync_adapter: chosen,
        sync_interval_minutes: intervalMinutes === null ? provider.sync_interval_minutes : Number(intervalMinutes),
      },
      {
        onSuccess: () => toast?.add({ title: "Sync settings saved", type: "success" }),
        onError: (caught) => toast?.add({ title: errorText(caught, "Could not save"), type: "error" }),
      },
    );

  const close = () => {
    setAdapter(null);
    setIntervalMinutes(null);
    setKcClientId("");
    setKcSecret("");
    setScim(null);
    setPreassignDraft({});
    onClose();
  };

  return (
    <Dialog open={open} title={`Directory — ${provider.name}`} onClose={close} footer={<Button onClick={close}>Close</Button>}>
      <div className={FORM}>
        {!provider.capabilities.adapters.length && (
          <Notice tone="info" title="Just-in-time only">
            A {provider.kind} provider offers no way to list its users: OIDC itself has no listing
            API. People appear here when they first sign in.
          </Notice>
        )}
        <Select
          label="How this directory tells us about people"
          value={chosen}
          onChange={(e) => setAdapter(e.target.value as SyncAdapter)}
        >
          {options.map((option) => (
            <option key={option} value={option}>
              {ADAPTER_LABEL[option]}
            </option>
          ))}
        </Select>
        {pull && (
          <Input
            label="Pull every (minutes)"
            type="number"
            value={intervalMinutes ?? String(provider.sync_interval_minutes)}
            onChange={(e) => setIntervalMinutes(e.target.value)}
            hint="0 means only when run from here."
          />
        )}
        <div>
          <Button
            variant="primary"
            busy={update.isPending}
            disabled={chosen === provider.sync_adapter && intervalMinutes === null}
            onClick={saveAdapter}
          >
            Save
          </Button>
        </div>

        {provider.sync_adapter === "keycloak_admin" && (
          <div className="flex flex-col gap-2">
            <div className={DETAIL_LABEL}>Service-account client (view-users, query-groups)</div>
            <Input label="Client ID" value={kcClientId} onChange={(e) => setKcClientId(e.target.value)} />
            <Input
              label="Client secret"
              type="password"
              value={kcSecret}
              onChange={(e) => setKcSecret(e.target.value)}
              hint="Stored encrypted, never shown again. Saving it makes the next run a dry run."
            />
            <div>
              <Button
                disabled={!kcClientId || !kcSecret}
                busy={setConfig.isPending}
                onClick={() =>
                  setConfig.mutate(
                    { id, config: { client_id: kcClientId, client_secret: kcSecret } },
                    { onSuccess: () => toast?.add({ title: "Credentials saved", type: "success" }) },
                  )
                }
              >
                Save credentials
              </Button>
            </div>
          </div>
        )}

        {provider.sync_adapter === "scim" && (
          <div className="flex flex-col gap-2">
            <div className={DETAIL_LABEL}>SCIM endpoint and token</div>
            <p className="text-sm text-ink-muted">
              Give the identity provider's provisioning settings this endpoint and a token. Map the
              person's object id to <span className="font-mono">externalId</span> so accounts can
              exist before their first sign-in.
            </p>
            {scim ? (
              <Notice tone="warn" title="Shown once — copy it now">
                <div className={CODE}>{scim.endpoint}</div>
                <div className={CODE}>{scim.token}</div>
              </Notice>
            ) : (
              <div>
                <Button busy={mint.isPending} onClick={() => mint.mutate(id, { onSuccess: setScim })}>
                  Generate token (replaces the previous one)
                </Button>
              </div>
            )}
          </div>
        )}

        {provider.sync_adapter !== "none" && provider.sync_adapter !== "scim" && (
          <div className="flex flex-col gap-2">
            <div className={DETAIL_LABEL}>Runs</div>
            {!provider.sync_confirmed && (
              <Notice tone="info" title="The first run is a dry run">
                Nothing changes until you have looked at what the adapter would do and confirmed it.
                A run that would deactivate more than max(5, 10%) of this directory's accounts always
                stops for confirmation.
              </Notice>
            )}
            <div className="flex flex-wrap gap-2">
              <Button
                busy={test.isPending}
                onClick={() =>
                  test.mutate(id, {
                    onSuccess: (result) =>
                      toast?.add({ title: `The adapter sees ${result.total} people`, type: "success" }),
                    onError: (caught) => toast?.add({ title: errorText(caught, "Test failed"), type: "error" }),
                  })
                }
              >
                Test connection
              </Button>
              <Button busy={run.isPending} onClick={() => run.mutate({ id, dryRun: true })}>
                Dry run
              </Button>
              {!provider.sync_confirmed ? (
                <Button
                  variant="primary"
                  disabled={!lastRun || !lastRun.dry_run || lastRun.status !== "ok"}
                  busy={confirm.isPending}
                  onClick={() => confirm.mutate(id)}
                >
                  Confirm and enable
                </Button>
              ) : (
                <Button variant="primary" busy={run.isPending} onClick={() => run.mutate({ id, dryRun: false })}>
                  Run now
                </Button>
              )}
              {lastRun?.status === "needs_confirmation" && (
                <Button
                  className="text-danger"
                  onClick={() => run.mutate({ id, dryRun: false, force: true })}
                >
                  Apply anyway ({lastRun.deactivated} deactivations)
                </Button>
              )}
            </div>
            {runs.isPending ? (
              <Spinner label="Loading runs" />
            ) : (
              (runs.data ?? []).slice(0, 5).map((r) => <RunSummary key={r.id} run={r} />)
            )}
          </div>
        )}

        {provider.sync_adapter !== "none" && (
          <div className="flex flex-col gap-2">
            <div className={DETAIL_LABEL}>Not signed in yet</div>
            {(directory.data ?? []).filter((p) => !p.user_id && p.present).length === 0 ? (
              <span className="text-sm text-ink-faint">Everyone the directory lists has an account.</span>
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
                      label="Groups at first sign-in"
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
                      Save
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

export function AutheliaUsersDialog({
  provider,
  onClose,
}: {
  provider: IdentityProvider | null;
  onClose: () => void;
}) {
  const open = provider !== null;
  const id = provider?.id ?? "";
  const toast = useOptionalToast();
  const users = useAutheliaUsers(id, open);
  const create = useCreateAutheliaUser();
  const update = useUpdateAutheliaUser();
  const reset = useResetAutheliaPassword();
  const remove = useDeleteAutheliaUser();
  const [username, setUsername] = useState("");
  const [email, setEmail] = useState("");
  const [displayName, setDisplayName] = useState("");
  const [groups, setGroups] = useState("users");
  const [secret, setSecret] = useState<{ username: string; password: string } | null>(null);

  if (!provider) return null;
  const fail = (caught: unknown) => toast?.add({ title: errorText(caught, "The directory refused"), type: "error" });

  return (
    <Dialog
      open={open}
      title={`People in ${provider.name}`}
      onClose={() => {
        setSecret(null);
        onClose();
      }}
      footer={<Button onClick={() => { setSecret(null); onClose(); }}>Close</Button>}
    >
      <div className={FORM}>
        {secret && (
          <Notice tone="warn" title={`Password for ${secret.username} — shown once`}>
            <div className={CODE}>{secret.password}</div>
            Hand it over now; it is stored only as a digest.
          </Notice>
        )}
        {users.isPending ? (
          <Spinner label="Loading people" />
        ) : users.error ? (
          <Notice tone="danger" title="Could not read the users file">
            {errorText(users.error, "Unknown error.")}
          </Notice>
        ) : (
          (users.data ?? []).map((user) => (
            <div key={user.username} className="flex flex-wrap items-center justify-between gap-2 rounded-md border border-line p-3">
              <div className="min-w-0">
                <span className="font-medium">{user.username}</span>{" "}
                {user.disabled && <Badge tone="neutral">disabled</Badge>}
                <div className="text-xs text-ink-faint">
                  {user.email} · {user.groups.join(", ")}
                </div>
              </div>
              <div className="flex flex-wrap gap-2">
                <Button
                  variant="ghost"
                  onClick={() => update.mutate({ id, username: user.username, disabled: !user.disabled }, { onError: fail })}
                >
                  {user.disabled ? "Enable" : "Disable"}
                </Button>
                <Button
                  variant="ghost"
                  onClick={() =>
                    reset.mutate(
                      { id, username: user.username },
                      { onSuccess: (r) => setSecret({ username: user.username, password: r.password }), onError: fail },
                    )
                  }
                >
                  Reset password
                </Button>
                <Button
                  variant="ghost"
                  className="text-danger"
                  onClick={() => remove.mutate({ id, username: user.username }, { onError: fail })}
                >
                  Delete
                </Button>
              </div>
            </div>
          ))
        )}
        <div className={DETAIL_LABEL}>Add a person</div>
        <Input label="Login" value={username} onChange={(e) => setUsername(e.target.value)} />
        <Input label="Email" value={email} onChange={(e) => setEmail(e.target.value)} />
        <Input label="Display name" value={displayName} onChange={(e) => setDisplayName(e.target.value)} />
        <Input
          label="Groups"
          value={groups}
          onChange={(e) => setGroups(e.target.value)}
          hint="Comma-separated. They reach the gateway as the groups claim."
        />
        <div>
          <Button
            variant="primary"
            disabled={!username || !email}
            busy={create.isPending}
            onClick={() =>
              create.mutate(
                {
                  id,
                  username: username.trim(),
                  email: email.trim(),
                  display_name: displayName.trim(),
                  groups: groups.split(",").map((g) => g.trim()).filter(Boolean),
                },
                {
                  onSuccess: (result) => {
                    setSecret({ username: result.user.username, password: result.password });
                    setUsername("");
                    setEmail("");
                    setDisplayName("");
                  },
                  onError: fail,
                },
              )
            }
          >
            Add person
          </Button>
        </div>
      </div>
    </Dialog>
  );
}
