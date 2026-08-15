import { Badge, Button, Card, Dialog, Input, Notice, Spinner, Table } from "@llmp/ui";
import type { Column } from "@llmp/ui";
import { formatMoney } from "@llmp/ui";
import { useState } from "react";
import {
  useCreateModel,
  useDiscovery,
  useGroups,
  useImportModels,
  useModelAccess,
  useModels,
  useUpdateModel,
} from "../lib/admin";
import type { AdminModel, DiscoveredModel } from "../lib/types";
import { PageHeader } from "../components/PageHeader";
import styles from "./Admin.module.css";

export function AdminModels() {
  const models = useModels();
  const update = useUpdateModel();

  const [creating, setCreating] = useState(false);
  const [access, setAccess] = useState<AdminModel | null>(null);
  const [discovering, setDiscovering] = useState(false);

  const columns: Column<AdminModel>[] = [
    {
      key: "name",
      header: "Model",
      render: (model) => (
        <>
          <div>{model.name}</div>
          <div className={`${styles.muted} ${styles.code}`}>{model.upstream_model}</div>
        </>
      ),
    },
    {
      key: "price",
      header: "Price / Mtok",
      numeric: true,
      render: (model) =>
        model.current_price ? (
          <>
            <div>{formatMoney(model.current_price.input_per_mtok, model.current_price.currency)} in</div>
            <div className={styles.muted}>
              {formatMoney(model.current_price.output_per_mtok, model.current_price.currency)} out
            </div>
          </>
        ) : (
          // An unpriced model serves happily and records a cost of zero, which
          // is a quiet way to give away money. Worth flagging in the list.
          <Badge tone="warn">No price</Badge>
        ),
    },
    {
      key: "access",
      header: "Groups",
      render: (model) =>
        model.granted_to.length === 0 ? (
          <span className={styles.muted}>nobody</span>
        ) : (
          <div className={styles.chips}>
            {model.granted_to.map((group) => (
              <Badge key={group}>{group}</Badge>
            ))}
          </div>
        ),
    },
    {
      key: "status",
      header: "Status",
      render: (model) =>
        model.is_active ? <Badge tone="ok">Active</Badge> : <Badge>Inactive</Badge>,
    },
    {
      key: "actions",
      header: "",
      render: (model) => (
        <div className={styles.rowActions}>
          <Button onClick={() => setAccess(model)}>Access</Button>
          <Button
            busy={update.isPending && update.variables?.id === model.id}
            onClick={() => update.mutate({ id: model.id, is_active: !model.is_active })}
          >
            {model.is_active ? "Deactivate" : "Activate"}
          </Button>
        </div>
      ),
    },
  ];

  return (
    <div className={styles.page}>
      <PageHeader
        title="Models"
        subtitle="The catalogue is an allowlist. A model is invisible to callers until a group is
          granted it, and models are deactivated rather than deleted so historical spend stays
          attributable."
        actions={
          <>
            <Button onClick={() => setDiscovering(true)}>Discover</Button>
            <Button variant="primary" onClick={() => setCreating(true)}>
              Add model
            </Button>
          </>
        }
      />

      {update.error ? (
        <Notice tone="danger" title="Could not update the model">
          {update.error instanceof Error ? update.error.message : "Unknown error."}
        </Notice>
      ) : null}

      <Card flush>
        {models.isPending ? (
          <Spinner label="Loading the catalogue" />
        ) : models.error ? (
          <Notice tone="danger" title="Could not load the catalogue">
            {models.error instanceof Error ? models.error.message : "Unknown error."}
          </Notice>
        ) : (
          <Table
            columns={columns}
            rows={models.data ?? []}
            rowKey={(model) => model.id}
            empty="No models catalogued. Use Discover to see what the provider offers."
            caption="Catalogued models, their current price and who may use them."
          />
        )}
      </Card>

      <CreateModelDialog open={creating} onClose={() => setCreating(false)} />
      <AccessDialog model={access} onClose={() => setAccess(null)} />
      <DiscoveryDialog open={discovering} onClose={() => setDiscovering(false)} />
    </div>
  );
}

function CreateModelDialog({ open, onClose }: { open: boolean; onClose: () => void }) {
  const create = useCreateModel();
  const [name, setName] = useState("");
  const [upstream, setUpstream] = useState("");

  return (
    <Dialog
      open={open}
      title="Add a model"
      onClose={onClose}
      footer={
        <>
          <Button onClick={onClose}>Cancel</Button>
          <Button
            variant="primary"
            busy={create.isPending}
            disabled={!name.trim() || !upstream.trim()}
            onClick={() =>
              create.mutate(
                { name: name.trim(), upstream_model: upstream.trim() },
                { onSuccess: () => { setName(""); setUpstream(""); onClose(); } },
              )
            }
          >
            Add
          </Button>
        </>
      }
    >
      {create.error ? (
        <Notice tone="danger">
          {create.error instanceof Error ? create.error.message : "Unknown error."}
        </Notice>
      ) : null}

      <Input
        label="Name"
        value={name}
        onChange={(e) => setName(e.target.value)}
        placeholder="fast-summariser"
        hint="What callers send as `model`. Cannot be changed later — usage rows record it."
      />
      <Input
        label="Upstream model"
        value={upstream}
        onChange={(e) => setUpstream(e.target.value)}
        placeholder="provider/model-id"
        hint="What the gateway asks the provider for."
      />

      <Notice tone="info">
        No group is granted access, and no price is set. Both are deliberate: a new
        model is invisible until someone chooses to expose it, and an unpriced model
        would record a cost of zero.
      </Notice>
    </Dialog>
  );
}

