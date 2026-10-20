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
import { Message } from "../components/Message";
import { m } from "../paraglide/messages.js";

export function AdminSettings() {
  return (
    <div className={PAGE}>
      <PageHeader
        title={m["admin_settings.title"]()}
        subtitle={m["admin_settings.subtitle"]()}
      />
      <EmailCard />
      <ProvidersCard />
      <Card title={m["admin_settings.provisioning_policy_card"]()}>
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
      <Card title={m["admin_settings.email.card"]()}>
        <Spinner label={m["admin_settings.email.loading"]()} />
      </Card>
    );
  }

  return (
    <Card
      title={m["admin_settings.email.card"]()}
      description={m["admin_settings.email.description"]()}
      actions={
        email.data ? (
          <Badge tone={email.data.enabled ? "ok" : "neutral"}>
            {email.data.enabled
              ? m["admin_settings.email.enabled"]()
              : m["admin_settings.email.disabled"]()}
          </Badge>
        ) : null
      }
    >
      {email.error ? (
        <Notice tone="danger" title={m["admin_settings.email.load_error.title"]()}>
          {/* i18n: server text — the API's own error message. */}
          {email.error instanceof Error
            ? email.error.message
            : m["admin_settings.unknown_error"]()}
        </Notice>
      ) : email.data ? (
        <div className={FORM}>
          <div className="grid gap-4 [grid-template-columns:repeat(auto-fit,minmax(10rem,1fr))]">
            <div>
              <div className={DETAIL_LABEL}>{m["admin_settings.email.host"]()}</div>
              <div className={CODE}>{email.data.host || m["admin_settings.email.not_set"]()}</div>
            </div>
            <div>
              <div className={DETAIL_LABEL}>{m["admin_settings.email.port"]()}</div>
              <div className={CODE}>{email.data.port}</div>
            </div>
          </div>
          <div>
            <div className={DETAIL_LABEL}>{m["admin_settings.email.username"]()}</div>
            <div className={CODE}>{email.data.username || m["admin_settings.email.none"]()}</div>
          </div>
          <div>
            <div className={DETAIL_LABEL}>{m["admin_settings.email.password"]()}</div>
            <div className={CODE}>
              {email.data.has_password ? m["admin_settings.email.set"]() : m["admin_settings.email.none"]()}
            </div>
          </div>
          <div>
            <div className={DETAIL_LABEL}>{m["admin_settings.email.from_address"]()}</div>
            <div className={CODE}>
              {email.data.from_address || m["admin_settings.email.not_set"]()}
            </div>
          </div>
          <div className="flex flex-wrap items-end gap-2">
            <Input
              label={m["admin_settings.email.test_to"]()}
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
                        ? m["admin_settings.email.toast_sent"]()
                        : m["admin_settings.email.toast_failed"](),
                      // i18n: server text — the test result's own detail.
                      description: result.detail,
                      type: result.ok ? "success" : "error",
                    }),
                  onError: (caught: unknown) =>
                    toast?.add({
                      // i18n: server text — the gateway's own error message.
                      title:
                        caught instanceof Error
                          ? caught.message
                          : m["admin_settings.email.toast_failed"](),
                      type: "error",
                    }),
                })
              }
            >
              {m["admin_settings.email.send_test"]()}
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
      <div className={DETAIL_LABEL}>{m["admin_settings.redirect_uri.label"]()}</div>
      <div className={SECRET_ROW}>
        <code className={CODE}>{uri}</code>
        {slug && (
          <Button onClick={copy}>
            {copied ? m["admin_settings.redirect_uri.copied"]() : m["admin_settings.redirect_uri.copy"]()}
          </Button>
        )}
      </div>
      <p className="mt-1 text-sm text-ink-muted">{m["admin_settings.redirect_uri.note"]()}</p>
      {copied === false && (
        <p className="text-sm text-warn">{m["admin_settings.redirect_uri.clipboard"]()}</p>
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
      title={m["admin_settings.providers.card"]()}
      description={m["admin_settings.providers.description"]()}
    >
      {providers.isPending ? (
        <Spinner label={m["admin_settings.providers.loading"]()} />
      ) : providers.error ? (
        <Notice tone="danger" title={m["admin_settings.providers.load_error.title"]()}>
          {/* i18n: server text — the API's own error message. */}
          {providers.error instanceof Error
            ? providers.error.message
            : m["admin_settings.unknown_error"]()}
        </Notice>
      ) : rows.length === 0 ? (
        <EmptyState
          title={m["admin_settings.providers.empty_title"]()}
          detail={m["admin_settings.providers.empty_detail"]()}
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
              <div className={DETAIL_LABEL}>{m["admin_settings.providers.disabled_heading"]()}</div>
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
            {provider.is_enabled
              ? m["admin_settings.badge.enabled"]()
              : m["admin_settings.badge.disabled"]()}
          </Badge>
          {/* i18n: server text — the provider kind is the API's enum value. */}
          <Badge tone="neutral">{provider.kind}</Badge>
          {provider.admin_source === "claim" && (
            <Badge tone="warn">{m["admin_settings.badge.admin_from_idp"]()}</Badge>
          )}
          {provider.link_by_email && (
            <Badge tone="danger">{m["admin_settings.badge.email_link"]()}</Badge>
          )}
          {syncable && provider.sync_adapter !== "none" && (
            <Badge
              tone={
                provider.sync_confirmed || provider.sync_adapter === "scim"
                  ? "ok"
                  : "warn"
              }
            >
              {provider.sync_confirmed || provider.sync_adapter === "scim"
                ? m["admin_settings.badge.sync"]({ adapter: provider.sync_adapter })
                : m["admin_settings.badge.sync_unreviewed"]({ adapter: provider.sync_adapter })}
            </Badge>
          )}
        </div>
        <div className={`mt-1 text-sm ${MUTED}`}>{provider.issuer}</div>
        <div className="mt-1 text-xs text-ink-faint">
          {/* ADR 0095: one mapping surface, the Groups page — this line only
              points there now. The link and the claim are inside the message, so
              a translator moves the sentence and the target stays. */}
          <Message
            message={m["admin_settings.provider_row.line"]}
            markup={{
              code: (children) => <span className={CODE}>{children}</span>,
              link: (children) => (
                <Link to="/admin/groups" className="underline">
                  {children}
                </Link>
              ),
            }}
            inputs={{
              client: provider.client_id,
              claim: provider.groups_claim,
              source:
                provider.group_source === "claim"
                  ? m["admin_settings.provider_row.source.claim"]()
                  : provider.group_source === "directory"
                    ? m["admin_settings.provider_row.source.directory"]()
                    : m["admin_settings.provider_row.source.console"](),
              cadence:
                provider.group_sync === "every_login"
                  ? m["admin_settings.provider_row.sync.every_login"]()
                  : provider.group_sync === "first_login"
                    ? m["admin_settings.provider_row.sync.first_login"]()
                    : m["admin_settings.provider_row.sync.never"](),
              users: m["admin_settings.provider_row.users"]({ count: provider.user_count }),
            }}
          />
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
            {m["admin_settings.provider_row.user_sync"]()}
          </Button>
        )}
        {onRemove && !provider.is_enabled && provider.removable && (
          <Button variant="ghost" onClick={onRemove}>
            {m["admin_settings.provider_row.remove"]()}
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
      title={m["admin_settings.remove.title"]({
        provider: provider?.name ?? m["admin_settings.remove.title_fallback"](),
      })}
      onClose={onClose}
      footer={
        <>
          <Button onClick={onClose}>{m["admin_settings.remove.cancel"]()}</Button>
          <Button
            variant="danger"
            busy={remove.isPending}
            onClick={() =>
              provider &&
              remove.mutate(provider.id, {
                onSuccess: () => {
                  toast?.add({
                    title: m["admin_settings.remove.toast_done"](),
                    type: "success",
                  });
                  onClose();
                },
                onError: (error) =>
                  toast?.add({
                    title: m["admin_settings.remove.toast_failed"](),
                    description:
                      error instanceof Error ? error.message : undefined,
                    type: "error",
                  }),
              })
            }
          >
            {m["admin_settings.remove.confirm"]()}
          </Button>
        </>
      }
    >
      <p>
        <Message
          message={m["admin_settings.remove.body"]}
          markup={{ code: (children) => <span className={CODE}>{children}</span> }}
          inputs={{ issuer: provider?.issuer ?? "" }}
        />
      </p>
    </Dialog>
  );
}

// -- capability defaults (ADR 0096, amended) -----------------------------------

const CAPABILITIES: {
  key: "code_agents" | "api_keys";
  title: () => string;
  titleLower: () => string;
  hint: () => string;
}[] = [
  {
    key: "code_agents",
    title: m["admin_settings.capability.code_agents"],
    titleLower: m["admin_settings.capability.code_agents_lower"],
    hint: m["admin_settings.capability.code_agents_hint"],
  },
  {
    key: "api_keys",
    title: m["admin_settings.capability.api_keys"],
    titleLower: m["admin_settings.capability.api_keys_lower"],
    hint: m["admin_settings.capability.api_keys_hint"],
  },
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
      <Card title={m["admin_settings.capabilities.card"]()}>
        {policy.isPending ? (
          <Spinner label={m["admin_settings.capabilities.loading"]()} />
        ) : (
          <Notice tone="danger" title={m["admin_settings.capabilities.load_error.title"]()}>
            {/* i18n: server text — the API's own error message. */}
            {policy.error instanceof Error
              ? policy.error.message
              : m["admin_settings.unknown_error"]()}
          </Notice>
        )}
      </Card>
    );
  }

  return (
    <Card
      title={m["admin_settings.capabilities.card"]()}
      description={m["admin_settings.capabilities.description"]()}
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
                  <span className="font-medium">{entry.title()}</span>
                  <Badge tone={mode === "on" ? "ok" : "neutral"}>
                    {mode === "on"
                      ? m["admin_settings.capability.badge_on"]()
                      : m["admin_settings.capability.badge_off"]()}
                  </Badge>
                </div>
                <div className={`mt-1 text-sm ${MUTED}`}>{entry.hint()}</div>
              </div>
              {/* w-full on phones: shrink-0 alone kept the two long labels
                  side by side and they ran past the card edge. */}
              <div className="flex w-full flex-wrap items-center gap-2 sm:w-auto sm:shrink-0">
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
                            title: m["admin_settings.capability.toast_saved"]({
                              capability: entry.title(),
                            }),
                            type: "success",
                          }),
                        onError: (caught: unknown) =>
                          toast?.add({
                            // i18n: server text — the gateway's own error message.
                            title:
                              caught instanceof Error
                                ? caught.message
                                : m["admin_settings.capability.toast_save_failed"](),
                            type: "error",
                          }),
                      },
                    )
                  }
                >
                  {m["admin_settings.capability.turn_on_new"]()}
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
                            title: m["admin_settings.capability.toast_saved"]({
                              capability: entry.title(),
                            }),
                            type: "success",
                          }),
                        onError: (caught: unknown) =>
                          toast?.add({
                            // i18n: server text — the gateway's own error message.
                            title:
                              caught instanceof Error
                                ? caught.message
                                : m["admin_settings.capability.toast_save_failed"](),
                            type: "error",
                          }),
                      },
                    )
                  }
                >
                  {m["admin_settings.capability.turn_off_new"]()}
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
                            // i18n: server text — the gateway's own error message.
                            title:
                              caught instanceof Error
                                ? caught.message
                                : m["admin_settings.capability.toast_preview_failed"](),
                            type: "error",
                          }),
                      },
                    );
                  }}
                >
                  {m["admin_settings.capability.turn_on_all"]()}
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
                            // i18n: server text — the gateway's own error message.
                            title:
                              caught instanceof Error
                                ? caught.message
                                : m["admin_settings.capability.toast_preview_failed"](),
                            type: "error",
                          }),
                      },
                    );
                  }}
                >
                  {m["admin_settings.capability.turn_off_all"]()}
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
                  title: m["admin_settings.capability.toast_updated"]({
                    count: result.updated,
                  }),
                  type: "success",
                });
                setPending(null);
              },
              onError: (caught: unknown) =>
                toast?.add({
                  // i18n: server text — the gateway's own error message.
                  title:
                    caught instanceof Error
                      ? caught.message
                      : m["admin_settings.capability.toast_apply_failed"](),
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
  const entry =
    CAPABILITIES.find((candidate) => candidate.key === pending?.capability) ?? CAPABILITIES[1]!;
  const count = pending?.value
    ? (preview?.would_gain ?? 0)
    : (preview?.would_lose ?? 0);
  return (
    <Dialog
      open={pending !== null}
      title={
        pending?.value
          ? m["admin_settings.bulk.title_on"]({ capability: entry.titleLower() })
          : m["admin_settings.bulk.title_off"]({ capability: entry.titleLower() })
      }
      onClose={onClose}
      footer={
        <>
          <Button onClick={onClose}>{m["admin_settings.bulk.cancel"]()}</Button>
          <Button
            variant={pending?.value ? "primary" : "danger"}
            busy={saving}
            onClick={() => pending && onSave(pending.capability, pending.value)}
          >
            {pending?.value ? m["admin_settings.bulk.turn_on"]() : m["admin_settings.bulk.turn_off"]()}
          </Button>
        </>
      }
    >
      {pending?.value
        ? m["admin_settings.bulk.gain"]({ count, capability: entry.titleLower() })
        : m["admin_settings.bulk.lose"]({ count, capability: entry.titleLower() })}
    </Dialog>
  );
}
