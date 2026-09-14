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
import { CHIPS, CODE, MUTED, PAGE, ROW_ACTIONS } from "../lib/layout";

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
          <Button onClick={() => setEditing(backend)}>Edit</Button>
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
    </div>
  );
}

/**
 * A backend is two decisions: which vendor, and the key. The endpoint, the
 * auth header scheme and the request path are the plugin's knowledge — the
 * gateway applies them and the operator never types a URL (ADR 0071). This is
 * deliberately not the inference-provider dialogue: web search providers are
 * not model providers, and half those fields mean nothing here.
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
  const [apiKey, setApiKey] = useState("");
  const [clearKey, setClearKey] = useState(false);
  const [loadedFor, setLoadedFor] = useState<string | null>(null);

  if (open && loadedFor !== (provider?.id ?? "new")) {
    setLoadedFor(provider?.id ?? "new");
    setPlugin(provider?.plugin ?? "");
    setApiKey("");
    setClearKey(false);
  }

  const pending = create.isPending || update.isPending;
  const error = create.error ?? update.error;
  // One backend per vendor: the vendor's name is the handle everything else
  // derives from — the grant anchor's name, the passthrough's path segment.
  const chosen = plugin || "";

  const submit = () => {
    if (editing && provider) {
      update.mutate(
        {
          id: provider.id,
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
            disabled={!chosen || (!editing && !apiKey.trim()) || (editing && !apiKey.trim() && !clearKey)}
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
        onChange={(event) => setPlugin(event.target.value)}
        hint="The endpoint and the credential scheme are the vendor's own — configured here, not typed anywhere."
      >
        <option value="">Choose a vendor…</option>
        {plugins.map((entry) => (
          <option key={entry.name} value={entry.name}>
            {entry.label}
          </option>
        ))}
      </Select>

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
