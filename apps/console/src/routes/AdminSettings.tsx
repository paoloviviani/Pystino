/**
 * The Settings screen (ADR 0051): the mail server, the identity providers,
 * and the provisioning policy that decides who may become a user.
 *
 * One screen because these three answers shape the same question — how people
 * get in, and how the platform reaches them. Each section saves independently;
 * a change to the SMTP port should not have to travel with a change to the
 * provisioning rule.
 */

import { Badge, Button, Card, EmptyState, Input, Notice, Spinner } from "@llmp/ui";
import { useEffect, useState } from "react";
import type { FormEvent } from "react";
import { useEmailSettings, useIdentityProviders, useTestEmail, useUpdateEmailSettings } from "../lib/admin";
import type { EmailSettingsInput, IdentityProvider } from "../lib/types";
import { DirectoryDialog } from "./IdentityDirectory";
import { useOptionalToast } from "../lib/toast";
import { PageHeader } from "../components/PageHeader";
import { ProvisioningPolicySection } from "./AdminIdentity";
import { CHIPS, CODE, DETAIL_LABEL, FORM, MUTED, PAGE, SECRET_ROW } from "../lib/layout";

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

/**
 * Read-only (ADR 0093 §14): the row is a projection of the environment now,
 * re-seeded at every start, so there is nothing here for an administrator to
 * create, edit or delete — `./configure` is where a deployment's provider
 * changes. What is still console-owned is the directory mirror, which is a
 * decision about *this* console's own user list, not about the IdP, so
 * `DirectoryDialog` stays reachable from here.
 */
function ProvidersCard() {
  const providers = useIdentityProviders();
  const [directoryOf, setDirectoryOf] = useState<IdentityProvider | null>(null);

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
          {providers.error instanceof Error ? providers.error.message : "Unknown error."}
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
    </Card>
  );
}

function ProviderRow({
  provider,
  onDirectory,
}: {
  provider: IdentityProvider;
  onDirectory: () => void;
}) {
  return (
    <div className="flex flex-wrap items-start justify-between gap-3 rounded-md border border-line p-4">
      <div className="min-w-0">
        <div className={CHIPS}>
          <span className="font-medium">{provider.name}</span>
          <Badge tone={provider.is_enabled ? "ok" : "neutral"}>
            {provider.is_enabled ? "enabled" : "disabled"}
          </Badge>
          <Badge tone="neutral">{provider.kind}</Badge>
          {provider.admin_source === "claim" && <Badge tone="warn">admin from IdP</Badge>}
          {provider.link_by_email && <Badge tone="danger">adopts local accounts by email</Badge>}
          {provider.sync_adapter !== "none" && (
            <Badge tone={provider.sync_confirmed || provider.sync_adapter === "scim" ? "ok" : "warn"}>
              sync: {provider.sync_adapter}
              {provider.sync_confirmed || provider.sync_adapter === "scim" ? "" : " (awaiting review)"}
            </Badge>
          )}
        </div>
        <div className={`mt-1 text-sm ${MUTED}`}>{provider.issuer}</div>
        <div className="mt-1 text-xs text-ink-faint">
          client {provider.client_id} · groups claim{" "}
          <span className={CODE}>{provider.groups_claim}</span> ·{" "}
          {provider.group_mappings.length} mapping rule(s) · groups from{" "}
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
        <div className="mt-2 max-w-md">
          <RedirectUri name={provider.name} />
        </div>
      </div>
      <div className="flex shrink-0 flex-wrap items-center gap-2">
        {/* A row backed only by the environment fallback (no re-seed has run
            yet) has no real id to call these endpoints with. */}
        {provider.source === "console" && (
          <Button variant="ghost" onClick={onDirectory}>
            Directory
          </Button>
        )}
      </div>
    </div>
  );
}
