import {
  Badge,
  Button,
  Card,
  Notice,
  Spinner,
  Table,
} from "@llmp/ui";
import type { Column } from "@llmp/ui";
import { useState } from "react";
import { useGroups, useModelAccess, useModels, useProviders } from "../lib/admin";
import type { AdminProvider } from "../lib/types";
import { PageHeader } from "../components/PageHeader";
import { CHIPS, CODE, MUTED, PAGE, ROW_ACTIONS } from "../lib/layout";
import { ProviderDialog } from "./AdminProviders";

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
  const models = useModels();
  const groups = useGroups();

  const [creating, setCreating] = useState(false);
  const [editing, setEditing] = useState<AdminProvider | null>(null);

  // The screen's own vocabulary: backends are the search providers, tiers the
  // models of kind `search`. Everything else in those listings belongs to the
  // providers and models screens.
  const backends = (providers.data?.items ?? []).filter((p) => p.kind === "search");
  const tiers = (models.data?.items ?? []).filter((m) => m.kind === "search");
  const groupList = groups.data?.items ?? [];

  const access = useModelAccess();

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

      <ProviderDialog
        open={creating || editing !== null}
        provider={editing}
        pluginFilter={(plugin) => plugin.kind === "search"}
        onClose={() => {
          setCreating(false);
          setEditing(null);
        }}
      />
    </div>
  );
}
