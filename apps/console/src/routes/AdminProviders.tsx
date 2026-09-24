import {
  Badge,
  Button,
  Card,
  Dialog,
  Input,
  Notice,
  Select,
  Spinner,
  SummaryStrip,
  Table,
  
} from "@llmp/ui";
import type { Column } from "@llmp/ui";
import { Tooltip } from "@llmp/ui";
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
import { CHIPS, CHECK_ITEM, CODE, MUTED, PAGE, ROW_ACTIONS } from "../lib/layout";
import { useOptionalToast } from "../lib/toast";


export function AdminProviders() {
  const providers = useProviders();
  const plugins = useProviderPlugins();
  const update = useUpdateProvider();
  const remove = useDeleteProvider();
  const test = useTestProvider();
  const toast = useOptionalToast();

  const [creating, setCreating] = useState(false);
  const [editing, setEditing] = useState<AdminProvider | null>(null);
  const [results, setResults] = useState<Record<string, ProviderTestResult>>({});

  // Search backends are a different concept and have their own screen
  // (ADR 0071): they answer /v1/search, are metered as counts, and were
  // presented here in the vocabulary of inference providers — which is how
  // the confusion started.
  const items = (providers.data?.items ?? []).filter((p) => p.kind !== "search");

  const runTest = (provider: AdminProvider) => {
    test.mutate(provider.id, {
      onSuccess: (result) => {
        setResults((current) => ({ ...current, [provider.id]: result }));
        // The row's badge keeps the result on the record; the toast only marks
        // the round trip itself, the same acknowledgment the other writes give.
        toast?.add({
          title: result.ok ? "Provider reachable" : "Provider is not reachable",
          type: result.ok ? "success" : "error",
        });
      },
      onError: () => toast?.add({ title: "Could not test the provider", type: "error" }),
    });
  };

  const columns: Column<AdminProvider>[] = [
    {
      key: "name",
      header: "Provider",
      render: (provider) => (
        <>
          <div>{provider.name}</div>
          <div className={`${MUTED} ${CODE}`}>{provider.base_url}</div>
          {provider.description && <div className={MUTED}>{provider.description}</div>}
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
          <div className={CHIPS}>
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
          <span className={CODE}>{provider.api_key_hint}</span>
        ) : (
          <span className={MUTED}>none</span>
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
        <div className={CHIPS}>
          {provider.is_active ? <Badge tone="ok">Active</Badge> : <Badge>Inactive</Badge>}
          <TestBadge result={results[provider.id]} />
        </div>
      ),
    },
    {
      key: "actions",
      header: "",
      render: (provider) => (
        <div className={ROW_ACTIONS}>
          {/* So a wrong URL or a stale key shows up here rather than in a
              request to a model. Said here, where the button is — it spent a
              while as a notice inside the create dialog, explaining an action
              that did not exist yet. */}
          <Tooltip label="Calls /models with the stored credential — no model request is made.">
            <Button
              busy={test.isPending && test.variables === provider.id}
              onClick={() => runTest(provider)}
            >
              Test
            </Button>
          </Tooltip>
          <Button onClick={() => setEditing(provider)}>Edit</Button>
          <Button
            busy={update.isPending && update.variables?.id === provider.id}
            onClick={() =>
              update.mutate(
                { id: provider.id, is_active: !provider.is_active },
                {
                  onSuccess: () => toast?.add({ title: "Provider updated", type: "success" }),
                  onError: () =>
                    toast?.add({ title: "Could not update the provider", type: "error" }),
                },
              )
            }
          >
            {provider.is_active ? "Deactivate" : "Activate"}
          </Button>
          <Button
            variant="ghost"
            className="text-danger"
            busy={remove.isPending && remove.variables === provider.id}
            onClick={() =>
              remove.mutate(provider.id, {
                onSuccess: () => toast?.add({ title: "Provider deleted", type: "success" }),
                onError: () =>
                  toast?.add({ title: "Could not delete the provider", type: "error" }),
              })
            }
          >
            Delete
          </Button>
        </div>
      ),
    },
  ];

  const failures = Object.entries(results).filter(([, result]) => !result.ok);

  return (
    <div className={PAGE}>
      <PageHeader
        title="Providers"
        subtitle="The endpoints models are served from. API keys are encrypted and never
          shown again."
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
          <>
            <div className="p-5">
              <SummaryStrip
                headline={`${items.length} ${items.length === 1 ? "provider" : "providers"}`}
                detail={providerState(items)}
                active={items.length > 0}
              />
            </div>
            <Table
              columns={columns}
              rows={items}
              rowKey={(provider) => provider.id}
              empty="No providers configured. Nothing can be served until one exists."
              caption="Inference endpoints, their credentials and how many models they serve."
            />
          </>
        )}
      </Card>

      <ProviderDialog
        open={creating}
        provider={null}
        pluginFilter={isLlmProviderPlugin}
        onClose={() => setCreating(false)}
      />
      <ProviderDialog
        open={editing !== null}
        provider={editing}
        pluginFilter={isLlmProviderPlugin}
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

