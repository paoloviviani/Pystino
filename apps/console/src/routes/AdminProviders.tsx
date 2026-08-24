import { Badge, Button, Card, Dialog, Input, Notice, Select, Spinner, Table } from "@llmp/ui";
import type { Column } from "@llmp/ui";
import { useState } from "react";
import {
  useCreateProvider,
  useDeleteProvider,
  useProviderPlugins,
  useProviders,
  useTestProvider,
  useUpdateProvider,
} from "../lib/admin";
import type { AdminProvider, ProviderPlugin, ProviderTestResult } from "../lib/types";
import { PageHeader } from "../components/PageHeader";
import styles from "./Admin.module.css";

export function AdminProviders() {
  const providers = useProviders();
  const plugins = useProviderPlugins();
  const update = useUpdateProvider();
  const remove = useDeleteProvider();
  const test = useTestProvider();

  const [creating, setCreating] = useState(false);
  const [editing, setEditing] = useState<AdminProvider | null>(null);
  const [results, setResults] = useState<Record<string, ProviderTestResult>>({});

  const runTest = (provider: AdminProvider) => {
    test.mutate(provider.id, {
      onSuccess: (result) => setResults((current) => ({ ...current, [provider.id]: result })),
    });
  };

  const columns: Column<AdminProvider>[] = [
    {
      key: "name",
      header: "Provider",
      render: (provider) => (
        <>
          <div>{provider.name}</div>
          <div className={`${styles.muted} ${styles.code}`}>{provider.base_url}</div>
          {provider.description && <div className={styles.muted}>{provider.description}</div>}
        </>
      ),
    },
    {
      key: "type",
      header: "Type",
      render: (provider) => (
        <>
          {/* The label, not the internal name: the operator chose "Cortecs
              (router)" and should see that back. */}
          <div>{pluginLabel(plugins.data, provider.plugin)}</div>
          <div className={styles.chips}>
            {provider.kind === "router" && <Badge tone="accent">router</Badge>}
            {provider.billing_mode === "provider_reported" && (
              <Badge tone="warn">bills from provider</Badge>
            )}
            {/* A named plugin that disagrees with the stored kind is a
                configuration to point at, not a silent inconsistency. */}
            {provider.plugin_kind !== null && provider.plugin_kind !== provider.kind && (
              <Badge tone="danger">kind mismatch</Badge>
            )}
            {/* Unpriced models reserve nothing, so no cost ceiling trips. */}
            {provider.unpriced_model_count > 0 && (
              <Badge tone="warn">{provider.unpriced_model_count} unpriced</Badge>
            )}
          </div>
        </>
      ),
    },
    {
      key: "credential",
      header: "API key",
      render: (provider) =>
        provider.has_api_key ? (
          // Only ever a hint: the key itself is write-only and never leaves the
          // gateway (ADR 0027).
          <span className={styles.code}>{provider.api_key_hint}</span>
        ) : (
          <span className={styles.muted}>none</span>
        ),
    },
    {
      key: "models",
      header: "Models",
      numeric: true,
      render: (provider) => provider.model_count.toLocaleString(),
    },
    {
      key: "status",
      header: "Status",
      render: (provider) => (
        <div className={styles.chips}>
          {provider.is_active ? <Badge tone="ok">Active</Badge> : <Badge>Inactive</Badge>}
          <TestBadge result={results[provider.id]} />
        </div>
      ),
    },
    {
      key: "actions",
      header: "",
      render: (provider) => (
        <div className={styles.rowActions}>
          <Button
            busy={test.isPending && test.variables === provider.id}
            onClick={() => runTest(provider)}
          >
            Test
          </Button>
          <Button onClick={() => setEditing(provider)}>Edit</Button>
          <Button
            busy={update.isPending && update.variables?.id === provider.id}
            onClick={() => update.mutate({ id: provider.id, is_active: !provider.is_active })}
          >
            {provider.is_active ? "Deactivate" : "Activate"}
          </Button>
          <Button
            variant="ghost"
            busy={remove.isPending && remove.variables === provider.id}
            onClick={() => remove.mutate(provider.id)}
          >
            Delete
          </Button>
        </div>
      ),
    },
  ];

  const failures = Object.entries(results).filter(([, result]) => !result.ok);

  return (
    <div className={styles.page}>
      <PageHeader
        title="Providers"
        subtitle="The inference endpoints models are served from. An API key entered here is
          encrypted before it is stored and is never shown again — only a hint, enough to tell two
          keys apart."
        actions={
          <Button variant="primary" onClick={() => setCreating(true)}>
            Add provider
          </Button>
        }
      />

      {remove.error ? (
        <Notice tone="danger" title="Could not delete the provider">
          {remove.error instanceof Error ? remove.error.message : "Unknown error."}
        </Notice>
      ) : null}

      {failures.map(([id, result]) => {
        const provider = providers.data?.items.find((entry) => entry.id === id);
        return (
          <Notice key={id} tone="warn" title={`${provider?.name ?? "Provider"} is not reachable`}>
            {result.detail}
          </Notice>
        );
      })}

      <Card flush>
        {providers.isPending ? (
          <Spinner label="Loading providers" />
        ) : providers.error ? (
          <Notice tone="danger" title="Could not load providers">
            {providers.error instanceof Error ? providers.error.message : "Unknown error."}
          </Notice>
        ) : (
          <Table
            columns={columns}
            rows={providers.data?.items ?? []}
            rowKey={(provider) => provider.id}
            empty="No providers configured. Nothing can be served until one exists."
            caption="Inference endpoints, their credentials and how many models they serve."
          />
        )}
      </Card>

      <ProviderDialog
        open={creating}
        provider={null}
        onClose={() => setCreating(false)}
      />
      <ProviderDialog
        open={editing !== null}
        provider={editing}
        onClose={() => setEditing(null)}
      />
    </div>
  );
}

