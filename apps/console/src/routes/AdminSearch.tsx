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
  useDeleteSearchBackend,
  useGroups,
  useModelAccess,
  useModels,
  useProviderPlugins,
  useProviders,
  useSetGroupSearchBackend,
  useUpdateProvider,
} from "../lib/admin";
import type { AdminProvider, ProviderPlugin } from "../lib/types";
import { useOptionalToast } from "../lib/toast";
import { PageHeader } from "../components/PageHeader";
import { CHIPS, CODE, FORM, MUTED, PAGE, ROW_ACTIONS } from "../lib/layout";
import { formatNumber } from "../lib/format";
import { Message } from "../components/Message";
import { m } from "../paraglide/messages.js";

/**
 * Web search, as its own screen (2026-09-13).
 *
 * A search backend is not a model provider and a tier is not a model: they
 * answer /v1/search, are metered as a count of requests, never priced, and
 * are absent from /v1/models unless a caller asks for them by name (ADR
 * 0071). Mixing them into the providers and models screens presented a
 * different concept in the vocabulary of another one — the reason this screen
 * exists. Everything about search lives here: the backends, their tiers, and
 * which groups may run which.
 */

export function AdminSearch() {
  const providers = useProviders();
  const plugins = useProviderPlugins();
  const models = useModels();
  const groups = useGroups();

  const [creating, setCreating] = useState(false);
  const [editing, setEditing] = useState<AdminProvider | null>(null);
  const [deleting, setDeleting] = useState<AdminProvider | null>(null);

  // The screen's own vocabulary: backends are the search providers, tiers the
  // models of kind `search`. Everything else in those listings belongs to the
  // providers and models screens.
  const backends = (providers.data?.items ?? []).filter((p) => p.kind === "search");
  const searchPlugins = (plugins.data ?? []).filter((p) => p.kind === "search");
  const tiers = (models.data?.items ?? []).filter((m) => m.kind === "search");
  const groupList = groups.data?.items ?? [];

  const access = useModelAccess();
  const setPolicy = useSetGroupSearchBackend();

  function toggleGrant(groupId: string, modelId: string, grant: boolean) {
    access.mutate({ groupId, modelId, grant });
  }

  const backendColumns: Column<AdminProvider>[] = [
    {
      key: "name",
      header: m["admin_search.column.backend"](),
      render: (backend) => (
        <>
          <div>{backend.name}</div>
          <div className={`${MUTED} ${CODE}`}>{backend.base_url}</div>
        </>
      ),
    },
    {
      key: "credential",
      header: m["admin_search.column.api_key"](),
      render: (backend) =>
        backend.has_api_key ? (
          <span className={CODE}>{backend.api_key_hint}</span>
        ) : (
          <span className={MUTED}>{m["admin_search.no_key"]()}</span>
        ),
    },
    {
      key: "tiers",
      header: m["admin_search.column.tiers"](),
      numeric: true,
      render: (backend) =>
        formatNumber(tiers.filter((tier) => tier.provider_id === backend.id).length),
    },
    {
      key: "status",
      header: m["admin_search.column.status"](),
      render: (backend) => (
        <div className={CHIPS}>
          {backend.is_active ? (
            <Badge tone="ok">{m["admin_search.badge.active"]()}</Badge>
          ) : (
            <Badge>{m["admin_search.badge.inactive"]()}</Badge>
          )}
        </div>
      ),
    },
    {
      key: "actions",
      header: "",
      render: (backend) => (
        <div className={ROW_ACTIONS}>
          <Button variant="secondary" onClick={() => setEditing(backend)}>
            {m["admin_search.action.edit"]()}
          </Button>
          <RowActions
            label={m["table.row_actions"]({ name: backend.name })}
            actions={[
              {
                key: "delete",
                label: m["admin_search.action.delete"](),
                onSelect: () => setDeleting(backend),
                destructive: true,
              },
            ]}
          />
        </div>
      ),
    },
  ];

  return (
    <div className={PAGE}>
      <PageHeader title={m["admin_search.title"]()} />

      <Notice tone="info">
        <Message
          message={m["admin_search.intro"]}
          markup={{ code: (children) => <code>{children}</code> }}
        />
      </Notice>

      {/* -- backends ------------------------------------------------ */}
      <Card title={m["admin_search.backends_card"]()} flush>
        {providers.isPending ? (
          <div className="p-6">
            <Spinner label={m["admin_search.loading_backends"]()} />
          </div>
        ) : (
          <>
            {/* The strip carries the count, the state and the action, so the
                card header stays a title and the table starts with its
                summary rather than its first row. */}
            <div className="p-5">
              <SummaryStrip
                headline={m["admin_search.count"]({ count: backends.length })}
                detail={
                  backends.length === 0
                    ? m["admin_search.state.none"]()
                    : m["admin_search.state.active"]({
                        count: backends.filter((backend) => backend.is_active).length,
                      })
                }
                active={backends.length > 0}
                actions={
                  <Button onClick={() => setCreating(true)}>{m["admin_search.add_backend"]()}</Button>
                }
              />
            </div>
            <Table
              columns={backendColumns}
              rows={backends}
              rowKey={(row) => row.id}
              empty={m["admin_search.backends_empty"]()}
            />
          </>
        )}
      </Card>

      {/* -- grants ---------------------------------------------------- */}
      <Card flush>
        <div className="p-4">
          <h2 className="text-sm font-semibold">{m["admin_search.grants_heading"]()}</h2>
          <p className={`${MUTED} mt-1 text-sm`}>{m["admin_search.grants_body"]()}</p>
        </div>
        {models.isPending ? (
          <div className="p-6">
            <Spinner label={m["admin_search.loading_tiers"]()} />
          </div>
        ) : (
          <Table
            columns={[
              {
                key: "tier",
                header: m["admin_search.column.backend"](),
                render: (tier) => (
                  <>
                    <div>{tier.name}</div>
                    <div className={`${MUTED} ${CODE}`}>{tier.provider_name}</div>
                  </>
                ),
              },
              {
                key: "grants",
                header: m["admin_search.tier_column.grants"](),
                render: (tier) => (
                  <div className={CHIPS}>
                    {groupList.map((group) => {
                      const granted = tier.granted_to.includes(group.name);
                      return (
                        // A grant made visible: the checked chip carries the accent
                        // tint, through tokens rather than the literal blues this
                        // used (`blue-50`/`blue-700` plus a `dark:` block) — the
                        // `.dark` block in tokens.css is the whole dark-mode
                        // mechanism, and a second one here would diverge from it
                        // on the next palette change. Checked accent-on-subtle
                        // clears 4.5:1 in both themes (5.5:1 light, 5.8:1 dark),
                        // which the old pairing managed in light only.
                        <label
                          key={group.id}
                          className="flex cursor-pointer items-center gap-1.5 rounded-full border border-line px-2.5 py-1 text-xs font-medium transition-colors has-[:checked]:border-accent/25 has-[:checked]:bg-accent-subtle has-[:checked]:text-accent"
                        >
                          <input
                            type="checkbox"
                            className="size-3"
                            checked={granted}
                            disabled={access.isPending}
                            onChange={(event) =>
                              toggleGrant(group.id, tier.id, event.target.checked)
                            }

                          />
                          {group.name}
                        </label>
                      );
                    })}
                    {groupList.length === 0 && (
                      <span className={MUTED}>{m["admin_search.no_groups_yet"]()}</span>
                    )}
                  </div>
                ),
              },
            ]}
            rows={tiers}
            rowKey={(row) => row.id}
            empty={m["admin_search.tiers_empty"]()}
          />
        )}
      </Card>

      {/* -- one backend per group ------------------------------------- */}
      <Card flush>
        <div className="p-4">
          <h2 className="text-sm font-semibold">{m["admin_search.policy_heading"]()}</h2>
          <p className={`${MUTED} mt-1 text-sm`}>
            <Message
              message={m["admin_search.policy_body"]}
              markup={{ code: (children) => <code>{children}</code> }}
            />
          </p>
        </div>
        {setPolicy.error ? (
          <div className="px-4 pb-2">
            <Notice tone="danger" title={m["admin_search.policy_error.title"]()}>
              {/* i18n: server text — the API's own error message. */}
              {setPolicy.error instanceof Error
                ? setPolicy.error.message
                : m["admin_search.unknown_error"]()}
            </Notice>
          </div>
        ) : null}
        {groups.isPending || models.isPending ? (
          <div className="p-6">
            <Spinner label={m["admin_search.loading_groups"]()} />
          </div>
        ) : (
          <Table
            columns={[
              {
                key: "group",
                header: m["admin_search.group_column.group"](),
                render: (group) => <div>{group.name}</div>,
              },
              {
                key: "backend",
                header: m["admin_search.group_column.backend"](),
                render: (group) => {
                  const granted = tiers.filter((tier) => tier.granted_to.includes(group.name));
                  const current = granted.find((tier) => tier.name === group.search_backend);
                  return (
                    <Select
                      label={m["admin_search.backend_for_group"]({ group: group.name })}
                      value={current?.id ?? ""}
                      onChange={(event) =>
                        setPolicy.mutate({
                          groupId: group.id,
                          modelId: event.target.value === "" ? null : event.target.value,
                        })
                      }
                    >
                      <option value="">{m["admin_search.backend_unset"]()}</option>
                      {granted.map((tier) => (
                        <option key={tier.id} value={tier.id}>
                          {tier.name}
                        </option>
                      ))}
                    </Select>
                  );
                },
              },
            ]}
            rows={groupList}
            rowKey={(row) => row.id}
            empty={m["admin_search.groups_empty"]()}
          />
        )}
      </Card>

      <BackendDialog
        open={creating || editing !== null}
        provider={editing}
        plugins={searchPlugins}
        onClose={() => {
          setCreating(false);
          setEditing(null);
        }}
      />
      <DeleteBackendDialog
        backend={deleting}
        tierCount={deleting ? tiers.filter((tier) => tier.provider_id === deleting.id).length : 0}
        onClose={() => setDeleting(null)}
      />
    </div>
  );
}