/** Grant and revoke a model per group. Each toggle is its own request. */
function AccessDialog({ model, onClose }: { model: AdminModel | null; onClose: () => void }) {
  const groups = useGroups();
  const access = useModelAccess();

  return (
    <Dialog open={model !== null} title={`Access · ${model?.name ?? ""}`} onClose={onClose}>
      {access.error ? (
        <Notice tone="danger">
          {access.error instanceof Error ? access.error.message : "Unknown error."}
        </Notice>
      ) : null}

      {groups.isPending ? (
        <Spinner />
      ) : (
        <div className={styles.checkList}>
          {(groups.data ?? []).map((group) => {
            const granted = model ? group.models.includes(model.name) : false;
            return (
              <label key={group.id} className={styles.checkItem}>
                <input
                  type="checkbox"
                  checked={granted}
                  disabled={access.isPending}
                  onChange={() =>
                    model &&
                    access.mutate({ groupId: group.id, modelId: model.id, grant: !granted })
                  }
                />
                <span>{group.name}</span>
              </label>
            );
          })}
        </div>
      )}

      <Notice tone="info">
        Absence of a grant means no access — there is no global allow-all. Revoking
        takes effect on the next request.
      </Notice>
    </Dialog>
  );
}

/**
 * What the provider offers against what we carry.
 *
 * Fetched on open rather than on mount: it calls out to the provider, and a page
 * that hangs on load because a third party is slow is worse than one with a
 * button. Nothing is adopted without an explicit choice — auto-importing would
 * let a provider's release notes change what users can spend money on.
 */
function DiscoveryDialog({ open, onClose }: { open: boolean; onClose: () => void }) {
  const discovery = useDiscovery(open);
  const importModels = useImportModels();
  const [selected, setSelected] = useState<string[]>([]);

  const toggle = (id: string) =>
    setSelected((current) =>
      current.includes(id) ? current.filter((entry) => entry !== id) : [...current, id],
    );

  const columns: Column<DiscoveredModel>[] = [
    {
      key: "pick",
      header: "",
      render: (row) => (
        <input
          type="checkbox"
          checked={selected.includes(row.upstream_model)}
          disabled={row.blocked_reason !== null}
          aria-label={`Import ${row.suggested_name}`}
          onChange={() => toggle(row.upstream_model)}
        />
      ),
    },
    {
      key: "model",
      header: "Offered upstream",
      render: (row) => (
        <>
          <div>{row.suggested_name}</div>
          <div className={`${styles.muted} ${styles.code}`}>{row.upstream_model}</div>
          {row.blocked_reason && (
            <div className={styles.muted}>
              <Badge tone="warn">Cannot import</Badge> {row.blocked_reason}
            </div>
          )}
        </>
      ),
    },
    {
      key: "price",
      header: "Price / Mtok",
      numeric: true,
      render: (row) =>
        row.input_per_mtok && row.currency
          ? `${formatMoney(row.input_per_mtok, row.currency)} / ${formatMoney(
              row.output_per_mtok ?? "0",
              row.currency,
            )}`
          : "—",
    },
  ];

  return (
    <Dialog
      open={open}
      title="Provider catalogue"
      onClose={onClose}
      footer={
        <>
          <Button onClick={onClose}>Close</Button>
          <Button
            variant="primary"
            busy={importModels.isPending}
            disabled={selected.length === 0}
            onClick={() =>
              importModels.mutate(selected, { onSuccess: () => setSelected([]) })
            }
          >
            Import {selected.length || ""}
          </Button>
        </>
      }
    >
      {discovery.isPending && <Spinner label="Asking the provider" />}
      {discovery.error ? (
        <Notice tone="danger" title="Could not read the provider catalogue">
          {discovery.error instanceof Error ? discovery.error.message : "Unknown error."}
        </Notice>
      ) : null}

      {importModels.data && (
        <Notice tone="info" title="Import finished">
          {importModels.data.results.map((result) => (
            <div key={result.upstream_model}>
              {result.imported
                ? `${result.name} imported${result.priced ? " with its price" : ""}`
                : `${result.upstream_model} skipped — ${result.reason}`}
            </div>
          ))}
        </Notice>
      )}

      {discovery.data && (
        <>
          {/* The dangerous direction of drift: still served, no longer offered.
              These fail only when someone calls them. */}
          {discovery.data.missing_upstream.length > 0 && (
            <Notice tone="warn" title="Served here, no longer offered upstream">
              {discovery.data.missing_upstream.map((row) => (
                <div key={row.name}>
                  {row.name} ({row.upstream_model})
                </div>
              ))}
            </Notice>
          )}

          <Table
            columns={columns}
            rows={discovery.data.available}
            rowKey={(row) => row.upstream_model}
            empty="The provider offers nothing that is not already catalogued."
            caption={`${discovery.data.provider_model_count} models offered by the provider.`}
          />
        </>
      )}
    </Dialog>
  );
}