/**
 * The strip's state line, drawn from the same facts the rows badge: how many
 * are switched off, and how many models across them have no price (which is
 * what "reserves nothing" looks like from this screen).
 */
function providerState(items: AdminProvider[]): string {
  if (items.length === 0) return "nothing can be served until one exists";
  const off = items.filter((provider) => !provider.is_active).length;
  const unpriced = items.reduce((sum, provider) => sum + provider.unpriced_model_count, 0);
  const parts: string[] = [];
  parts.push(off === 0 ? "all active" : `${off} deactivated`);
  if (unpriced > 0) parts.push(`${unpriced} unpriced`);
  return parts.join(" · ");
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
 * The kinds the Providers screen owns: inference providers and routers.
 *
 * Search backends live on the Search screen (ADR 0071) and the extractor is
 * the deployment's own plumbing — the ProviderPlugin doc comment says it
 * never belongs in an LLM-provider picker, mirroring the AdminModels
 * treatment. Kept at the call site rather than in the shared dialog because
 * the dialog is kind-agnostic by design: each screen owns its kinds, and the
 * search screen passes the opposite rule.
 */
function isLlmProviderPlugin(plugin: ProviderPlugin): boolean {
  return plugin.kind === "provider" || plugin.kind === "router";
}

/**
 * Create and edit share a dialog, because the fields are the same and the only
 * real difference is what happens to the API key.
 *
 * On edit the key field starts empty and an empty field means "leave it alone" —
 * matching the API's three-way convention. Pre-filling it with the hint would be
 * worse than useless: saving would then store the mask as the credential.
 */
export function ProviderDialog({
  open,
  provider,
  onClose,
  pluginFilter,
}: {
  open: boolean;
  provider: AdminProvider | null;
  onClose: () => void;
  /** A screen that owns one kind of provider offers only those types — the
   * search screen is not a place to create an inference provider. */
  pluginFilter?: (plugin: ProviderPlugin) => boolean;
}) {
  const create = useCreateProvider();
  const update = useUpdateProvider();
  const toast = useOptionalToast();
  const editing = provider !== null;

  const [name, setName] = useState("");
  const [baseUrl, setBaseUrl] = useState("");
  const [description, setDescription] = useState("");
  const [apiKey, setApiKey] = useState("");
  const [clearKey, setClearKey] = useState(false);
  // The provider *type*. Read from the API rather than hardcoded, so installing
  // a plugin makes it selectable without a console release (ADR 0032).
  const allPlugins = useProviderPlugins();
  // The filtered list, plus the row's own type when editing: a provider whose
  // plugin the filter would hide (the extractor row opened here) must keep
  // its Type selectable rather than blanking it. Creation needs no such
  // exception — there is no current value to preserve.
  const plugins = pluginFilter
    ? {
        ...allPlugins,
        data: allPlugins.data?.filter(
          (entry) =>
            pluginFilter(entry) ||
            (editing &&
              (provider?.plugin === entry.name ||
                (provider?.plugin == null && entry.is_default))),
        ),
      }
    : allPlugins;
  const [plugin, setPlugin] = useState<string>("");
  const [billingMode, setBillingMode] = useState<"own_prices" | "provider_reported">("own_prices");
  const [prefix, setPrefix] = useState("");
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
    setPlugin(provider?.plugin ?? "");
    setBillingMode(provider?.billing_mode ?? "own_prices");
    setPrefix(provider?.prefix ?? "");
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
    if (editing && provider) {
      update.mutate(
        {
          id: provider.id,
          name,
          base_url: baseUrl,
          description: description || null,
          plugin: plugin || null,
          // Taken from the plugin rather than asked for separately: whether the
          // serving endpoint is chosen per request is a property of the
          // counterparty, not an opinion an operator should have to hold.
          kind: chosen?.kind ?? "provider",
          billing_mode: effectiveMode,
          prefix,
          // Three ways, deliberately: a typed key replaces, the explicit clear
          // removes, and neither leaves the stored credential untouched.
          ...(apiKey ? { api_key: apiKey } : clearKey ? { api_key: "" } : {}),
        },
        {
          onSuccess: () => {
            toast?.add({ title: "Provider updated", type: "success" });
            onClose();
          },
          onError: () =>
            toast?.add({ title: "Could not update the provider", type: "error" }),
        },
      );
    } else {
      create.mutate(
        {
          name,
          base_url: baseUrl,
          description: description || null,
          plugin: plugin || null,
          kind: chosen?.kind ?? "provider",
          billing_mode: effectiveMode,
          prefix,
          ...(apiKey ? { api_key: apiKey } : {}),
        },
        {
          onSuccess: () => {
            toast?.add({ title: "Provider created", type: "success" });
            onClose();
          },
          onError: () =>
            toast?.add({ title: "Could not create the provider", type: "error" }),
        },
      );
    }
  };

  return (
    <Dialog
      open={open}
      title={editing ? `Edit ${provider?.name}` : "New provider"}
      onClose={onClose}
      footer={
        <>
          <Button onClick={onClose}>Cancel</Button>
          <Button
            variant="primary"
            busy={pending}
            // An empty endpoint is fine when the type supplies one — the API
            // applies the plugin's default — and refused otherwise.
            disabled={!name.trim() || (!baseUrl.trim() && !chosen?.default_base_url)}
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
            ? "Letters, digits, dot, dash and underscore. Renaming is safe; it changes" +
              " owned_by on this provider's /v1/models cards."
            : "Letters, digits, dot, dash and underscore."
        }
      />

      <Input
        label="Model name prefix"
        value={prefix}
        onChange={(event) => setPrefix(event.target.value)}
        placeholder="none"
        hint={
          `Prepended to every model this provider contributes — two vendors both selling a "deep"` +
          ` become ${prefix ? `"${prefix}deep"` : `"deep"`} and a second vendor's. Saving also renames` +
          " the models already here."
        }
      />

      <Select
        label="Type"
        value={plugin}
        onChange={(event) => {
          const next = event.target.value;
          // Choosing a type is choosing its endpoint, when the type has one
          // worth knowing: pre-fill it, so creating a Cortecs provider is
          // typing a name and a key. A URL that is not some plugin's default
          // is the operator's own — a private gateway, a proxy — and is never
          // clobbered; one that is was put there by this same rule and follows
          // the type.
          const byName = (name: string) =>
            (plugins.data ?? []).find((entry) => entry.name === name || (name === "" && entry.is_default));
          const previous = byName(plugin || defaultPluginName(plugins.data));
          const upcoming = byName(next || defaultPluginName(plugins.data));
          if (
            upcoming?.default_base_url &&
            (baseUrl.trim() === "" || baseUrl.trim() === (previous?.default_base_url ?? ""))
          ) {
            setBaseUrl(upcoming.default_base_url);
          }
          setPlugin(next);
        }}
        hint={
          chosen
            ? chosen.description
            : "How it is talked to, and whether its reported cost can be believed."
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
          hint="Which figure is charged. Both are recorded either way."
        >
          <option value="own_prices">Our prices — tokens counted here</option>
          <option value="provider_reported">
            The provider's reported cost — pass-through
          </option>
        </Select>
      )}

      {/* Admission happens before the request and the provider's figure arrives
          after it, which is why prices are still needed in this mode. */}
      {effectiveMode === "provider_reported" && (
        <Notice tone="warn">
          Prices are still needed: an unpriced model reserves nothing, so no cost ceiling
          trips.
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
            ? `Currently ${provider.api_key_hint}. Type a new key to replace it; empty keeps it.`
            : "Encrypted before storage. A local vLLM or Ollama usually needs none."
        }
      />

      {editing && provider?.has_api_key && (
        <label className={CHECK_ITEM}>
          <input
            type="checkbox"
            checked={clearKey}
            disabled={apiKey.length > 0}
            onChange={(event) => setClearKey(event.target.checked)}
          />
          <span>Remove the stored key</span>
        </label>
      )}

    </Dialog>
  );
}
