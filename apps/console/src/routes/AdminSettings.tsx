/**
 * The Settings screen (ADR 0051): the mail server, the identity providers,
 * and the provisioning policy that decides who may become a user.
 *
 * One screen because these three answers shape the same question — how people
 * get in, and how the platform reaches them. Each section saves independently;
 * a change to the SMTP port should not have to travel with a change to the
 * provisioning rule.
 */

import {
  Badge,
  Button,
  Card,
  Dialog,
  EmptyState,
  Input,
  Notice,
  Spinner,
} from "@llmp/ui";
import { useState } from "react";
import { Link } from "react-router";
import {
  useBulkGroupCapabilities,
  useEmailSettings,
  useIdentityProviders,
  useOidcPolicy,
  usePolicyPreview,
  useRemoveIdentityProvider,
  useTestEmail,
  useUpdateOidcPolicy,
} from "../lib/admin";
import type { CapabilityMode, IdentityProvider } from "../lib/types";
import { DirectoryDialog } from "./IdentityDirectory";
import { useOptionalToast } from "../lib/toast";
import { PageHeader } from "../components/PageHeader";
import { ProvisioningPolicySection } from "./AdminIdentity";
import {
  CHIPS,
  CODE,
  DETAIL_LABEL,
  FORM,
  MUTED,
  PAGE,
  SECRET_ROW,
} from "../lib/layout";

export function AdminSettings() {
  return (
    <div className={PAGE}>
      <PageHeader
        title="Settings"
        subtitle="The mail server, the identity providers, who may become a user, and
          what new groups start with. Each section saves on its own."
      />
      <EmailCard />
      <ProvidersCard />
      <Card title="Provisioning policy">
        <ProvisioningPolicySection />
      </Card>
      <CapabilityDefaultsCard />
    </div>
  );
}

// -- email ---------------------------------------------------------------------

/**
 * Read-only (ADR 0093 §1, §14): mail is one setting now, `GATEWAY_SMTP__*`,
 * so there is nothing here for an administrator to edit — only "send test"
 * stays, to prove whatever is configured actually works.
 */
function EmailCard() {
  const email = useEmailSettings();
  const test = useTestEmail();
  const toast = useOptionalToast();

  const [testTo, setTestTo] = useState("");

  if (email.isPending) {
    return (
      <Card title="Email">
        <Spinner label="Loading the email settings" />
      </Card>
    );
  }

  return (
    <Card
      title="Email"
      description="Configured in .env; change with ./configure. Used for quota notices
        and the bundled Authelia's self-service password reset."
      actions={
        email.data ? (
          <Badge tone={email.data.enabled ? "ok" : "neutral"}>
            {email.data.enabled ? "enabled" : "disabled"}
          </Badge>
        ) : null
      }
    >
      {email.error ? (
        <Notice tone="danger" title="Could not load the email settings">
          {email.error instanceof Error
            ? email.error.message
            : "Unknown error."}
        </Notice>
      ) : email.data ? (
        <div className={FORM}>
          <div className="grid gap-4 [grid-template-columns:repeat(auto-fit,minmax(10rem,1fr))]">
            <div>
              <div className={DETAIL_LABEL}>SMTP host</div>
              <div className={CODE}>{email.data.host || "(not set)"}</div>
            </div>
            <div>
              <div className={DETAIL_LABEL}>Port</div>
              <div className={CODE}>{email.data.port}</div>
            </div>
          </div>
          <div>
            <div className={DETAIL_LABEL}>Username</div>
            <div className={CODE}>{email.data.username || "(none)"}</div>
          </div>
          <div>
            <div className={DETAIL_LABEL}>Password</div>
            <div className={CODE}>
              {email.data.has_password ? "set" : "(none)"}
            </div>
          </div>
          <div>
            <div className={DETAIL_LABEL}>From address</div>
            <div className={CODE}>{email.data.from_address || "(not set)"}</div>
          </div>
          <div className="flex flex-wrap items-end gap-2">
            <Input
              label="Send a test email to"
              value={testTo}
              onChange={(e) => setTestTo(e.target.value)}
            />
            <Button
              busy={test.isPending}
              disabled={!testTo.trim()}
              onClick={() =>
                test.mutate(testTo.trim(), {
                  onSuccess: (result) =>
                    toast?.add({
                      title: result.ok
                        ? "Test email sent"
                        : "Test email failed",
                      description: result.detail,
                      type: result.ok ? "success" : "error",
                    }),
                  onError: (caught: unknown) =>
                    toast?.add({
                      title:
                        caught instanceof Error
                          ? caught.message
                          : "Test email failed",
                      type: "error",
                    }),
                })
              }
            >
              Send test
            </Button>
          </div>
        </div>
      ) : null}
    </Card>
  );
}

