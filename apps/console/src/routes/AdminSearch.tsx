import {
  Badge,
  Button,
  Card,
  Dialog,
  Input,
  Notice,
  Select,
  Spinner,
  Table,
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
      header: "Backend",
      render: (backend) => (
        <>
          <div>{backend.name}</div>
          <div className={`${MUTED} ${CODE}`}>{backend.base_url}</div>
        </>
      ),
    },
    {
      key: "credential",
      header: "API key",
      render: (backend) =>
        backend.has_api_key ? (
          <span className={CODE}>{backend.api_key_hint}</span>
        ) : (
          <span className={MUTED}>none — set one before searching</span>
        ),
    },
    {
      key: "tiers",
      header: "Tiers",
      numeric: true,
      render: (backend) =>
        tiers.filter((tier) => tier.provider_id === backend.id).length.toLocaleString(),
    },
    {
      key: "status",
      header: "Status",
      render: (backend) => (
        <div className={CHIPS}>
          {backend.is_active ? <Badge tone="ok">Active</Badge> : <Badge>Inactive</Badge>}
        </div>
      ),
    },
    {
      key: "actions",
      header: "",
      render: (backend) => (
        <div className={ROW_ACTIONS}>
          {/* Explicit, not inherited: the row's actions read as one system
              across the console — Edit is always the light outline, never a
              colour that could be mistaken for the primary action of the row. */}
          <Button variant="secondary" onClick={() => setEditing(backend)}>
            Edit
          </Button>
          {/* Thin red text, never filled: at row level a filled red button
              outweighs every other element in the table, and deletion here is
              confirmable, not accidental — the confirm dialog is where the
              filled danger belongs. */}
          <Button variant="ghost" className="text-danger" onClick={() => setDeleting(backend)}>
            Delete
          </Button>
        </div>
      ),
    },
  ];

  return (
    <div className={PAGE}>
      <PageHeader title="Web search" />

      <Notice tone="info">
        A search backend answers <code>POST /v1/search/&#123;backend&#125;</code> — an
        authenticated, metering passthrough: the gateway counts the request, attaches the
        backend's own credential, and forwards the caller's body to the vendor verbatim, returning
        the vendor's answer verbatim (ADR 0071). Backends are absent from{" "}
        <code>/v1/models</code> unless a client asks for them explicitly, so no other client will
        mistake one for a chat model. Who may search is the grant below; how much they may search
        is a request ceiling on Quotas.
      </Notice>

      {/* -- backends ------------------------------------------------ */}
      <Card flush>
        <div className="flex items-center justify-between p-4">
          <h2 className="text-sm font-semibold">Backends</h2>
          <Button onClick={() => setCreating(true)}>Add backend</Button>
        </div>
        {providers.isPending ? (
          <div className="p-6">
            <Spinner label="Loading backends" />
          </div>
        ) : (
          <Table columns={backendColumns} rows={backends} rowKey={(row) => row.id} />
        )}
      </Card>

      {/* -- grants ---------------------------------------------------- */}
      <Card flush>
        <div className="p-4">
          <h2 className="text-sm font-semibold">Who may search</h2>
          <p className={`${MUTED} mt-1 text-sm`}>
            One grant per backend — creating the backend made its row. What the caller sends
            inside the body (a depth, a result count) is the vendor's own pricing, not a decision
            this screen makes.
          </p>
        </div>
        {models.isPending ? (
          <div className="p-6">
            <Spinner label="Loading tiers" />
          </div>
        ) : (
          <Table
            columns={[
              {
                key: "tier",
                header: "Backend",
                render: (tier) => (
                  <>
                    <div>{tier.name}</div>
                    <div className={`${MUTED} ${CODE}`}>{tier.provider_name}</div>
                  </>
                ),
              },
              {
                key: "grants",
                header: "Groups that may run it",
                render: (tier) => (
                  <div className={CHIPS}>
                    {groupList.map((group) => {
                      const granted = tier.granted_to.includes(group.name);
                      return (
                        <label
                          key={group.id}
                          className="flex cursor-pointer items-center gap-1.5 rounded-full border px-2.5 py-1 text-xs font-medium transition-colors has-[:checked]:border-blue-600/30 has-[:checked]:bg-blue-50 has-[:checked]:text-blue-700 dark:has-[:checked]:border-blue-700/60 dark:has-[:checked]:bg-blue-900/30 dark:has-[:checked]:text-blue-300"
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
                    {groupList.length === 0 && <span className={MUTED}>no groups yet</span>}
                  </div>
                ),
              },
            ]}
            rows={tiers}
            rowKey={(row) => row.id}
            empty="No backends yet. Add one above."
          />
        )}
      </Card>

      {/* -- one backend per group ------------------------------------- */}
      <Card flush>
        <div className="p-4">
          <h2 className="text-sm font-semibold">Which backend each group searches through</h2>
          <p className={`${MUTED} mt-1 text-sm`}>
            The policy behind <code>POST /v1/search</code> (no backend in the path): one
            request shape in, one answer shape out, through exactly this backend at its
            default depth. Only backends the group is granted above are offered — a group
            with none set cannot use the unified route at all.
          </p>
        </div>
        {setPolicy.error ? (
          <div className="px-4 pb-2">
            <Notice tone="danger" title="Could not set the search backend">
              {setPolicy.error instanceof Error ? setPolicy.error.message : "Unknown error."}
            </Notice>
          </div>
        ) : null}
        {groups.isPending || models.isPending ? (
          <div className="p-6">
            <Spinner label="Loading groups" />
          </div>
        ) : (
          <Table
            columns={[
              {
                key: "group",
                header: "Group",
                render: (group) => <div>{group.name}</div>,
              },
              {
                key: "backend",
                header: "Searches through",
                render: (group) => {
                  const granted = tiers.filter((tier) => tier.granted_to.includes(group.name));
                  const current = granted.find((tier) => tier.name === group.search_backend);
                  return (
                    <Select
                      label={`Search backend for ${group.name}`}
                      value={current?.id ?? ""}
                      onChange={(event) =>
                        setPolicy.mutate({
                          groupId: group.id,
                          modelId: event.target.value === "" ? null : event.target.value,
                        })
                      }
                    >
                      <option value="">Unset — no unified search</option>
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
            empty="No groups yet."
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
            toast?.add({ title: "Backend updated", type: "success" });
            onClose();
          },
          onError: () => toast?.add({ title: "Could not update the backend", type: "error" }),
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
            toast?.add({ title: "Backend added", type: "success" });
            onClose();
          },
          onError: () => toast?.add({ title: "Could not add the backend", type: "error" }),
        },
      );
    }
  };

  return (
    <Dialog
      open={open}
      title={editing ? `Edit ${provider?.name}` : "Add a search backend"}
      onClose={onClose}
      footer={
        <>
          <Button onClick={onClose}>Cancel</Button>
          <Button
            variant="primary"
            busy={pending}
            disabled={
              !chosen
              || (!editing && !apiKey.trim())
              || (editing && !apiKey.trim() && !clearKey && !hostChanged)
            }
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

      <Select
        label="Vendor"
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
          setPlugin(event.target.value);
        }}
        hint="The endpoint and the credential scheme are the vendor's own — configured here, not typed anywhere."
      >
        <option value="">Choose a vendor…</option>
        {plugins.map((entry) => (
          <option key={entry.name} value={entry.name}>
            {entry.label}
          </option>
        ))}
      </Select>

      {endpointOptions && (
        <Select
          label="Endpoint"
          value={baseUrl}
          onChange={(event) => setBaseUrl(event.target.value)}
          hint="Which of the vendor's documented hosts the searches go to."
        >
          {endpointOptions.map((entry) => (
            <option key={entry.url} value={entry.url}>
              {entry.label}
            </option>
          ))}
        </Select>
      )}

      <Input
        label="API key"
        type="password"
        value={apiKey}
        onChange={(event) => setApiKey(event.target.value)}
        placeholder={editing ? provider?.api_key_hint || "unset" : "paste the vendor's key"}
        hint={
          editing
            ? "Leave empty to keep the stored key; tick to remove it."
            : "Stored encrypted; shown back only as a hint."
        }
      />

      {editing && (
        <label className="flex cursor-pointer items-baseline gap-2">
          <input
            type="checkbox"
            checked={clearKey}
            onChange={(event) => {
              setClearKey(event.target.checked);
              if (event.target.checked) setApiKey("");
            }}
          />
          <span>Remove the stored key</span>
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
      title={`Delete ${backend?.name ?? "this backend"}`}
      onClose={onClose}
      footer={
        <>
          <Button onClick={onClose}>Cancel</Button>
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
                        ? `Backend deleted — ${who} lost ${
                            result.cleared_groups.length === 1 ? "its" : "their"
                          } unified-search policy`
                        : "Backend deleted",
                    type: "success",
                  });
                  onClose();
                },
                onError: () =>
                  toast?.add({ title: "Could not delete the backend", type: "error" }),
              })
            }
          >
            Delete permanently
          </Button>
        </>
      }
    >
      <div className={FORM}>
        {del.error ? (
          <Notice tone="danger">
            {del.error instanceof Error ? del.error.message : "Unknown error."}
          </Notice>
        ) : null}
        <p>
          Removes the backend and its {tierCount} {tierCount === 1 ? "tier" : "tiers"} from the
          gateway outright. Callers of <code className={CODE}>POST /v1/search/&#123;backend&#125;</code>{" "}
          get "unknown backend" from the next request on, and groups searching through it lose
          their unified-search policy. This cannot be undone.
        </p>
        <p className={MUTED}>
          Recorded spend is unaffected: past searches keep their attribution in the ledger.
        </p>
      </div>
    </Dialog>
  );
}