function TestBadge({ result }: { result: ProviderTestResult | undefined }) {
  if (!result) return null;
  if (!result.ok) return <Badge tone="danger">Test failed</Badge>;
  return (
    <Badge tone="ok">
      {result.model_count ?? 0} models{result.latency_ms ? ` · ${result.latency_ms}ms` : ""}
    </Badge>
  );
}

/** A provider type's human label, falling back to its name if it is not installed. */
function pluginLabel(plugins: ProviderPlugin[] | undefined, name: string | null): string {
  const match = (plugins ?? []).find((entry) =>
    name === null ? entry.is_default : entry.name === name,
  );
  return match?.label ?? name ?? "OpenAI-compatible";
}

/** The default type's name, whose value in the selector is the empty string. */
function defaultPluginName(plugins: ProviderPlugin[] | undefined): string {
  return plugins?.find((entry) => entry.is_default)?.name ?? "generic";
}

/**
 * Create and edit share a dialog, because the fields are the same and the only
 * real difference is what happens to the API key.
 *
 * On edit the key field starts empty and an empty field means "leave it alone" —
 * matching the API's three-way convention. Pre-filling it with the hint would be
 * worse than useless: saving would then store the mask as the credential.
 */
function ProviderDialog({
  open,
  provider,
  onClose,
}: {
  open: boolean;
  provider: AdminProvider | null;
  onClose: () => void;
}) {
  const create = useCreateProvider();
  const update = useUpdateProvider();
  const editing = provider !== null;

  const [name, setName] = useState("");
  const [baseUrl, setBaseUrl] = useState("");
  const [description, setDescription] = useState("");
  const [apiKey, setApiKey] = useState("");
  const [clearKey, setClearKey] = useState(false);
  const [streamOptions, setStreamOptions] = useState(true);
  const [authScheme, setAuthScheme] = useState<"bearer" | "x_api_key">("bearer");
  // The provider *type*. Read from the API rather than hardcoded, so installing
  // a plugin makes it selectable without a console release (ADR 0032).
  const plugins = useProviderPlugins();
  const [plugin, setPlugin] = useState<string>("");
  const [billingMode, setBillingMode] = useState<"own_prices" | "provider_reported">("own_prices");
  // Keyed remount: without this the fields keep the previous provider's values
  // when a different row is opened.
  const [loadedFor, setLoadedFor] = useState<string | null>(null);

  if (open && loadedFor !== (provider?.id ?? "new")) {
    setLoadedFor(provider?.id ?? "new");
    setName(provider?.name ?? "");
    setBaseUrl(provider?.base_url ?? "");
    setDescription(provider?.description ?? "");
    setApiKey("");
    setClearKey(false);
    setStreamOptions(provider?.forward_stream_options ?? true);
    setAuthScheme(provider?.auth_scheme ?? "bearer");
    setPlugin(provider?.plugin ?? "");
    setBillingMode(provider?.billing_mode ?? "own_prices");
  }

  const chosen = (plugins.data ?? []).find(
    (entry) => entry.name === (plugin || defaultPluginName(plugins.data)),
  );
  // Only the modes this type can support, so the form cannot offer a
  // configuration the API would refuse.
  const modes = chosen?.billing_modes ?? ["own_prices"];
  const effectiveMode = modes.includes(billingMode) ? billingMode : "own_prices";

  const pending = create.isPending || update.isPending;
  const error = create.error ?? update.error;

  const submit = () => {
    const done = { onSuccess: onClose };
    if (editing && provider) {
      update.mutate(
        {
          id: provider.id,
          name,
          base_url: baseUrl,
          description: description || null,
          forward_stream_options: streamOptions,
          auth_scheme: authScheme,
          plugin: plugin || null,
          // Taken from the plugin rather than asked for separately: whether the
          // serving endpoint is chosen per request is a property of the
          // counterparty, not an opinion an operator should have to hold.
          kind: chosen?.kind ?? "provider",
          billing_mode: effectiveMode,
          // Three ways, deliberately: a typed key replaces, the explicit clear
          // removes, and neither leaves the stored credential untouched.
          ...(apiKey ? { api_key: apiKey } : clearKey ? { api_key: "" } : {}),
        },
        done,
      );
    } else {
      create.mutate(
        {
          name,
          base_url: baseUrl,
          description: description || null,
          forward_stream_options: streamOptions,
          auth_scheme: authScheme,
          plugin: plugin || null,
          kind: chosen?.kind ?? "provider",
          billing_mode: effectiveMode,
          ...(apiKey ? { api_key: apiKey } : {}),
        },
        done,
      );
    }
  };

  return (
    <Dialog
      open={open}
      title={editing ? `Edit ${provider?.name}` : "Add a provider"}
      onClose={onClose}
      footer={
        <>
          <Button onClick={onClose}>Cancel</Button>
          <Button
            variant="primary"
            busy={pending}
            disabled={!baseUrl.trim() || !name.trim()}
            onClick={submit}
          >
            {editing ? "Save" : "Add"}
          </Button>
        </>
      }
    >
      {error ? (
        <Notice tone="danger">
          {error instanceof Error ? error.message : "Unknown error."}
        </Notice>
      ) : null}

      <Input
        label="Name"
        value={name}
        onChange={(event) => setName(event.target.value)}
        placeholder="cortecs"
        hint={
          editing
            ? "Letters, digits, dot, dash and underscore. Renaming is safe: models" +
              " reference the provider by id and past spend keeps its attribution." +
              " It does change owned_by on every /v1/models card this provider serves."
            : "Letters, digits, dot, dash and underscore."
        }
      />

      <Select
        label="Type"
        value={plugin}
        onChange={(event) => setPlugin(event.target.value)}
        hint={
          chosen
            ? chosen.description
            : "How this counterparty is talked to, and what may be believed about what it charged."
        }
      >
        {(plugins.data ?? []).map((entry) => (
          <option key={entry.name} value={entry.is_default ? "" : entry.name}>
            {entry.label}
            {entry.kind === "router" ? " — chooses an endpoint per request" : ""}
          </option>
        ))}
      </Select>

      {/* Only offered where the type can support more than one, and only a type
          that asserts its reported figure is the real charge can. */}
      {modes.length > 1 && (
        <Select
          label="Billing"
          value={effectiveMode}
          onChange={(event) =>
            setBillingMode(event.target.value as "own_prices" | "provider_reported")
          }
          hint="Which figure is charged. Both are always recorded, so a divergence stays
            reconstructable either way."
        >
          <option value="own_prices">Our prices — tokens counted here</option>
          <option value="provider_reported">
            The provider's reported cost — pass-through
          </option>
        </Select>
      )}

      {effectiveMode === "provider_reported" && (
        <Notice tone="warn">
          Prices are still needed: admission happens before the request and the provider's
          figure only arrives after, so an unpriced model reserves nothing and no cost
          ceiling ever trips for it.
        </Notice>
      )}

      <Input
        label="Base URL"
        value={baseUrl}
        onChange={(event) => setBaseUrl(event.target.value)}
        placeholder="https://api.example.com/v1"
        hint="Include the version path. Most OpenAI-compatible endpoints end in /v1."
      />

      <Input
        label="Description"
        value={description}
        onChange={(event) => setDescription(event.target.value)}
        placeholder="Commercial API, EU region"
      />

      <Input
        label="API key"
        type="password"
        value={apiKey}
        onChange={(event) => setApiKey(event.target.value)}
        autoComplete="new-password"
        placeholder={editing && provider?.has_api_key ? "unchanged" : "leave empty if none needed"}
        hint={
          editing && provider?.has_api_key
            ? `Currently ${provider.api_key_hint}. Type a new key to replace it, or leave empty to keep it.`
            : "Encrypted before storage. A local vLLM or Ollama usually needs none."
        }
      />

      {editing && provider?.has_api_key && (
        <label className={styles.checkItem}>
          <input
            type="checkbox"
            checked={clearKey}
            disabled={apiKey.length > 0}
            onChange={(event) => setClearKey(event.target.checked)}
          />
          <span>Remove the stored key</span>
        </label>
      )}

      <label className={styles.checkItem}>
        <input
          type="checkbox"
          checked={streamOptions}
          onChange={(event) => setStreamOptions(event.target.checked)}
        />
        <span>
          Ask for token usage on streamed responses
          <span className={styles.muted}>
            {" "}— turn off for a provider that sends usage anyway and rejects unknown
            parameters. Cortecs is one.
          </span>
        </span>
      </label>

      <Select
        label="Credential header"
        value={authScheme}
        onChange={(event) => setAuthScheme(event.target.value as "bearer" | "x_api_key")}
        hint="Anthropic's own API takes x-api-key and rejects a bearer token. Every
          OpenAI-compatible endpoint — including Cortecs, for all of its routes — takes bearer."
      >
        <option value="bearer">Authorization: Bearer</option>
        <option value="x_api_key">x-api-key (Anthropic)</option>
      </Select>

      <Notice tone="info">
        Use <strong>Test</strong> after saving. It calls the provider&apos;s <code>/models</code>
        {" "}with the credential as stored, so a wrong URL or a stale key shows up now rather than
        in someone&apos;s request.
      </Notice>
    </Dialog>
  );
}