// -- identity providers ---------------------------------------------------------

/**
 * The callback URL to register at the provider, shown as the name is typed.
 *
 * It has to be shown, and it has to be shown *here*. The path carries the
 * connection's own name — `/auth/callback/<name>` — so it is not something an
 * administrator can guess or read off a docs page, and the IdP rejects the
 * login unless it holds this exact string. Registering it wrong produces a
 * successful sign-in that fails on the way back, which is the worst place to
 * discover a typo.
 *
 * Built from `window.location.origin` rather than served by the API, and that
 * is the accurate source rather than a shortcut: the gateway derives the
 * redirect URI from the origin the login *arrived on*
 * (`OIDCProviderRegistry.client_for`), so the URI this deployment will actually
 * send is the origin the administrator is reading this on. A value computed on
 * the server would be whatever `PUBLIC_ORIGIN` says, which is the same thing
 * only when it is set correctly — and if it is not, this line is the fastest
 * way to notice.
 */
export function RedirectUri({ name }: { name: string }) {
  const [copied, setCopied] = useState<boolean | null>(null);
  const origin = typeof window === "undefined" ? "" : window.location.origin;
  const slug = name.trim();
  const uri = `${origin}/auth/callback/${slug || "<name>"}`;

  const copy = async () => {
    try {
      await navigator.clipboard.writeText(uri);
      setCopied(true);
    } catch {
      // No clipboard API on an insecure origin, or permission denied. The text
      // is selectable either way, so this is a downgrade rather than a failure.
      setCopied(false);
    }
  };

  return (
    <div>
      <div className={DETAIL_LABEL}>Redirect URI</div>
      <div className={SECRET_ROW}>
        <code className={CODE}>{uri}</code>
        {slug && <Button onClick={copy}>{copied ? "Copied" : "Copy"}</Button>}
      </div>
      <p className="mt-1 text-sm text-ink-muted">
        Register exactly this at the provider — never a wildcard.
      </p>
      {copied === false && (
        <p className="text-sm text-warn">
          Could not reach the clipboard; copy it by hand.
        </p>
      )}
    </div>
  );
}

/**
 * Read-only (ADR 0093 §14): the row is a projection of the environment now,
 * re-seeded at every start, so there is nothing here for an administrator to
 * create, edit or delete — `./configure` is where a deployment's provider
 * changes. What is still console-owned is user sync (the directory mirror),
 * which is a decision about *this* console's own user list, not about the
 * IdP, so `DirectoryDialog` stays reachable from here — for an external
 * enabled provider only. The bundled Authelia needs none of it (the Users
 * page manages its people outright, and the re-seed never overwrites a row's
 * `sync_adapter`), and a disabled row syncs nothing, so neither offers the
 * button or the sync badge.
 */
function ProvidersCard() {
  const providers = useIdentityProviders();
  const [directoryOf, setDirectoryOf] = useState<IdentityProvider | null>(null);
  const [removing, setRemoving] = useState<IdentityProvider | null>(null);

  const rows = providers.data ?? [];
  const active = rows.filter((p) => p.is_enabled);
  const disabled = rows.filter((p) => !p.is_enabled);

  return (
    <Card
      title="Identity providers"
      description="Configured in .env; change with ./configure. Users from different
        providers are different accounts — identity is (issuer, subject)."
    >
      {providers.isPending ? (
        <Spinner label="Loading providers" />
      ) : providers.error ? (
        <Notice tone="danger" title="Could not load the identity providers">
          {providers.error instanceof Error
            ? providers.error.message
            : "Unknown error."}
        </Notice>
      ) : rows.length === 0 ? (
        <EmptyState
          title="No identity provider is configured"
          detail="Nobody can sign in yet: set GATEWAY_OIDC__* in .env, or run ./configure."
        />
      ) : (
        <div className="flex flex-col gap-4">
          <div className="flex flex-col gap-2">
            {active.map((provider) => (
              <ProviderRow
                key={provider.id}
                provider={provider}
                onDirectory={() => setDirectoryOf(provider)}
              />
            ))}
          </div>
          {disabled.length > 0 && (
            <div>
              <div className={DETAIL_LABEL}>Disabled previous providers</div>
              <div className="mt-2 flex flex-col gap-2">
                {disabled.map((provider) => (
                  <ProviderRow
                    key={provider.id}
                    provider={provider}
                    onDirectory={() => setDirectoryOf(provider)}
                    onRemove={() => setRemoving(provider)}
                  />
                ))}
              </div>
            </div>
          )}
        </div>
      )}

      <DirectoryDialog
        provider={rows.find((p) => p.id === directoryOf?.id) ?? null}
        onClose={() => setDirectoryOf(null)}
      />
      <RemoveProviderDialog
        provider={removing}
        onClose={() => setRemoving(null)}
      />
    </Card>
  );
}

