/**
 * The Settings screen (ADR 0051): the mail server, the identity providers,
 * and the provisioning policy that decides who may become a user.
 *
 * One screen because these three answers shape the same question — how people
 * get in, and how the platform reaches them. Each section saves independently;
 * a change to the SMTP port should not have to travel with a change to the
 * provisioning rule.
 */

import { Badge, Button, Card, Dialog, EmptyState, Input, Notice, Select, Spinner } from "@llmp/ui";
import { useEffect, useState } from "react";
import type { FormEvent } from "react";
import {
  useCreateIdentityProvider,
  useDeleteIdentityProvider,
  useEmailSettings,
  useIdentityKinds,
  useIdentityProviders,
  useTestEmail,
  useUpdateEmailSettings,
  useUpdateIdentityProvider,
} from "../lib/admin";
import type {
  AdminSource,
  EmailSettingsInput,
  GroupSource,
  GroupSync,
  IdentityKind,
  IdentityProvider,
  IdentityProviderInput,
} from "../lib/types";
import { AutheliaUsersDialog, DirectoryDialog } from "./IdentityDirectory";
import { useOptionalToast } from "../lib/toast";
import { PageHeader } from "../components/PageHeader";
import { ProvisioningPolicySection } from "./AdminIdentity";
import { CODE, DETAIL_LABEL, FORM, PAGE, SECRET_ROW } from "../lib/layout";

export function AdminSettings() {
  return (
    <div className={PAGE}>
      <PageHeader
        title="Settings"
        subtitle="The mail server, the identity providers, and who may become a user.
          Each section saves on its own."
      />
      <EmailCard />
      <ProvidersCard />
      <Card title="Provisioning policy">
        <ProvisioningPolicySection />
      </Card>
    </div>
  );
}

// -- email ---------------------------------------------------------------------

function EmailCard() {
  const email = useEmailSettings();
  const save = useUpdateEmailSettings();
  const test = useTestEmail();
  const toast = useOptionalToast();

  const [host, setHost] = useState("");
  const [port, setPort] = useState("587");
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [fromAddress, setFromAddress] = useState("");
  const [testTo, setTestTo] = useState("");

  useEffect(() => {
    if (!email.data) return;
    setHost(email.data.host);
    setPort(String(email.data.port));
    setUsername(email.data.username);
    setFromAddress(email.data.from_address);
  }, [email.data]);

  const buildInput = (): EmailSettingsInput => ({
    host: host.trim(),
    port: Number(port) || 587,
    username: username.trim(),
    // An empty password field means "keep the stored one": the form never
    // shows a password, so it cannot ask to re-type one it never saw.
    password: password || undefined,
    from_address: fromAddress.trim(),
  });

  const submit = (event: FormEvent) => {
    event.preventDefault();
    save.mutate(buildInput(), {
      onSuccess: () => toast?.add({ title: "Email settings saved", type: "success" }),
      onError: (caught: unknown) =>
        toast?.add({
          title: caught instanceof Error ? caught.message : "Could not save the email settings",
          type: "error",
        }),
    });
  };

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
      description="Used for password-reset links and quota notices. The password is
        write-only: leave it blank to keep the stored one."
      actions={
        <Badge tone={email.data?.source === "console" ? "accent" : "neutral"}>
          {email.data?.source === "console" ? "set in the console" : "from the environment"}
        </Badge>
      }
    >
      <form className={FORM} onSubmit={submit}>
        {email.error ? (
          <Notice tone="danger" title="Could not load the email settings">
            {email.error instanceof Error ? email.error.message : "Unknown error."}
          </Notice>
        ) : null}
        <div className="grid gap-4 [grid-template-columns:repeat(auto-fit,minmax(10rem,1fr))]">
          <Input label="SMTP host" value={host} onChange={(e) => setHost(e.target.value)} />
          <Input label="Port" value={port} onChange={(e) => setPort(e.target.value)} />
        </div>
        <Input
          label="Username"
          value={username}
          onChange={(e) => setUsername(e.target.value)}
          hint="Optional, when the server asks to log in."
        />
        <Input
          label="Password"
          type="password"
          value={password}
          onChange={(e) => setPassword(e.target.value)}
          hint={
            email.data?.has_password
              ? "Stored. Type to replace; leave blank to keep it."
              : "Optional, when the server asks to log in."
          }
        />
        <Input
          label="From address"
          value={fromAddress}
          onChange={(e) => setFromAddress(e.target.value)}
          hint={'RFC 5322: "Pystino <no-reply@example.org>".'}
        />
        <div className="flex flex-wrap items-end gap-2">
          <Button type="submit" variant="primary" busy={save.isPending}>
            Save
          </Button>
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
                    title: result.ok ? "Test email sent" : "Test email failed",
                    description: result.detail,
                    type: result.ok ? "success" : "error",
                  }),
                onError: (caught: unknown) =>
                  toast?.add({
                    title: caught instanceof Error ? caught.message : "Test email failed",
                    type: "error",
                  }),
              })
            }
          >
            Send test
          </Button>
        </div>
      </form>
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
        <p className="text-sm text-warn">Could not reach the clipboard; copy it by hand.</p>
      )}
    </div>
  );
}

