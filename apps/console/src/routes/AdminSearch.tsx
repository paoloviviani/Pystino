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
import { useOptionalToast } from "../lib/toast";
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
        A search backend answers <code>POST /v1/search</code>, and a tier — Linkup's{" "}
        <code>depth</code>, Exa's <code>type</code> — is what a caller names there. Tiers are
        metered as a count of requests, never priced, and are absent from <code>/v1/models</code>{" "}
        unless a client asks for them explicitly, so no other client will mistake one for a chat
        model (ADR 0071). What a caller may run is the grant below; how much they may run is a
        request ceiling on Quotas.
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

      {/* -- tiers ---------------------------------------------------- */}
      <Card flush>
        <div className="flex items-center justify-between p-4">
          <h2 className="text-sm font-semibold">Tiers and grants</h2>
          <TierImporter backends={backends} />
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
                header: "Tier",
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
            empty="No tiers yet. Add a backend, then import its tiers."
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

/** One click: ask the backend what tiers it offers, and adopt them all. */
function TierImporter({ backends }: { backends: AdminProvider[] }) {
  const [busyId, setBusyId] = useState<string | null>(null);
  const toast = useOptionalToast();

  async function importTiers(backend: AdminProvider) {
    setBusyId(backend.id);
    try {
      // A backend's tiers are a fixed enum its plugin already knows — built
      // in, never fetched — so there is nothing for the operator to curate
      // here: discover, then adopt every offered tier. The decision a search
      // screen exists to make is which groups may run which, not which tiers
      // exist.
      const discovery = await fetch(
        `/api/admin/models/discover?provider_id=${encodeURIComponent(backend.id)}`,
      ).then((r) => {
        if (!r.ok) throw new Error("The backend did not answer.");
        return r.json() as Promise<{
          available: { upstream_model: string; blocked_reason: string | null }[];
        }>;
      });
      const offered = discovery.available
        .filter((row) => row.blocked_reason === null)
        .map((row) => ({ upstream_model: row.upstream_model }));
      if (offered.length === 0) {
        toast?.add({ title: "Every tier is already imported", type: "info" });
        return;
      }
      await fetch(`/api/admin/models/import?provider_id=${encodeURIComponent(backend.id)}`, {
        method: "POST",
        headers: { "content-type": "application/json" },
        body: JSON.stringify({ models: offered }),
      }).then((r) => {
        if (!r.ok) throw new Error("The import was refused.");
      });
      toast?.add({ title: "Tiers imported", type: "success" });
    } catch (err) {
      toast?.add({
        title: err instanceof Error ? err.message : "Could not import the tiers",
        type: "error",
      });
    } finally {
      setBusyId(null);
    }
  }

  if (backends.length === 0) {
    return <Button disabled>Add a backend first</Button>;
  }

  return (
    <div className="flex items-center gap-2">
      {backends.map((backend) => (
        <Button
          key={backend.id}
          busy={busyId === backend.id}
          disabled={busyId !== null}
          onClick={() => importTiers(backend)}
        >
          Import tiers — {backend.name}
        </Button>
      ))}
    </div>
  );
}
