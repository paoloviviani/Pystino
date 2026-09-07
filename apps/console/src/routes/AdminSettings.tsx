/**
 * The Settings screen (ADR 0051): the mail server, the identity providers,
 * and the provisioning policy that decides who may become a user.
 *
 * One screen because these three answers shape the same question — how people
 * get in, and how the platform reaches them. Each section saves independently;
 * a change to the SMTP port should not have to travel with a change to the
 * provisioning rule.
 */

import { Badge, Button, Card, Dialog, Input, Notice, Spinner } from "@llmp/ui";
import { useEffect, useState } from "react";
import type { FormEvent } from "react";
import {
  useCreateIdentityProvider,
  useDeleteIdentityProvider,
  useEmailSettings,
  useIdentityProviders,
  useTestEmail,
  useUpdateEmailSettings,
  useUpdateIdentityProvider,
} from "../lib/admin";
import type { EmailSettingsInput, IdentityProvider, IdentityProviderInput } from "../lib/types";
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

  return (
    <Card
      title="Identity providers"
      description="One button per enabled provider on the sign-in page. Users from
        different providers are different accounts — identity is (issuer, subject)."
      actions={
        <Button variant="primary" onClick={() => setCreating(true)}>
          Add provider
        </Button>
      }
    >
      {providers.isPending ? (
        <Spinner label="Loading providers" />
      ) : providers.error ? (
        <Notice tone="danger" title="Could not load the identity providers">
          {providers.error instanceof Error ? providers.error.message : "Unknown error."}
        </Notice>
      ) : (providers.data ?? []).length === 0 ? (
        <Notice tone="info" title="No identity provider is configured">
          Sign-in is local accounts only. Add a provider to offer SSO.
        </Notice>
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
                </div>
                <div className="mt-1 text-sm text-ink-muted">{provider.issuer}</div>
                <div className="mt-1 text-xs text-ink-faint">
                  client {provider.client_id} · groups claim{" "}
                  <span className="font-mono">{provider.groups_claim}</span> ·{" "}
                  {Object.keys(provider.group_mappings).length} mapping rule(s)
                </div>
              </div>
              <div className="flex shrink-0 flex-wrap items-center gap-2">
                <Button variant="ghost" onClick={() => setEditing(provider)}>
                  Edit
                </Button>
                <Button variant="ghost" onClick={() => setDeleting(provider)}>
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
  const [isEnabled, setIsEnabled] = useState(true);

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
    setIsEnabled(target.is_enabled);
  }, [target]);

  const close = () => {
    setName("");
    setIssuer("");
    setClientId("");
    setClientSecret("");
    setGroupsClaim("groups");
    setMappings([]);
    setLinkLocal(false);
    setIsEnabled(true);
    create.reset();
    update.reset();
    onClose();
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
        is_enabled: isEnabled,
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