function ProvidersCard() {
  const providers = useIdentityProviders();
  const [creating, setCreating] = useState(false);
  const [editing, setEditing] = useState<IdentityProvider | null>(null);
  const [deleting, setDeleting] = useState<IdentityProvider | null>(null);
  const [directoryOf, setDirectoryOf] = useState<IdentityProvider | null>(null);
  const [usersOf, setUsersOf] = useState<IdentityProvider | null>(null);

  return (
    <Card
      title="Identity providers"
      description="One button per enabled provider on the sign-in page. Users from
        different providers are different accounts — identity is (issuer, subject)."
      actions={
        // Hidden once the empty state below shows its own: two "Add provider"
        // buttons on one screen is how the wrong one gets pressed in a test
        // and the right one missed by a reader. While loading the header keeps
        // its button, so the action never vanishes mid-flight.
        providers.data !== undefined && providers.data.length === 0 ? undefined : (
          <Button variant="primary" onClick={() => setCreating(true)}>
            Add provider
          </Button>
        )
      }
    >
      {providers.isPending ? (
        <Spinner label="Loading providers" />
      ) : providers.error ? (
        <Notice tone="danger" title="Could not load the identity providers">
          {providers.error instanceof Error ? providers.error.message : "Unknown error."}
        </Notice>
      ) : (providers.data ?? []).length === 0 ? (
        <EmptyState
          title="No identity provider is configured"
          detail="Nobody can sign in yet: every person signs in through an OpenID Connect provider."
          action={
            <Button variant="primary" onClick={() => setCreating(true)}>
              Add provider
            </Button>
          }
        />
      ) : (
        <div className="flex flex-col gap-2">
          {(providers.data ?? []).map((provider) => (
            <div
              key={provider.id}
              className="flex flex-wrap items-start justify-between gap-3 rounded-md border border-line p-4"
            >
              <div className="min-w-0">
                <div className="flex flex-wrap items-center gap-2">
                  <span className="font-medium">{provider.name}</span>
                  <Badge tone={provider.is_enabled ? "ok" : "neutral"}>
                    {provider.is_enabled ? "enabled" : "disabled"}
                  </Badge>
                  <Badge tone={provider.source === "console" ? "accent" : "neutral"}>
                    {provider.source}
                  </Badge>
                  <Badge tone="neutral">{provider.kind}</Badge>
                  {provider.admin_source === "claim" && <Badge tone="warn">admin from IdP</Badge>}
                  {provider.sync_adapter !== "none" && (
                    <Badge tone={provider.sync_confirmed || provider.sync_adapter === "scim" ? "ok" : "warn"}>
                      sync: {provider.sync_adapter}
                      {provider.sync_confirmed || provider.sync_adapter === "scim" ? "" : " (awaiting review)"}
                    </Badge>
                  )}
                </div>
                <div className="mt-1 text-sm text-ink-muted">{provider.issuer}</div>
                <div className="mt-1 text-xs text-ink-faint">
                  client {provider.client_id} · groups claim{" "}
                  <span className="font-mono">{provider.groups_claim}</span> ·{" "}
                  {Object.keys(provider.group_mappings).length} mapping rule(s) · groups from{" "}
                  {provider.group_source === "claim" ? "the token" : provider.group_source === "directory" ? "the directory" : "this console only"}
                </div>
              </div>
              <div className="flex shrink-0 flex-wrap items-center gap-2">
                <Button variant="ghost" onClick={() => setEditing(provider)}>
                  Edit
                </Button>
                {provider.source === "console" && (
                  <Button variant="ghost" onClick={() => setDirectoryOf(provider)}>
                    Directory
                  </Button>
                )}
                {provider.kind === "authelia" && provider.source === "console" && (
                  <Button variant="ghost" onClick={() => setUsersOf(provider)}>
                    People
                  </Button>
                )}
                <Button
                  variant="ghost"
                  className="text-danger"
                  onClick={() => setDeleting(provider)}
                >
                  Delete
                </Button>
              </div>
            </div>
          ))}
        </div>
      )}

      <ProviderDialog
        open={creating || editing !== null}
        existing={editing}
        onClose={() => {
          setCreating(false);
          setEditing(null);
        }}
      />
      <DeleteProviderDialog provider={deleting} onClose={() => setDeleting(null)} />
      <DirectoryDialog
        provider={(providers.data ?? []).find((p) => p.id === directoryOf?.id) ?? null}
        onClose={() => setDirectoryOf(null)}
      />
      <AutheliaUsersDialog provider={usersOf} onClose={() => setUsersOf(null)} />
    </Card>
  );
}

