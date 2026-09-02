import {
  Badge,
  Button,
  Card,
  Dialog,
  Input,
  Notice,
  Pagination,
  Select,
  Spinner,
  Table,
  
} from "@llmp/ui";
import type { Column } from "@llmp/ui";
import { formatMoney, useExactMoney } from "@llmp/ui";
import { useState } from "react";
import { Link, useNavigate } from "react-router";
import {
  Capabilities,
  CapabilityPicker,
  KNOWN_FEATURES,
  KNOWN_INPUTS,
  KNOWN_OUTPUTS,
} from "../components/CapabilityPicker";
import { PageHeader } from "../components/PageHeader";
import {
  useCreateModel,
  useDeleteModel,
  useDiscovery,
  useImportModels,
  useModels,
  useProviders,
  useUpdateModel,
} from "../lib/admin";
import { CHIPS, CODE, MUTED, PAGE, ROW_ACTIONS } from "../lib/layout";
import { usePaginated } from "../lib/paging";
import type { AdminModel, DiscoveredModel, ModelKind } from "../lib/types";
import { useOptionalToast } from "../lib/toast";


export function AdminModels() {
  // A rate is money too, so it follows the reader's precision preference. Read
  // by hand rather than via <Money> because these figures sit inside a phrase
  // ("0.117 in / 0.251 out").
  const exact = useExactMoney();
  // A provider catalogue import can bring hundreds of models in one click, so
  // this is one of the two screens that genuinely needs the pager.
  const paged = usePaginated();
  const models = useModels(paged.page);
  const update = useUpdateModel();
  const deleteModel = useDeleteModel();
  const toast = useOptionalToast();

  const navigate = useNavigate();
  const [creating, setCreating] = useState(false);
  const [discovering, setDiscovering] = useState(false);
  const [deleting, setDeleting] = useState<AdminModel | null>(null);

  const columns: Column<AdminModel>[] = [
    {
      key: "name",
      header: "Model",
      render: (model) => (
        <>
          <div>
            {/* The name is the link, not only the button beside it: it is what
                someone points at, and a real anchor is what makes "open in a new
                tab" and "copy link" work at all. */}
            <Link to={`/admin/models/${model.id}`}>{model.name}</Link>{" "}
            {model.kind !== "chat" && <Badge tone="accent">{model.kind}</Badge>}
          </div>
          <div className={`${MUTED} ${CODE}`}>{model.upstream_model}</div>
        </>
      ),
    },
    {
      key: "capabilities",
      header: "Capabilities",
      render: (model) => <Capabilities model={model} />,
    },
    {
      key: "provider",
      header: "Provider",
      render: (model) => (
        <>
          <div>{model.provider_name}</div>
          {/* Deactivating a provider silently takes every model behind it out
              of service. The catalogue is where that has to be visible. */}
          {!model.provider_is_active && <Badge tone="danger">Provider off</Badge>}
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
            <div>
              {formatMoney(model.current_price.input_per_mtok, model.current_price.currency, {
                exact,
              })}{" "}
              in
            </div>
            <div className={MUTED}>
              {formatMoney(model.current_price.output_per_mtok, model.current_price.currency, {
                exact,
              })}{" "}
              out
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
      header: "Access",
      render: (model) =>
        model.granted_to.length === 0 && model.granted_to_users.length === 0 ? (
          <span className={MUTED}>nobody</span>
        ) : (
          <div className={CHIPS}>
            {model.granted_to.map((group) => (
              <Badge key={group}>{group}</Badge>
            ))}
            {/* Personal grants are unioned with group grants, so they are shown
                alongside rather than in a separate column. */}
            {model.granted_to_users.map((user) => (
              <Badge key={user} tone="accent">
                {user}
              </Badge>
            ))}
          </div>
        ),
    },
    {
      key: "status",
      header: "Status",
      render: (model) => (
        <div className={CHIPS}>
          {model.is_active ? <Badge tone="ok">Active</Badge> : <Badge>Inactive</Badge>}
          {model.is_public && <Badge tone="accent">Public</Badge>}
        </div>
      ),
    },
    {
      key: "actions",
      header: "",
      render: (model) => (
        <div className={ROW_ACTIONS}>
          {/* Everything that configures one model — capabilities, price, who may
              reach it — is on its page now. Access used to be a dialog here and
              pricing a whole separate screen, which meant three places to change
              one model and a price you had to go and look for. */}
          <Button onClick={() => navigate(`/admin/models/${model.id}`)}>Edit</Button>
          <Button
            busy={update.isPending && update.variables?.id === model.id}
            onClick={() =>
              update.mutate(
                { id: model.id, is_active: !model.is_active },
                {
                  onSuccess: () => toast?.add({ title: "Model updated", type: "success" }),
                  onError: () =>
                    toast?.add({ title: "Could not update the model", type: "error" }),
                },
              )
            }
          >
            {model.is_active ? "Deactivate" : "Activate"}
          </Button>
          <Button variant="ghost" onClick={() => setDeleting(model)}>
            Delete
          </Button>
        </div>
      ),
    },
  ];

  return (
    <div className={PAGE}>
      <PageHeader
        title="Models"
        subtitle="An allowlist: a model is invisible to callers until a group is granted it."
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

      {deleteModel.error ? (
        <Notice tone="danger" title="Could not delete the model">
          {deleteModel.error instanceof Error ? deleteModel.error.message : "Unknown error."}
        </Notice>
      ) : null}

      <Card>
        {/* The search row and its growing field are a one-off shape (a lone
            input that must shrink before the row wraps), so they are inline
            here rather than constants in lib/layout. */}
        <div className="flex flex-wrap items-end gap-3">
          <div className="min-w-56 flex-1">
            <Input
              label="Search"
              value={paged.search}
              onChange={(event) => paged.setSearch(event.target.value)}
              placeholder="model name or upstream id"
              hint={
                models.data
                  ? `${models.data.total.toLocaleString()} matching`
                  : "Matches name and upstream id."
              }
            />
          </div>
        </div>
      </Card>

      <Card flush>
        {models.isPending ? (
          <Spinner label="Loading the catalogue" />
        ) : models.error ? (
          <Notice tone="danger" title="Could not load the catalogue">
            {models.error instanceof Error ? models.error.message : "Unknown error."}
          </Notice>
        ) : (
          <>
            <Table
              columns={columns}
              rows={models.data?.items ?? []}
              rowKey={(model) => model.id}
              empty={
                paged.query
                  ? "No model matches that."
                  : "No models catalogued. Use Discover to see what the provider offers."
              }
              caption="Catalogued models, their current price and who may use them."
            />
            <Pagination
              total={models.data?.total ?? 0}
              limit={paged.limit}
              offset={paged.offset}
              onOffsetChange={paged.setOffset}
              noun="models"
              busy={models.isFetching}
            />
          </>
        )}
      </Card>

      <CreateModelDialog open={creating} onClose={() => setCreating(false)} />
      <DiscoveryDialog open={discovering} onClose={() => setDiscovering(false)} />
      <DeleteModelDialog model={deleting} onClose={() => setDeleting(null)} />
    </div>
  );
}

/**
 * Removing a model from the catalogue outright, as opposed to deactivating it.
 *
 * Deactivation takes it out of service but leaves the row, and a catalogue
 * pruned of a hundred stale imports is the case delete exists for. The ledger
 * does not suffer: past usage keeps the model's name and its user/group
 * attribution, which is what the dialog says, because "delete" beside money
 * has to answer "what happens to the records" before the click.
 */
function DeleteModelDialog({ model, onClose }: { model: AdminModel | null; onClose: () => void }) {
  const del = useDeleteModel();
  const toast = useOptionalToast();

  return (
    <Dialog
      open={model !== null}
      title={`Delete ${model?.name ?? "this model"}`}
      onClose={onClose}
      footer={
        <>
          <Button onClick={onClose}>Cancel</Button>
          <Button
            variant="danger"
            busy={del.isPending}
            onClick={() =>
              model &&
              del.mutate(model.id, {
                onSuccess: () => {
                  toast?.add({ title: "Model deleted", type: "success" });
                  onClose();
                },
                onError: () =>
                  toast?.add({ title: "Could not delete the model", type: "error" }),
              })
            }
          >
            Delete permanently
          </Button>
        </>
      }
    >
      {del.error ? (
        <Notice tone="danger">
          {del.error instanceof Error ? del.error.message : "Unknown error."}
        </Notice>
      ) : null}
      <p>
        Removed from the catalogue and from <code className={CODE}>/v1/models</code>; its
        prices and access grants go with it. Callers asking for{" "}
        <code className={CODE}>{model?.name}</code> get "model not found" from the next
        request on. This cannot be undone.
      </p>
      <p className={MUTED}>
        Recorded spend is unaffected: past usage keeps the model's name and stays attributed to
        the people and groups that ran it.
      </p>
    </Dialog>
  );
}

function CreateModelDialog({ open, onClose }: { open: boolean; onClose: () => void }) {
  const create = useCreateModel();
  const providers = useProviders();
  const toast = useOptionalToast();
  const [name, setName] = useState("");
  const [upstream, setUpstream] = useState("");
  const [providerId, setProviderId] = useState("");
  const [kind, setKind] = useState<ModelKind>("chat");
  const [inputs, setInputs] = useState<string[]>([]);
  const [outputs, setOutputs] = useState<string[]>([]);
  const [features, setFeatures] = useState<string[]>([]);
  // Bumped after a successful create to remount the pickers. Clearing the
  // arrays is not enough on its own: each picker owns the text in its Other
  // box, so without this the previous model's typed-in capabilities are still
  // sitting there and rejoin the set the moment any checkbox is touched.
  const [generation, setGeneration] = useState(0);

  // Only active providers: creating a model on a deactivated endpoint produces
  // something that cannot serve a request the moment it exists.
  const choices = (providers.data?.items ?? []).filter((provider) => provider.is_active);

  return (
    <Dialog
      open={open}
      title="New model"
      onClose={onClose}
      footer={
        <>
          <Button onClick={onClose}>Cancel</Button>
          <Button
            variant="primary"
            busy={create.isPending}
            disabled={!name.trim() || !upstream.trim() || !providerId}
            onClick={() =>
              create.mutate(
                {
                  name: name.trim(),
                  upstream_model: upstream.trim(),
                  provider_id: providerId,
                  kind,
                  input_modalities: inputs,
                  output_modalities: outputs,
                  supported_features: features,
                },
                {
                  onSuccess: () => {
                    toast?.add({ title: "Model created", type: "success" });
                    setName("");
                    setUpstream("");
                    setInputs([]);
                    setOutputs([]);
                    setFeatures([]);
                    setGeneration((current) => current + 1);
                    onClose();
                  },
                  onError: () =>
                    toast?.add({ title: "Could not create the model", type: "error" }),
                },
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
        hint="What callers send as `model`. Cannot be changed later."
      />
      <Input
        label="Upstream model"
        value={upstream}
        onChange={(e) => setUpstream(e.target.value)}
        placeholder="provider/model-id"
        hint="What the gateway asks the provider for."
      />

      <Select
        label="Kind"
        value={kind}
        onChange={(e) => setKind(e.target.value as ModelKind)}
        hint="Which routes will serve it."
      >
        <option value="chat">Chat — /v1/chat/completions, /v1/responses, /v1/messages</option>
        <option value="embedding">Embedding — /v1/embeddings</option>
        <option value="image">Image — /v1/images/generations</option>
      </Select>

      {/* Describing it now rather than importing-then-editing. Left empty this
          reads "not stated", which is honest for a model nobody has described. */}
      <CapabilityPicker
        key={`inputs-${generation}`}
        label="Accepts"
        otherLabel="Other input modalities"
        hint="What can be sent to it."
        known={KNOWN_INPUTS}
        value={inputs}
        onChange={setInputs}
      />
      <CapabilityPicker
        key={`outputs-${generation}`}
        label="Produces"
        otherLabel="Other output modalities"
        hint="What comes back."
        known={KNOWN_OUTPUTS}
        value={outputs}
        onChange={setOutputs}
      />
      <CapabilityPicker
        key={`features-${generation}`}
        label="Features"
        otherLabel="Other features"
        hint="Not limited to the boxes above."
        known={KNOWN_FEATURES}
        value={features}
        onChange={setFeatures}
      />

      <Select
        label="Provider"
        value={providerId}
        onChange={(e) => setProviderId(e.target.value)}
      >
        <option value="">Choose an endpoint…</option>
        {choices.map((provider) => (
          <option key={provider.id} value={provider.id}>
            {provider.name} — {provider.base_url}
          </option>
        ))}
      </Select>
      {choices.length === 0 && !providers.isPending && (
        <Notice tone="warn">
          No active provider. Add one on the Providers page first.
        </Notice>
      )}

      {/* Both are deliberate: a new model is invisible until someone chooses to
          expose it, and an unpriced model would record a cost of zero. */}
      <Notice tone="info">No group is granted access, and no price is set.</Notice>
    </Dialog>
  );
}

/**
 * Grant and revoke a model, per group and per person.
 *
 * Access is the union of the two (ADR 0027), which is why they sit in one dialog:
 * "who can use this" is a single question, and answering it from two screens
 * invites the reading that one overrides the other. Nothing here can *remove*
 * access a group grants — there are no denials.
 */
/**
 * What the provider offers against what we carry.
 *
 * Fetched on open rather than on mount: it calls out to the provider, and a page
 * that hangs on load because a third party is slow is worse than one with a
 * button. Nothing is adopted without an explicit choice — auto-importing would
 * let a provider's release notes change what users can spend money on.
 */
function DiscoveryDialog({ open, onClose }: { open: boolean; onClose: () => void }) {
  const exact = useExactMoney();
  const providers = useProviders();
  const [providerId, setProviderId] = useState("");
  // "What is on offer" is only a meaningful question about one endpoint, so
  // nothing is fetched until one is chosen.
  const discovery = useDiscovery(open && providerId ? providerId : null);
  const importModels = useImportModels();
  const toast = useOptionalToast();
  const [selected, setSelected] = useState<string[]>([]);

  const choices = (providers.data?.items ?? []).filter((provider) => provider.is_active);

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
          <div className={`${MUTED} ${CODE}`}>{row.upstream_model}</div>
          {row.blocked_reason && (
            <div className={MUTED}>
              <Badge tone="warn">Cannot import</Badge> {row.blocked_reason}
            </div>
          )}
        </>
      ),
    },
    {
      key: "capabilities",
      header: "Capabilities",
      // Shown before importing, because "does this one do tool calling" and
      // "can it read an image" are the questions asked at exactly this moment.
      render: (row) => {
        const shown = [
          ...(row.kind !== "chat" ? [row.kind] : []),
          ...row.input_modalities.filter((item) => item !== "text"),
          ...row.supported_features,
        ];
        return shown.length === 0 ? (
          <span className={MUTED}>not stated</span>
        ) : (
          <div className={CHIPS}>
            {shown.map((item) => (
              <Badge key={item}>{item.replace(/_/g, " ")}</Badge>
            ))}
          </div>
        );
      },
    },
    {
      key: "context",
      header: "Context",
      numeric: true,
      render: (row) =>
        row.context_window ? (
          row.context_window.toLocaleString()
        ) : (
          <span className={MUTED}>—</span>
        ),
    },
    {
      key: "price",
      header: "Price / Mtok",
      numeric: true,
      render: (row) =>
        row.input_per_mtok && row.currency
          ? `${formatMoney(row.input_per_mtok, row.currency, { exact })} / ${formatMoney(
              row.output_per_mtok ?? "0",
              row.currency,
              { exact },
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
            disabled={selected.length === 0 || !providerId}
            onClick={() =>
              importModels.mutate(
                { providerId, upstreamModels: selected },
                {
                  onSuccess: () => {
                    toast?.add({ title: "Models imported", type: "success" });
                    setSelected([]);
                  },
                  onError: () =>
                    toast?.add({ title: "Could not import models", type: "error" }),
                },
              )
            }
          >
            Import {selected.length || ""}
          </Button>
        </>
      }
    >
      <Select
        label="Provider"
        value={providerId}
        onChange={(event) => {
          setProviderId(event.target.value);
          setSelected([]);
        }}
      >
        <option value="">Choose an endpoint…</option>
        {choices.map((provider) => (
          <option key={provider.id} value={provider.id}>
            {provider.name}
          </option>
        ))}
      </Select>

      {providerId && discovery.isPending && <Spinner label="Asking the provider" />}
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