function ProviderRow({
  provider,
  onDirectory,
  onRemove,
}: {
  provider: IdentityProvider;
  onDirectory: () => void;
  onRemove?: () => void;
}) {
  // User sync is an external, enabled provider's screen: the bundled Authelia
  // has nothing to import — its people are managed on the Users page, and its
  // seeded `authelia_file` adapter would otherwise badge every bundled
  // deployment with a warning nobody can act on — and a disabled row (a
  // provider the deployment moved away from) syncs nothing either way.
  const syncable = provider.kind !== "authelia" && provider.is_enabled;
  return (
    <div className="flex flex-wrap items-start justify-between gap-3 rounded-md border border-line p-4">
      <div className="min-w-0">
        <div className={CHIPS}>
          <span className="font-medium">{provider.name}</span>
          <Badge tone={provider.is_enabled ? "ok" : "neutral"}>
            {provider.is_enabled ? "enabled" : "disabled"}
          </Badge>
          <Badge tone="neutral">{provider.kind}</Badge>
          {provider.admin_source === "claim" && (
            <Badge tone="warn">admin from IdP</Badge>
          )}
          {provider.link_by_email && (
            <Badge tone="danger">
              links new sign-ins to existing accounts by verified email
            </Badge>
          )}
          {syncable && provider.sync_adapter !== "none" && (
            <Badge
              tone={
                provider.sync_confirmed || provider.sync_adapter === "scim"
                  ? "ok"
                  : "warn"
              }
            >
              sync: {provider.sync_adapter}
              {provider.sync_confirmed || provider.sync_adapter === "scim"
                ? ""
                : " (awaiting review)"}
            </Badge>
          )}
        </div>
        <div className={`mt-1 text-sm ${MUTED}`}>{provider.issuer}</div>
        <div className="mt-1 text-xs text-ink-faint">
          client {provider.client_id} · groups claim{" "}
          <span className={CODE}>{provider.groups_claim}</span> · group mappings
          are decided on the{" "}
          {/* ADR 0095: one mapping surface, the Groups page — this line only
              points there now. */}
          <Link to="/admin/groups" className="underline">
            Groups
          </Link>{" "}
          page · groups from{" "}
          {provider.group_source === "claim"
            ? "the token"
            : provider.group_source === "directory"
              ? "the directory"
              : "this console only"}{" "}
          · sync every{" "}
          {provider.group_sync === "every_login"
            ? "sign-in"
            : provider.group_sync === "first_login"
              ? "first sign-in only"
              : "never"}
          {" · "}
          {provider.user_count} {provider.user_count === 1 ? "user" : "users"}
        </div>
        {/* Said up front, so a kept row explains itself instead of offering a
            button that would only answer 409. */}
        {!provider.is_enabled && provider.kept_reason && (
          <div className={`mt-1 text-sm ${MUTED}`}>{provider.kept_reason}</div>
        )}
        <div className="mt-2 max-w-md">
          <RedirectUri name={provider.name} />
        </div>
      </div>
      <div className="flex shrink-0 flex-wrap items-center gap-2">
        {/* A row backed only by the environment fallback (no re-seed has run
            yet) has no real id to call these endpoints with, and a bundled or
            disabled row has no user sync to open (see `syncable` above). */}
        {provider.source === "console" && syncable && (
          <Button variant="ghost" onClick={onDirectory}>
            User sync…
          </Button>
        )}
        {onRemove && !provider.is_enabled && provider.removable && (
          <Button variant="ghost" onClick={onRemove}>
            Remove
          </Button>
        )}
      </div>
    </div>
  );
}

/**
 * Removing a previous provider nobody ever signed in through. The server
 * refuses anything else (and re-checks under a lock), so this only has to say
 * what goes: the row, and its directory mirror and sync history. Bringing the
 * issuer back is a `.env` change, which re-creates the row.
 */