/**
 * A backend is two decisions: which vendor, and the key. The auth header
 * scheme and the request path are the plugin's knowledge — the gateway applies
 * them and the operator never types a URL (ADR 0071). The endpoint is the one
 * partial exception: where a vendor documents more than one host, the plugin
 * names them and the dialog offers that choice as a select, because free-text
 * URL entry for a decision with two documented values is a typo waiting to
 * happen. This is deliberately not the inference-provider dialogue: web search
 * providers are not model providers, and half those fields mean nothing here.
 */
function BackendDialog({
  open,
  provider,
  plugins,
  onClose,
}: {
  open: boolean;
  provider: AdminProvider | null;
  plugins: ProviderPlugin[];
  onClose: () => void;
}) {
  const create = useCreateProvider();
  const update = useUpdateProvider();
  const toast = useOptionalToast();
  const editing = provider !== null;

  const [plugin, setPlugin] = useState("");
  const [baseUrl, setBaseUrl] = useState("");
  const [apiKey, setApiKey] = useState("");
  const [clearKey, setClearKey] = useState(false);
  const [loadedFor, setLoadedFor] = useState<string | null>(null);

  if (open && loadedFor !== (provider?.id ?? "new")) {
    setLoadedFor(provider?.id ?? "new");
    setPlugin(provider?.plugin ?? "");
    setBaseUrl(provider?.base_url ?? "");
    setApiKey("");
    setClearKey(false);
  }

  const pending = create.isPending || update.isPending;
  const error = create.error ?? update.error;
  // One backend per vendor: the vendor's name is the handle everything else
  // derives from — the grant anchor's name, the passthrough's path segment.
  const chosen = plugin || "";
  const chosenPlugin = plugins.find((entry) => entry.name === chosen);
  // Keyless means no key UI, not an optional one (DuckDuckGo): the field is
  // not rendered, no validation mentions a key, and the create succeeds with
  // no credential. Demanding a key would force a dummy one, which then looks
  // like a secret worth rotating; offering it as optional invites exactly
  // that. True (the field shows) for every vendor that does not say
  // otherwise — an installed plugin that says nothing needs a key.
  const needsKey = chosenPlugin?.requires_api_key ?? true;

  // The vendor's documented hosts, when there is more than one. A stored URL
  // the plugin does not document — an operator's own proxy, or a value set
  // before the plugin listed its hosts — is offered alongside, preselected:
  // an unrelated save must not silently move the host, and it cannot unless
  // keeping the current one is itself a choice on the screen.
  const documented = chosenPlugin?.base_url_options ?? [];
  const endpointOptions =
    documented.length > 1
      ? editing && provider && !documented.some((entry) => entry.url === provider.base_url)
        ? [{ url: provider.base_url, label: provider.base_url }, ...documented]
        : documented
      : null;
  // A host switch is a change even when no key was typed: the Save guard used
  // to refuse every edit that touched nothing, which was right when the key
  // was the only thing here and would have made the endpoint choice
  // unsaveable on its own.
  const hostChanged =
    endpointOptions !== null && editing && provider !== null && baseUrl !== provider.base_url;

  const submit = () => {
    if (editing && provider) {
      update.mutate(
        {
          id: provider.id,
          // Where the vendor documents a choice, what the select shows is
          // what is sent; elsewhere the URL is omitted and the patch leaves
          // the stored one untouched.
          ...(endpointOptions && baseUrl ? { base_url: baseUrl } : {}),
          // Three ways, deliberately: a typed key replaces, the explicit clear
          // removes, and neither leaves the stored credential untouched.
          ...(apiKey ? { api_key: apiKey } : clearKey ? { api_key: "" } : {}),
        },
        {
          onSuccess: () => {
            toast?.add({ title: m["admin_search.toast.updated"](), type: "success" });
            onClose();
          },
          onError: () =>
            toast?.add({ title: m["admin_search.toast.update_failed"](), type: "error" }),
        },
      );
    } else {
      create.mutate(
        {
          name: chosen,
          plugin: chosen,
          kind: "search",
          // Sent only where the vendor documents a choice: a single-host
          // backend's plugin default is the whole answer, and sending
          // nothing is what applies it.
          ...(endpointOptions && baseUrl ? { base_url: baseUrl } : {}),
          ...(apiKey ? { api_key: apiKey } : {}),
        },
        {
          onSuccess: () => {
            toast?.add({ title: m["admin_search.toast.added"](), type: "success" });
            onClose();
          },
          onError: () => toast?.add({ title: m["admin_search.toast.add_failed"](), type: "error" }),
        },
      );
    }
  };

  return (
    <Dialog
      open={open}
      title={
        editing
          ? m["admin_search.dialog.edit_title"]({ provider: provider?.name ?? "" })
          : m["admin_search.dialog.add_title"]()
      }
      onClose={onClose}
      footer={
        <>
          <Button onClick={onClose}>{m["admin_search.dialog.cancel"]()}</Button>
          <Button
            variant="primary"
            busy={pending}
            disabled={
              !chosen
              || (needsKey && !editing && !apiKey.trim())
              || (needsKey && editing && !apiKey.trim() && !clearKey && !hostChanged)
            }
            onClick={submit}
          >
            {editing ? m["admin_search.dialog.save"]() : m["admin_search.dialog.add"]()}
          </Button>
        </>
      }
    >
      {error ? (
        <Notice tone="danger">
          {/* i18n: server text — the API's own error message. */}
          {error instanceof Error ? error.message : m["admin_search.unknown_error"]()}
        </Notice>
      ) : null}

      <Select
        label={m["admin_search.field.vendor"]()}
        value={plugin}
        disabled={editing}
        onChange={(event) => {
          // Choosing a vendor is choosing its endpoint, when it documents
          // more than one: pre-fill the host the plugin names as default. A
          // single-host vendor leaves the field empty — its default is the
          // whole answer — and a URL chosen for a *previous* vendor must
          // never ride along to this one.
          const next = plugins.find((entry) => entry.name === event.target.value);
          const hosts = next?.base_url_options ?? [];
          if (hosts.length > 1) {
            const fallback = hosts.find((entry) => entry.url === next?.default_base_url);
            setBaseUrl(fallback?.url ?? hosts[0]?.url ?? "");
          } else {
            setBaseUrl("");
          }
          // A key typed for the previous vendor never rides to the next one —
          // and for a keyless vendor there is nothing on the screen to carry.
          setApiKey("");
          setClearKey(false);
          setPlugin(event.target.value);
        }}
        hint={m["admin_search.field.vendor_hint"]()}
      >
        <option value="">{m["admin_search.field.vendor_choose"]()}</option>
        {plugins.map((entry) => (
          <option key={entry.name} value={entry.name}>
            {entry.label}
          </option>
        ))}
      </Select>

      {endpointOptions && (
        <Select
          label={m["admin_search.field.endpoint"]()}
          value={baseUrl}
          onChange={(event) => setBaseUrl(event.target.value)}
          hint={m["admin_search.field.endpoint_hint"]()}
        >
          {endpointOptions.map((entry) => (
            <option key={entry.url} value={entry.url}>
              {entry.label}
            </option>
          ))}
        </Select>
      )}

      {/* Rendered only for vendors that take a credential. For a keyless
          vendor the option is absent from the screen entirely — no field, no
          placeholder, no validation — because a credential here would be
          theatre: the gateway sends nothing to a vendor that never asked. */}
      {needsKey && (
        <Input
          label={m["admin_search.field.api_key"]()}
          type="password"
          value={apiKey}
          onChange={(event) => setApiKey(event.target.value)}
          placeholder={
            editing
              ? provider?.api_key_hint || m["admin_search.field.api_key_placeholder_unset"]()
              : m["admin_search.field.api_key_placeholder"]()
          }
          hint={
            editing
              ? m["admin_search.field.api_key_hint_edit"]()
              : m["admin_search.field.api_key_hint_new"]()
          }
        />
      )}

      {needsKey && editing && (
        <label className="flex cursor-pointer items-baseline gap-2">
          <input
            type="checkbox"
            checked={clearKey}
            onChange={(event) => {
              setClearKey(event.target.checked);
              if (event.target.checked) setApiKey("");
            }}
          />
          <span>{m["admin_search.field.remove_key"]()}</span>
        </label>
      )}
    </Dialog>
  );
}