/**
 * One dialog for create and edit: `existing` set means edit, and an omitted
 * client secret means "keep the stored one" — the field shows nothing and the
 * save sends no password at all.
 */
function ProviderDialog({
  open,
  existing,
  onClose,
}: {
  open: boolean;
  existing?: IdentityProvider | null;
  onClose: () => void;
}) {
  const isEdit = existing !== null && existing !== undefined;
  const create = useCreateIdentityProvider();
  const update = useUpdateIdentityProvider();
  const toast = useOptionalToast();

  const [name, setName] = useState("");
  const [issuer, setIssuer] = useState("");
  const [clientId, setClientId] = useState("");
  const [clientSecret, setClientSecret] = useState("");
  const [groupsClaim, setGroupsClaim] = useState("groups");
  const [mappings, setMappings] = useState<{ idp: string; local: string }[]>([]);
  const [linkLocal, setLinkLocal] = useState(false);
  const [groupSync, setGroupSync] = useState<GroupSync>("first_login");
  const [isEnabled, setIsEnabled] = useState(true);
  const [kind, setKind] = useState<IdentityKind>("generic");
  const [internalUrl, setInternalUrl] = useState("");
  const [logoutUrl, setLogoutUrl] = useState("");
  const [groupSource, setGroupSource] = useState<GroupSource>("claim");
  const [adminSource, setAdminSource] = useState<AdminSource>("console");
  const [adminClaim, setAdminClaim] = useState("groups");
  const [adminValues, setAdminValues] = useState("");
  const [subjectClaim, setSubjectClaim] = useState("sub");
  const kinds = useIdentityKinds();
  const caps = kinds.data?.[kind];

  const target = isEdit ? existing : null;

  useEffect(() => {
    if (!target) return;
    setName(target.name);
    setIssuer(target.issuer);
    setClientId(target.client_id);
    setClientSecret("");
    setGroupsClaim(target.groups_claim);
    setMappings(target.group_mappings.map((rule) => ({ ...rule })));
    setLinkLocal(target.link_local_by_email);
    setGroupSync(target.group_sync);
    setIsEnabled(target.is_enabled);
    setKind(target.kind);
    setInternalUrl(target.internal_base_url);
    setLogoutUrl(target.logout_url ?? "");
    setGroupSource(target.group_source);
    setAdminSource(target.admin_source);
    setAdminClaim(target.admin_claim);
    setAdminValues(target.admin_values.join(", "));
    setSubjectClaim(target.subject_claim);
  }, [target]);

  const close = () => {
    setName("");
    setIssuer("");
    setClientId("");
    setClientSecret("");
    setGroupsClaim("groups");
    setMappings([]);
    setLinkLocal(false);
    setGroupSync("first_login");
    setIsEnabled(true);
    setKind("generic");
    setInternalUrl("");
    setLogoutUrl("");
    setGroupSource("claim");
    setAdminSource("console");
    setAdminClaim("groups");
    setAdminValues("");
    setSubjectClaim("sub");
    create.reset();
    update.reset();
    onClose();
  };

  const policy = {
    kind,
    internal_base_url: internalUrl.trim(),
    logout_url: logoutUrl.trim(),
    group_source: groupSource,
    admin_source: adminSource,
    admin_claim: adminClaim.trim() || "groups",
    admin_values: adminValues.split(",").map((v) => v.trim()).filter(Boolean),
    subject_claim: subjectClaim.trim() || "sub",
  };

  const submit = () => {
    if (isEdit && target) {
      const body: IdentityProviderInput = {
        issuer: issuer.trim(),
        client_id: clientId.trim(),
        scopes: undefined,
        groups_claim: groupsClaim.trim(),
        fetch_userinfo: true,
        group_mappings: mappings.filter((r) => r.idp.trim() && r.local.trim()),
        link_local_by_email: linkLocal,
        group_sync: groupSync,
        is_enabled: isEnabled,
        ...policy,
      };
      if (clientSecret) body.client_secret = clientSecret;
      update.mutate(
        { id: target.id, ...body },
        {
          onSuccess: () => {
            toast?.add({ title: `Provider ${target.name} updated`, type: "success" });
            close();
          },
          onError: (caught: unknown) =>
            toast?.add({
              title: caught instanceof Error ? caught.message : "Could not update the provider",
              type: "error",
            }),
        },
      );
    } else {
      create.mutate(
        {
          name: name.trim(),
          issuer: issuer.trim(),
          client_id: clientId.trim(),
          client_secret: clientSecret,
          groups_claim: groupsClaim.trim(),
          group_mappings: mappings.filter((r) => r.idp.trim() && r.local.trim()),
          link_local_by_email: linkLocal,
          group_sync: groupSync,
          ...policy,
        },
        {
          onSuccess: (created) => {
            toast?.add({ title: `Provider ${created.name} added`, type: "success" });
            close();
          },
          onError: (caught: unknown) =>
            toast?.add({
              title: caught instanceof Error ? caught.message : "Could not add the provider",
              type: "error",
            }),
        },
      );
    }
  };

  const ready = issuer.trim() && clientId.trim() && (isEdit || (name.trim() && clientSecret));

  return (
    <Dialog
      open={open}
      title={isEdit ? `Edit provider — ${target?.name}` : "Add identity provider"}
      onClose={close}
      footer={
        <>
          <Button onClick={close}>Cancel</Button>
          <Button variant="primary" busy={create.isPending || update.isPending} disabled={!ready} onClick={submit}>
            {isEdit ? "Save" : "Add provider"}
          </Button>
        </>
      }
    >
      {create.error || update.error ? (
        <Notice tone="danger">
          {(create.error ?? update.error) instanceof Error
            ? (create.error ?? update.error)!.message
            : "Unknown error."}
        </Notice>
      ) : null}
      <div className={FORM}>
        <Input
          label="Name"
          value={name}
          onChange={(e) => setName(e.target.value)}
          hint="A short slug: the sign-in button reads “Sign in with <name>”."
          disabled={isEdit}
        />
        <RedirectUri name={name} />
        <Input
          label="Issuer"
          value={issuer}
          onChange={(e) => setIssuer(e.target.value)}
          hint="The identity provider's issuer URL — discovery is fetched from
            <issuer>/.well-known/openid-configuration."
        />
        <Select
          label="Kind of directory"
          value={kind}
          onChange={(e) => setKind(e.target.value as IdentityKind)}
          hint="Decides what this directory can offer: groups in the token, a user listing, SCIM push."
        >
          <option value="generic">Generic OpenID Connect</option>
          <option value="authelia">Authelia (the bundled one)</option>
          <option value="keycloak">Keycloak</option>
          <option value="entra">Microsoft Entra ID</option>
          <option value="okta">Okta</option>
          <option value="authentik">Authentik</option>
          <option value="google">Google Workspace</option>
        </Select>
        <Input
          label="Internal URL (optional)"
          value={internalUrl}
          onChange={(e) => setInternalUrl(e.target.value)}
          hint="Where this server reaches the issuer when not at its public URL — the bundled
            Authelia is http://authelia:9091/authelia. Browsers always use the issuer."
        />
        <Input
          label="Logout URL (optional)"
          value={logoutUrl}
          onChange={(e) => setLogoutUrl(e.target.value)}
          placeholder={
            kind === "authelia"
              ? `${issuer.trim().replace(/\/+$/, "")}/logout?rd={redirect}`
              : "the provider's end_session_endpoint"
          }
          hint="Where signing out sends the browser to end this directory's own session.
            {redirect} is replaced by the page to come back to. Empty: the provider's
            end_session_endpoint, or for Authelia (which publishes none) the default shown."
        />
        <Input label="Client ID" value={clientId} onChange={(e) => setClientId(e.target.value)} />
        <Input
          label="Client secret"
          type="password"
          value={clientSecret}
          onChange={(e) => setClientSecret(e.target.value)}
          hint={
            isEdit
              ? "Stored encrypted. Type to replace; leave blank to keep it."
              : "Stored encrypted, never shown again."
          }
        />
        <Input
          label="Groups claim"
          value={groupsClaim}
          onChange={(e) => setGroupsClaim(e.target.value)}
          hint="Where this directory puts the person's groups (dotted paths reach
            into nested claims)."
        />
        <div>
          <div className={DETAIL_LABEL}>Mapping rules — IdP group → local group</div>
          <div className="mt-2 flex flex-col gap-2">
            {mappings.map((rule, index) => (
              <div key={index} className="flex items-end gap-2">
                <Input
                  label={index === 0 ? "In the IdP" : undefined}
                  hideLabel={index !== 0}
                  value={rule.idp}
                  onChange={(e) =>
                    setMappings((current) =>
                      current.map((r, i) => (i === index ? { ...r, idp: e.target.value } : r)),
                    )
                  }
                />
                <Input
                  label={index === 0 ? "Here" : undefined}
                  hideLabel={index !== 0}
                  value={rule.local}
                  onChange={(e) =>
                    setMappings((current) =>
                      current.map((r, i) => (i === index ? { ...r, local: e.target.value } : r)),
                    )
                  }
                />
                <Button
                  variant="ghost"
                  onClick={() => setMappings((current) => current.filter((_, i) => i !== index))}
                >
                  Remove
                </Button>
              </div>
            ))}
          </div>
          <Button
            variant="ghost"
            className="mt-2"
            onClick={() => setMappings((current) => [...current, { idp: "", local: "" }])}
          >
            Add mapping
          </Button>
        </div>
        <Select
          label="Who decides group membership?"
          value={groupSource}
          onChange={(e) => setGroupSource(e.target.value as GroupSource)}
        >
          {caps?.claims_groups !== false && (
            <option value="claim">The identity provider — the groups claim at sign-in</option>
          )}
          {(caps?.adapters.length ?? 0) > 0 && (
            <option value="directory">The directory — its user listing or SCIM push</option>
          )}
          <option value="none">This console only</option>
        </Select>
        <Select
          label="When does the provider's answer apply?"
          value={groupSync}
          onChange={(e) => setGroupSync(e.target.value as GroupSync)}
          hint="Applies only to memberships this directory granted. A group an
            administrator assigned is never removed by a sign-in, whichever of
            these is chosen."
        >
          <option value="every_login">
            Set on every sign-in — the directory is authoritative
          </option>
          <option value="first_login">
            Set once, when the account first appears — administered here afterwards
          </option>
          <option value="never">
            Never — sign-in only, groups assigned here
          </option>
        </Select>
        <Select
          label="Who decides who is an administrator?"
          value={adminSource}
          onChange={(e) => setAdminSource(e.target.value as AdminSource)}
          hint="The provider can only take away an administrator flag it granted, and never the
            last active administrator's; `pystino admin grant` stays the way back in."
        >
          <option value="console">This console</option>
          <option value="claim">A claim or group from this provider</option>
        </Select>
        {adminSource === "claim" && (
          <>
            <Input label="Admin claim" value={adminClaim} onChange={(e) => setAdminClaim(e.target.value)} />
            <Input
              label="Values that make someone an administrator"
              value={adminValues}
              onChange={(e) => setAdminValues(e.target.value)}
              hint="Comma-separated; matched as the directory spells them or as mapped here."
            />
          </>
        )}
        <Input
          label="Subject claim"
          value={subjectClaim}
          onChange={(e) => setSubjectClaim(e.target.value)}
          hint="The claim that identifies a person: sub, or oid for Entra ID. Locked once
            the provider has users."
        />
        <div>
          <label className="flex cursor-pointer items-start gap-2">
            <input
              type="checkbox"
              className="mt-1"
              checked={linkLocal}
              onChange={(e) => setLinkLocal(e.target.checked)}
            />
            <span>
              Adopt local accounts with the same address
              <span className="mt-0.5 block text-xs text-ink-faint">
                A first sign-in here becomes the existing local account when this
                directory reports the same address as verified — one person, one
                account, keys and spend included. Its groups then become
                authoritative for that account, including whether it is an
                administrator. Unverified addresses are never matched.
              </span>
            </span>
          </label>
        </div>
        {isEdit && (
          <label className="flex cursor-pointer items-center gap-2">
            <input
              type="checkbox"
              checked={isEnabled}
              onChange={(e) => setIsEnabled(e.target.checked)}
            />
            Enabled — offered on the sign-in page
          </label>
        )}
      </div>
    </Dialog>
  );
}

function DeleteProviderDialog({
  provider,
  onClose,
}: {
  provider: IdentityProvider | null;
  onClose: () => void;
}) {
  const remove = useDeleteIdentityProvider();
  const toast = useOptionalToast();

  return (
    <Dialog
      open={provider !== null}
      title={`Delete ${provider?.name ?? "this provider"}?`}
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
                  toast?.add({ title: "Provider deleted", type: "success" });
                  onClose();
                },
                onError: () =>
                  toast?.add({ title: "Could not delete the provider", type: "error" }),
              })
            }
          >
            Delete provider
          </Button>
        </>
      }
    >
      <p>
        Sign-in through this provider stops immediately. Accounts that arrived
        through it are keyed on its issuer and stay in the ledger, but they can
        no longer sign in unless the provider returns.
      </p>
    </Dialog>
  );
}