function RemoveProviderDialog({
  provider,
  onClose,
}: {
  provider: IdentityProvider | null;
  onClose: () => void;
}) {
  const remove = useRemoveIdentityProvider();
  const toast = useOptionalToast();

  return (
    <Dialog
      open={provider !== null}
      title={`Remove ${provider?.name ?? "this provider"}?`}
      onClose={onClose}
      footer={
        <>
          <Button onClick={onClose}>Cancel</Button>
          <Button
            variant="danger"
            busy={remove.isPending}
            onClick={() =>
              provider &&
              remove.mutate(provider.id, {
                onSuccess: () => {
                  toast?.add({
                    title: "Identity provider removed",
                    type: "success",
                  });
                  onClose();
                },
                onError: (error) =>
                  toast?.add({
                    title: "Could not remove the identity provider",
                    description:
                      error instanceof Error ? error.message : undefined,
                    type: "error",
                  }),
              })
            }
          >
            Remove
          </Button>
        </>
      }
    >
      <p>
        Nobody ever signed in through{" "}
        <span className={CODE}>{provider?.issuer}</span>, so removing it unlinks
        no one. Its directory entries and sync history are deleted with it.
        Pointing the deployment at this issuer again in .env creates it afresh.
      </p>
    </Dialog>
  );
}

// -- capability defaults (ADR 0096, amended) -----------------------------------

const CAPABILITIES: {
  key: "code_agents" | "api_keys";
  title: string;
  hint: string;
}[] = [
  {
    key: "code_agents",
    title: "Coding agents",
    hint: "The /code panel and the machine links that serve it.",
  },
  { key: "api_keys", title: "API keys", hint: "Minting and using API keys." },
];

/**
 * What new groups start with, and the two bulk flips over existing ones.
 *
 * A capability is a per-group flag now; there is no mode that gates anyone.
 * The default here is stamped on every group created from now on — console
 * New group, a mapped new group, an auto-imported one, a directory push —
 * and never gates an existing group. The bulk actions are the explicit way
 * to move the existing ones, each confirmed with the preview of how many
 * active non-admin people gain or lose it, applied in one transaction.
 */