/**
 * Deleting a backend, with the consequences said in place rather than left for
 * the operator to meet later. The gateway cascades here on purpose (ADR 0071)
 * — a backend and its tiers are one concept, and a half-deleted backend is
 * worse than an atomic delete — so the dialog says what goes with it: the
 * tiers, the grants, and the groups' unified-search policy. What it also says,
 * because "delete" beside money has to answer it before the click: the ledger
 * is untouched, the same guarantee the model delete makes.
 */
function DeleteBackendDialog({
  backend,
  tierCount,
  onClose,
}: {
  backend: AdminProvider | null;
  tierCount: number;
  onClose: () => void;
}) {
  const del = useDeleteSearchBackend();
  const toast = useOptionalToast();

  return (
    <Dialog
      open={backend !== null}
      title={m["admin_search.delete.title"]({
        backend: backend?.name ?? m["admin_search.delete.title_fallback"](),
      })}
      onClose={onClose}
      footer={
        <>
          <Button onClick={onClose}>{m["admin_search.dialog.cancel"]()}</Button>
          <Button
            variant="danger"
            busy={del.isPending}
            onClick={() =>
              backend &&
              del.mutate(backend.id, {
                onSuccess: (result) => {
                  // The gateway names the groups whose unified-search policy
                  // the cascade cleared; the toast repeats them, so the
                  // administrator hears who was affected without going to
                  // look — and hears nothing extra when nobody was.
                  const who = result.cleared_groups.map((name) => `'${name}'`).join(", ");
                  toast?.add({
                    title:
                      who.length > 0
                        ? m["admin_search.delete.toast_with_groups"]({
                            count: result.cleared_groups.length,
                            groups: who,
                          })
                        : m["admin_search.delete.toast_plain"](),
                    type: "success",
                  });
                  onClose();
                },
                onError: () =>
                  toast?.add({ title: m["admin_search.delete.toast_failed"](), type: "error" }),
              })
            }
          >
            {m["admin_search.delete.confirm"]()}
          </Button>
        </>
      }
    >
      <div className={FORM}>
        {del.error ? (
          <Notice tone="danger">
            {/* i18n: server text — the API's own error message. */}
            {del.error instanceof Error ? del.error.message : m["admin_search.unknown_error"]()}
          </Notice>
        ) : null}
        <p>
          <Message
            message={m["admin_search.delete.body"]}
            inputs={{ count: tierCount }}
            markup={{ code: (children) => <code className={CODE}>{children}</code> }}
          />
        </p>
        <p className={MUTED}>{m["admin_search.delete.spend_note"]()}</p>
      </div>
    </Dialog>
  );
}
