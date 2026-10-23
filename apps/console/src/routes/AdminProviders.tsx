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
  RowActions,
} from "@llmp/ui";
import type { Column } from "@llmp/ui";
import { useState } from "react";
import {
  useCreateProvider,
  useDeleteProvider,
  useProviderCatalogue,
  useProviderPlugins,
  useProviders,
  useTestProvider,
  useUpdateProvider,
} from "../lib/admin";
import type {
  AdminProvider,
  CatalogueEntry,
  ProviderPlugin,
  ProviderTestResult,
} from "../lib/types";
import { PageHeader } from "../components/PageHeader";
import {
  CHIPS,
  CHECK_ITEM,
  CLAMP,
  CODE,
  FINGERPRINT,
  MUTED,
  NOWRAP,
  PAGE,
  ROW_ACTIONS,
  shortHint,
} from "../lib/layout";
import { useOptionalToast } from "../lib/toast";
import { formatNumber } from "../lib/format";
import { m } from "../paraglide/messages.js";


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
          title: result.ok
            ? m["admin_providers.toast.reachable"]()
            : m["admin_providers.toast.unreachable"](),
          type: result.ok ? "success" : "error",
        });
      },
      onError: () =>
        toast?.add({ title: m["admin_providers.toast.test_failed"](), type: "error" }),
    });
  };

  const columns: Column<AdminProvider>[] = [
    {
      key: "name",
      header: m["admin_providers.column.provider"](),
      render: (provider) => (
        <>
          <div>{provider.name}</div>
          <div className={`${MUTED} ${CODE} ${CLAMP}`} title={provider.base_url}>
            {provider.base_url}
          </div>
          {/* The row keeps the endpoint it was created with; the catalogue may
              have moved on. Say so and offer the one-click fix, never rewrite
              it silently. */}
          {endpointDiffers(provider) && (
            <div className={CHIPS}>
              <Badge tone="warn">{m["admin_providers.badge.endpoint_differs"]()}</Badge>
              <Button
                variant="secondary"
                title={provider.catalogue?.base_url ?? ""}
                disabled={update.isPending && update.variables?.id === provider.id}
                onClick={() =>
                  update.mutate(
                    { id: provider.id, base_url: provider.catalogue?.base_url ?? "" },
                    {
                      onSuccess: () =>
                        toast?.add({ title: m["admin_providers.toast.updated"](), type: "success" }),
                      onError: () =>
                        toast?.add({
                          title: m["admin_providers.toast.update_failed"](),
                          type: "error",
                        }),
                    },
                  )
                }
              >
                {m["admin_providers.action.use_catalogue_endpoint"]()}
              </Button>
            </div>
          )}
          {provider.description && (
            <div className={`${MUTED} ${CLAMP}`} title={provider.description}>
              {provider.description}
            </div>
          )}
        </>
      ),
    },
    {
      key: "type",
      hideBelow: "md",
      header: m["admin_providers.column.type"](),
      render: (provider) => (
        <>
          {/* The label, not the internal name: the operator chose "Cortecs
              (router)" and should see that back. */}
          <div className={NOWRAP}>{pluginLabel(plugins.data, provider.plugin)}</div>
          <div className={CHIPS}>
            {provider.kind === "router" && (
              <Badge tone="accent">{m["admin_providers.badge.router"]()}</Badge>
            )}
            {provider.billing_mode === "provider_reported" && (
              <Badge tone="warn">{m["admin_providers.badge.bills_from_provider"]()}</Badge>
            )}
            {/* A named plugin that disagrees with the stored kind is a
                configuration to point at, not a silent inconsistency. */}
            {provider.plugin_kind !== null && provider.plugin_kind !== provider.kind && (
              <Badge tone="danger">{m["admin_providers.badge.kind_mismatch"]()}</Badge>
            )}
            {/* What the catalogue says about where it runs and what it keeps.
                Unknown stays unmarked: absence is not a "no". */}
            {provider.catalogue?.region && (
              <Badge tone="neutral">
                {m["admin_providers.badge.region"]({
                  region: regionLabel(provider.catalogue.region),
                })}
              </Badge>
            )}
            {provider.catalogue?.retention.zero_data_retention === true && (
              <Badge tone="ok">{m["admin_providers.badge.zero_retention"]()}</Badge>
            )}
            {provider.catalogue_missing && (
              <Badge tone="warn">{m["admin_providers.badge.catalogue_missing"]()}</Badge>
            )}
            {/* Unpriced models reserve nothing, so no cost ceiling trips. */}
            {provider.unpriced_model_count > 0 && (
              <Badge tone="warn">
                {m["admin_providers.badge.unpriced"]({ count: provider.unpriced_model_count })}
              </Badge>
            )}
          </div>
        </>
      ),
    },
    {
      key: "credential",
      hideBelow: "lg",
      header: m["admin_providers.column.api_key"](),
      render: (provider) =>
        provider.has_api_key ? (
          // Only ever a hint: the key itself is write-only and never leaves the
          // gateway (ADR 0027).
          <span className={`${CODE} ${FINGERPRINT}`} title={provider.api_key_hint ?? undefined}>
            {shortHint(provider.api_key_hint)}
          </span>
        ) : (
          <span className={MUTED}>{m["admin_providers.no_key"]()}</span>
        ),
    },
    {
      key: "models",
      hideBelow: "sm",
      header: m["admin_providers.column.models"](),
      numeric: true,
      render: (provider) => formatNumber(provider.model_count),
    },
    {
      key: "status",
      header: m["admin_providers.column.status"](),
      render: (provider) => (
        <div className={CHIPS}>
          {provider.is_active ? (
            <Badge tone="ok">{m["admin_providers.badge.active"]()}</Badge>
          ) : (
            <Badge>{m["admin_providers.badge.inactive"]()}</Badge>
          )}
          <TestBadge result={results[provider.id]} />
        </div>
      ),
    },
    {
      key: "actions",
      header: "",
      render: (provider) => (
        <div className={ROW_ACTIONS}>
          <Button variant="secondary" onClick={() => setEditing(provider)}>
            {m["admin_providers.action.edit"]()}
          </Button>
          <RowActions
            label={m["table.row_actions"]({ name: provider.name })}
            actions={[
              {
                key: "test",
                label: m["admin_providers.action.test"](),
                onSelect: () => runTest(provider),
                disabled: test.isPending && test.variables === provider.id,
              },
              {
                key: "toggle",
                label: provider.is_active
                  ? m["admin_providers.action.deactivate"]()
                  : m["admin_providers.action.activate"](),
                onSelect: () =>
                  update.mutate(
                    { id: provider.id, is_active: !provider.is_active },
                    {
                      onSuccess: () =>
                        toast?.add({ title: m["admin_providers.toast.updated"](), type: "success" }),
                      onError: () =>
                        toast?.add({
                          title: m["admin_providers.toast.update_failed"](),
                          type: "error",
                        }),
                    },
                  ),
                disabled: update.isPending && update.variables?.id === provider.id,
              },
              {
                key: "delete",
                label: m["admin_providers.action.delete"](),
                onSelect: () =>
                  remove.mutate(provider.id, {
                    onSuccess: () =>
                      toast?.add({ title: m["admin_providers.toast.deleted"](), type: "success" }),
                    onError: () =>
                      toast?.add({
                        title: m["admin_providers.toast.delete_failed"](),
                        type: "error",
                      }),
                  }),
                destructive: true,
                disabled:
                  provider.kind === "internal" || (remove.isPending && remove.variables === provider.id),
              },
            ]}
          />
        </div>
      ),
    },
  ];

  const failures = Object.entries(results).filter(([, result]) => !result.ok);

  return (
    <div className={PAGE}>
      <PageHeader
        title={m["admin_providers.title"]()}
        subtitle={m["admin_providers.subtitle"]()}
        actions={
          <Button variant="primary" onClick={() => setCreating(true)}>
            {m["admin_providers.add"]()}
          </Button>
        }
      />

      {remove.error ? (
        <Notice tone="danger" title={m["admin_providers.delete_error.title"]()}>
          {/* i18n: server text — the API's own error message. */}
          {remove.error instanceof Error ? remove.error.message : m["admin_providers.unknown_error"]()}
        </Notice>
      ) : null}

      {failures.map(([id, result]) => {
        const provider = providers.data?.items.find((entry) => entry.id === id);
        return (
          <Notice
            key={id}
            tone="warn"
            title={m["admin_providers.unreachable_title"]({
              // i18n: server text — the provider's own name.
              provider: provider?.name ?? m["admin_providers.provider_fallback"](),
            })}
          >
            {/* i18n: server text — the test result's own detail. */}
            {result.detail}
          </Notice>
        );
      })}

      <Card flush>
        {providers.isPending ? (
          <Spinner label={m["admin_providers.loading"]()} />
        ) : providers.error ? (
          <Notice tone="danger" title={m["admin_providers.load_error.title"]()}>
            {/* i18n: server text — the API's own error message. */}
            {providers.error instanceof Error
              ? providers.error.message
              : m["admin_providers.unknown_error"]()}
          </Notice>
        ) : (
          <>
            <div className="p-5">
              <SummaryStrip
                headline={m["admin_providers.count"]({ count: items.length })}
                detail={providerState(items)}
                active={items.length > 0}
              />
            </div>
            <Table
              columns={columns}
              rows={items}
              rowKey={(provider) => provider.id}
              empty={m["admin_providers.empty"]()}
              caption={m["admin_providers.caption"]()}
            />
          </>
        )}
      </Card>

      <CatalogueCard />

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

/**
 * The provider catalogue (YAML), read-only: each value with where it came from.
 *
 * The catalogue is files, edited where they live and applied on restart, so
 * there is nothing to edit here. What the screen is for is the question an
 * operator asks when a value surprises them: "is that the release's, or mine?".
 * Renders nothing while loading or when the request fails, because the page's
 * real content is the providers above it.
 */
function CatalogueCard() {
  const catalogue = useProviderCatalogue();
  const entries = Array.isArray(catalogue.data) ? catalogue.data : [];
  if (entries.length === 0) return null;

  const columns: Column<CatalogueEntry>[] = [
    {
      key: "entry",
      header: m["admin_providers.catalogue.column.entry"](),
      render: (entry) => (
        <>
          {/* i18n: server text — the catalogue file's own name and id. */}
          <div>{entry.name}</div>
          <div className={`${MUTED} ${CODE}`}>{entry.id}</div>
        </>
      ),
    },
    {
      key: "endpoint",
      header: m["admin_providers.catalogue.column.endpoint"](),
      render: (entry) => (
        <>
          <div className={CODE}>{entry.base_url ?? m["admin_providers.catalogue.unknown"]()}</div>
          <Origin entry={entry} fields={["base_url", "plugin"]} />
        </>
      ),
    },
    {
      key: "region",
      header: m["admin_providers.catalogue.column.region"](),
      render: (entry) => (
        <>
          <div>{regionLabel(entry.region)}</div>
          {/* i18n: server text — the catalogue file's own note. */}
          {entry.region_note && <div className={`${MUTED} text-xs`}>{entry.region_note}</div>}
          <Origin entry={entry} fields={["region", "region_note"]} />
        </>
      ),
    },
    {
      key: "retention",
      header: m["admin_providers.catalogue.column.retention"](),
      render: (entry) => (
        <>
          <div>{retentionText(entry)}</div>
          {/* i18n: server text — the catalogue file's own note. */}
          {entry.retention.note && (
            <div className={`${MUTED} text-xs`}>{entry.retention.note}</div>
          )}
          <Origin
            entry={entry}
            fields={[
              "retention.status",
              "retention.zero_data_retention",
              "retention.zero_data_retention_available",
              "retention.retention_days",
              "retention.trains_on_customer_data",
              "retention.note",
            ]}
          />
        </>
      ),
    },
    {
      key: "sources",
      hideBelow: "lg",
      header: m["admin_providers.catalogue.column.sources"](),
      render: (entry) => <Sources entry={entry} />,
    },
    {
      key: "prices",
      header: m["admin_providers.catalogue.column.prices"](),
      numeric: true,
      render: (entry) => formatNumber(entry.price_hints.length),
    },
  ];

  return (
    <Card flush>
      <div className="p-5">
        <h2 className="text-lg font-semibold">{m["admin_providers.catalogue.title"]()}</h2>
        <p className={MUTED}>{m["admin_providers.catalogue.subtitle"]()}</p>
      </div>
      <Table
        columns={columns}
        rows={entries}
        rowKey={(entry) => entry.id}
        empty=""
        caption={m["admin_providers.catalogue.caption"]()}
      />
    </Card>
  );
}

/** The provider's stored endpoint is not the one its catalogue entry now names. */
function endpointDiffers(provider: AdminProvider): boolean {
  const wanted = provider.catalogue?.base_url;
  if (!wanted) return false;
  return wanted.replace(/\/+$/, "") !== provider.base_url.replace(/\/+$/, "");
}

/**
 * A region tag as words. ``various`` (a router) and ``not-published`` are
 * statements about what is known, not places; every other tag is shown as the
 * file wrote it, so an operator's own tags read back unchanged.
 */
function regionLabel(region: string | null): string {
  if (region === null) return m["admin_providers.catalogue.unknown"]();
  if (region === "various") return m["admin_providers.catalogue.various"]();
  if (region === "not-published") return m["admin_providers.catalogue.not_published"]();
  if (region === "global") return m["admin_providers.catalogue.global"]();
  return region;
}

/** What the entry says it keeps, in one line; unknown is said as unknown. */
function retentionText(entry: CatalogueEntry): string {
  const r = entry.retention;
  if (r.status === "various") return m["admin_providers.catalogue.various"]();
  if (r.status === "not_published") return m["admin_providers.catalogue.not_published"]();
  const parts: string[] = [];
  if (r.zero_data_retention === true) parts.push(m["admin_providers.catalogue.zdr_yes"]());
  if (r.zero_data_retention === false) parts.push(m["admin_providers.catalogue.zdr_no"]());
  if (r.retention_days !== null) {
    parts.push(m["admin_providers.catalogue.days"]({ count: r.retention_days }));
  }
  if (r.zero_data_retention !== true && r.zero_data_retention_available === true) {
    parts.push(m["admin_providers.catalogue.zdr_available"]());
  }
  if (r.trains_on_customer_data === false) parts.push(m["admin_providers.catalogue.no_training"]());
  if (r.trains_on_customer_data === true) parts.push(m["admin_providers.catalogue.trains"]());
  return parts.length > 0 ? parts.join(" · ") : m["admin_providers.catalogue.unknown"]();
}

/** The primary sources behind an entry's facts, each with the day it was read. */
function Sources({ entry }: { entry: CatalogueEntry }) {
  if (entry.sources.length === 0) return <div className={MUTED}>-</div>;
  return (
    <ul className="space-y-1 text-xs">
      {entry.sources.map((source) => (
        <li key={source.url}>
          <a
            href={source.url}
            target="_blank"
            rel="noreferrer noopener"
            className="underline"
            title={m["admin_providers.catalogue.source_title"]({
              covers: source.covers ?? "",
              date: source.retrieved,
            })}
          >
            {sourceHost(source.url)}
          </a>
        </li>
      ))}
    </ul>
  );
}

function sourceHost(url: string): string {
  try {
    return new URL(url).hostname;
  } catch {
    return url;
  }
}

/**
 * Where the shown fields came from. Built-in is the quiet default; an override
 * is marked, with the file that did it, because that is the one to look for.
 */
function Origin({ entry, fields }: { entry: CatalogueEntry; fields: string[] }) {
  const overrides = entry.origins.filter(
    (origin) => fields.includes(origin.field) && origin.layer === "override",
  );
  if (overrides.length === 0) {
    return <div className={MUTED}>{m["admin_providers.catalogue.origin_builtin"]()}</div>;
  }
  const files = [...new Set(overrides.map((origin) => origin.file))].join(", ");
  return (
    <div className={CHIPS}>
      <Badge tone="accent">{m["admin_providers.catalogue.origin_override"]()}</Badge>
      <span className={`${MUTED} ${CODE}`}>{files}</span>
    </div>
  );
}

function TestBadge({ result }: { result: ProviderTestResult | undefined }) {
  if (!result) return null;
  if (!result.ok) return <Badge tone="danger">{m["admin_providers.test.failed"]()}</Badge>;
  const parts = [m["admin_providers.test.models"]({ count: result.model_count ?? 0 })];
  if (result.latency_ms) {
    parts.push(m["admin_providers.test.latency"]({ latency: result.latency_ms }));
  }
  return <Badge tone="ok">{parts.join(" · ")}</Badge>;
}

/**
 * The strip's state line, drawn from the same facts the rows badge: how many
 * are switched off, and how many models across them have no price (which is
 * what "reserves nothing" looks like from this screen).
 */
function providerState(items: AdminProvider[]): string {
  if (items.length === 0) return m["admin_providers.state.empty"]();
  const off = items.filter((provider) => !provider.is_active).length;
  const unpriced = items.reduce((sum, provider) => sum + provider.unpriced_model_count, 0);
  // Figures separated by a middot: a list, not a sentence, so the separator
  // stays here rather than inside any one message.
  const parts: string[] = [
    off === 0
      ? m["admin_providers.state.all_active"]()
      : m["admin_providers.state.deactivated"]({ count: off }),
  ];
  if (unpriced > 0) parts.push(m["admin_providers.state.unpriced"]({ count: unpriced }));
  return parts.join(" · ");
}

/** A provider type's human label, falling back to its name if it is not installed. */
function pluginLabel(plugins: ProviderPlugin[] | undefined, name: string | null): string {
  const match = (plugins ?? []).find((entry) =>
    name === null ? entry.is_default : entry.name === name,
  );
  // i18n: server text — a plugin's own label, or the plugin's name.
  return match?.label ?? name ?? m["admin_providers.plugin.default_label"]();
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
  // The catalogue entry this row is an instance of ("" for none). Linking copies
  // nothing into the row: on create it pre-fills the fields below, which stay
  // the operator's to change; on edit it only sets the link.
  const catalogue = useProviderCatalogue();
  const [catalogueId, setCatalogueId] = useState("");
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
    setCatalogueId(provider?.catalogue_id ?? "");
  }

  const chosen = (plugins.data ?? []).find(
    (entry) => entry.name === (plugin || defaultPluginName(plugins.data)),
  );
  // Only the modes this type can support, so the form cannot offer a
  // configuration the API would refuse.
  const modes = chosen?.billing_modes ?? ["own_prices"];
  const effectiveMode = modes.includes(billingMode) ? billingMode : "own_prices";

  // Entries this screen owns: those whose plugin the screen's filter accepts, so
  // the search screen's dialog is not offered inference providers.
  const offered = (catalogue.data ?? []).filter((entry) => {
    const type = (allPlugins.data ?? []).find((p) => p.name === entry.plugin);
    return type !== undefined && (pluginFilter ? pluginFilter(type) : true);
  });
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
          // Only when it changed, so saving something else never rewrites the link.
          ...(catalogueId !== (provider.catalogue_id ?? "") ? { catalogue_id: catalogueId } : {}),
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
            toast?.add({ title: m["admin_providers.toast.update_failed"](), type: "error" }),
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
          ...(catalogueId ? { catalogue_id: catalogueId } : {}),
          ...(apiKey ? { api_key: apiKey } : {}),
        },
        {
          onSuccess: () => {
            toast?.add({ title: m["admin_providers.toast.created"](), type: "success" });
            onClose();
          },
          onError: () =>
            toast?.add({ title: m["admin_providers.toast.create_failed"](), type: "error" }),
        },
      );
    }
  };

  return (
    <Dialog
      open={open}
      title={
        editing
          ? m["admin_providers.dialog.edit_title"]({ provider: provider?.name ?? "" })
          : m["admin_providers.dialog.new_title"]()
      }
      onClose={onClose}
      footer={
        <>
          <Button onClick={onClose}>{m["admin_providers.dialog.cancel"]()}</Button>
          <Button
            variant="primary"
            busy={pending}
            // An empty endpoint is fine when the type supplies one — the API
            // applies the plugin's default — and refused otherwise.
            disabled={!name.trim() || (!baseUrl.trim() && !chosen?.default_base_url)}
            onClick={submit}
          >
            {editing ? m["admin_providers.dialog.save"]() : m["admin_providers.dialog.add"]()}
          </Button>
        </>
      }
    >
      {error ? (
        <Notice tone="danger">
          {/* i18n: server text — the API's own error message. */}
          {error instanceof Error ? error.message : m["admin_providers.unknown_error"]()}
        </Notice>
      ) : null}

      <Input
        label={m["admin_providers.field.name"]()}
        value={name}
        onChange={(event) => setName(event.target.value)}
        placeholder={m["admin_providers.field.name_placeholder"]()}
        hint={
          editing
            ? m["admin_providers.field.name_hint.edit"]()
            : m["admin_providers.field.name_hint.new"]()
        }
      />

      <Input
        label={m["admin_providers.field.prefix"]()}
        value={prefix}
        onChange={(event) => setPrefix(event.target.value)}
        placeholder={m["admin_providers.field.prefix_placeholder"]()}
        hint={m["admin_providers.field.prefix_hint"]({
          example: prefix ? `${prefix}deep` : "deep",
        })}
      />

      {offered.length > 0 && (
        <Select
          label={m["admin_providers.field.catalogue"]()}
          value={catalogueId}
          onChange={(event) => {
            const id = event.target.value;
            setCatalogueId(id);
            const entry = offered.find((candidate) => candidate.id === id);
            // Pre-fill on create only; an existing row's fields are its own.
            if (!editing && entry) {
              const known = (plugins.data ?? []).find((p) => p.name === entry.plugin);
              setPlugin(known?.is_default ? "" : entry.plugin);
              if (entry.base_url) setBaseUrl(entry.base_url);
              setPrefix(entry.prefix);
              if (!name.trim()) setName(entry.id);
            }
          }}
          hint={
            editing
              ? m["admin_providers.field.catalogue_hint_edit"]()
              : m["admin_providers.field.catalogue_hint_new"]()
          }
        >
          <option value="">{m["admin_providers.field.catalogue_none"]()}</option>
          {offered.map((entry) => (
            // i18n: server text — the catalogue entry's own name.
            <option key={entry.id} value={entry.id}>
              {entry.name}
            </option>
          ))}
        </Select>
      )}

      <Select
        label={m["admin_providers.field.type"]()}
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
          // i18n: server text — the plugin's own description.
          chosen ? chosen.description : m["admin_providers.field.type_hint"]()
        }
      >
        {(plugins.data ?? []).map((entry) => (
          <option key={entry.name} value={entry.is_default ? "" : entry.name}>
            {entry.label}
            {entry.kind === "router" ? m["admin_providers.field.type_router_suffix"]() : ""}
          </option>
        ))}
      </Select>

      {/* Only offered where the type can support more than one, and only a type
          that asserts its reported figure is the real charge can. */}
      {modes.length > 1 && (
        <Select
          label={m["admin_providers.field.billing"]()}
          value={effectiveMode}
          onChange={(event) =>
            setBillingMode(event.target.value as "own_prices" | "provider_reported")
          }
          hint={m["admin_providers.field.billing_hint"]()}
        >
          <option value="own_prices">{m["admin_providers.field.billing_own_prices"]()}</option>
          <option value="provider_reported">
            {m["admin_providers.field.billing_provider_reported"]()}
          </option>
        </Select>
      )}

      {/* Admission happens before the request and the provider's figure arrives
          after it, which is why prices are still needed in this mode. */}
      {effectiveMode === "provider_reported" && (
        <Notice tone="warn">{m["admin_providers.field.billing_warning"]()}</Notice>
      )}

      <Input
        label={m["admin_providers.field.base_url"]()}
        value={baseUrl}
        onChange={(event) => setBaseUrl(event.target.value)}
        placeholder={m["admin_providers.field.base_url_placeholder"]()}
        hint={m["admin_providers.field.base_url_hint"]()}
      />

      <Input
        label={m["admin_providers.field.description"]()}
        value={description}
        onChange={(event) => setDescription(event.target.value)}
        placeholder={m["admin_providers.field.description_placeholder"]()}
      />

      <Input
        label={m["admin_providers.field.api_key"]()}
        type="password"
        value={apiKey}
        onChange={(event) => setApiKey(event.target.value)}
        autoComplete="new-password"
        placeholder={
          editing && provider?.has_api_key
            ? m["admin_providers.field.api_key_placeholder_unchanged"]()
            : m["admin_providers.field.api_key_placeholder_none"]()
        }
        hint={
          editing && provider?.has_api_key
            ? m["admin_providers.field.api_key_hint_edit"]({
                // i18n: server text — the API's own hint for the stored key.
                hint: provider.api_key_hint,
              })
            : m["admin_providers.field.api_key_hint_new"]()
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
          <span>{m["admin_providers.field.remove_key"]()}</span>
        </label>
      )}

    </Dialog>
  );
}