function CapabilityDefaultsCard() {
  const policy = useOidcPolicy();
  const save = useUpdateOidcPolicy();
  const preview = usePolicyPreview();
  const bulk = useBulkGroupCapabilities();
  const toast = useOptionalToast();

  const [pending, setPending] = useState<{
    capability: "code_agents" | "api_keys";
    value: boolean;
  } | null>(null);

  if (policy.isPending || policy.error) {
    return (
      <Card title="Capabilities">
        {policy.isPending ? (
          <Spinner label="Loading the capability defaults" />
        ) : (
          <Notice tone="danger" title="Could not load the capability defaults">
            {policy.error instanceof Error
              ? policy.error.message
              : "Unknown error."}
          </Notice>
        )}
      </Card>
    );
  }

  return (
    <Card
      title="Capabilities"
      description="A person holds a capability as an administrator, or through an
        active membership in a group carrying its flag. This sets what new
        groups start with; the toggles on the Groups page decide the existing
        ones. Grant or remove the flags in bulk below."
    >
      <div className={FORM}>
        {CAPABILITIES.map((entry) => {
          const mode: CapabilityMode =
            entry.key === "code_agents"
              ? (policy.data?.code_agents_mode ?? "on")
              : (policy.data?.api_keys_mode ?? "on");
          return (
            <div
              key={entry.key}
              className="flex flex-wrap items-center justify-between gap-3 rounded-md border border-line p-4"
            >
              <div className="min-w-0">
                <div className={CHIPS}>
                  <span className="font-medium">{entry.title}</span>
                  <Badge tone={mode === "on" ? "ok" : "neutral"}>
                    {mode === "on" ? "new groups: on" : "new groups: off"}
                  </Badge>
                </div>
                <div className={`mt-1 text-sm ${MUTED}`}>{entry.hint}</div>
              </div>
              <div className="flex shrink-0 flex-wrap items-center gap-2">
                <Button
                  variant="ghost"
                  disabled={mode === "on"}
                  busy={save.isPending}
                  onClick={() =>
                    save.mutate(
                      { [`${entry.key}_mode`]: "on" } as Record<string, never>,
                      {
                        onSuccess: () =>
                          toast?.add({
                            title: `${entry.title} default saved`,
                            type: "success",
                          }),
                        onError: (caught: unknown) =>
                          toast?.add({
                            title:
                              caught instanceof Error
                                ? caught.message
                                : "Could not save",
                            type: "error",
                          }),
                      },
                    )
                  }
                >
                  Turn on for new groups
                </Button>
                <Button
                  variant="ghost"
                  disabled={mode === "off"}
                  busy={save.isPending}
                  onClick={() =>
                    save.mutate(
                      { [`${entry.key}_mode`]: "off" } as Record<string, never>,
                      {
                        onSuccess: () =>
                          toast?.add({
                            title: `${entry.title} default saved`,
                            type: "success",
                          }),
                        onError: (caught: unknown) =>
                          toast?.add({
                            title:
                              caught instanceof Error
                                ? caught.message
                                : "Could not save",
                            type: "error",
                          }),
                      },
                    )
                  }
                >
                  Turn off for new groups
                </Button>
                <Button
                  variant="secondary"
                  onClick={() => {
                    preview.mutate(
                      { bulk_capability: entry.key, bulk_value: true },
                      {
                        onSuccess: (result) => {
                          if (result.would_gain === 0) {
                            applyBulk(entry.key, true, 0);
                          } else {
                            setPending({ capability: entry.key, value: true });
                          }
                        },
                        onError: (caught: unknown) =>
                          toast?.add({
                            title:
                              caught instanceof Error
                                ? caught.message
                                : "Could not preview",
                            type: "error",
                          }),
                      },
                    );
                  }}
                >
                  Turn on for all groups
                </Button>
                <Button
                  variant="secondary"
                  onClick={() => {
                    preview.mutate(
                      { bulk_capability: entry.key, bulk_value: false },
                      {
                        onSuccess: (result) => {
                          if (result.would_lose === 0) {
                            applyBulk(entry.key, false, 0);
                          } else {
                            setPending({ capability: entry.key, value: false });
                          }
                        },
                        onError: (caught: unknown) =>
                          toast?.add({
                            title:
                              caught instanceof Error
                                ? caught.message
                                : "Could not preview",
                            type: "error",
                          }),
                      },
                    );
                  }}
                >
                  Turn off for all groups
                </Button>
              </div>
            </div>
          );
        })}
      </div>

      <CapabilityBulkConfirm
        pending={pending}
        preview={preview.data}
        saving={bulk.isPending}
        onSave={(capability, value) =>
          bulk.mutate(
            { capability, value },
            {
              onSuccess: (result) => {
                toast?.add({
                  title: `Updated ${result.updated} groups`,
                  type: "success",
                });
                setPending(null);
              },
              onError: (caught: unknown) =>
                toast?.add({
                  title:
                    caught instanceof Error
                      ? caught.message
                      : "Could not apply",
                  type: "error",
                }),
            },
          )
        }
        onClose={() => {
          setPending(null);
          preview.reset();
        }}
      />
    </Card>
  );

  function applyBulk(
    capability: "code_agents" | "api_keys",
    value: boolean,
    count: number,
  ) {
    bulk.mutate(
      { capability, value },
      {
        onSuccess: (result) => {
          toast?.add({
            title: `Updated ${result.updated} groups`,
            type: "success",
          });
          setPending(null);
        },
        onError: (caught: unknown) =>
          toast?.add({
            title: caught instanceof Error ? caught.message : "Could not apply",
            type: "error",
          }),
      },
    );
    void count;
  }
}

function CapabilityBulkConfirm({
  pending,
  preview,
  saving,
  onSave,
  onClose,
}: {
  pending: { capability: "code_agents" | "api_keys"; value: boolean } | null;
  preview: { would_gain: number | null; would_lose: number | null } | undefined;
  saving: boolean;
  onSave: (capability: "code_agents" | "api_keys", value: boolean) => void;
  onClose: () => void;
}) {
  const title =
    pending?.capability === "code_agents" ? "Coding agents" : "API keys";
  const count = pending?.value
    ? (preview?.would_gain ?? 0)
    : (preview?.would_lose ?? 0);
  return (
    <Dialog
      open={pending !== null}
      title={
        pending?.value
          ? `Turn ${title.toLowerCase()} on for all groups?`
          : `Turn ${title.toLowerCase()} off for all groups?`
      }
      onClose={onClose}
      footer={
        <>
          <Button onClick={onClose}>Cancel</Button>
          <Button
            variant={pending?.value ? "primary" : "danger"}
            busy={saving}
            onClick={() => pending && onSave(pending.capability, pending.value)}
          >
            {pending?.value ? "Turn on" : "Turn off"}
          </Button>
        </>
      }
    >
      {pending?.value
        ? `${count} ${count === 1 ? "person" : "people"} would gain ${title.toLowerCase()} — everyone holding no flagged group today. Administrators hold it either way.`
        : `${count} ${count === 1 ? "person" : "people"} would lose ${title.toLowerCase()} at their next request — everyone whose only flagged group is this one being cleared. Administrators are exempt.`}
    </Dialog>
  );
}
