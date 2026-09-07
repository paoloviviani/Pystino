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
import { CHIPS, CODE, FORM, MUTED, PAGE, ROW_ACTIONS } from "../lib/layout";
import { usePaginated } from "../lib/paging";
import type { AdminModel, DiscoveredModel, ModelKind } from "../lib/types";
import { useOptionalToast } from "../lib/toast";


/**
 * The catalogue, grouped by what each model is for.
 *
 * One flat list stopped working when the fourth kind arrived: a chat model, an
 * embedding model and an OCR model are not alternatives an operator chooses
 * between, they are different things that happen to live in one table, and the
 * columns that matter differ — a per-page rate is meaningless on a chat row and
 * a context window is meaningless on an OCR one.
 *
 * Ordered by how often a deployment touches them, not alphabetically. A section
 * with nothing in it is omitted rather than rendered empty: "no image models"
 * is not information anybody needs on the screen where they manage the ones
 * they have.
 *
 * The pager stays on the page as a whole. Paginating each section separately
 * would mean four independent offsets in one URL, and the catalogue is not
 * large enough to earn that.
 */
const KIND_SECTIONS: { kind: ModelKind; title: string; description: string }[] = [
  {
    kind: "chat",
    title: "Chat",
    description: "/v1/chat/completions, /v1/responses and /v1/messages.",
  },
  {
    kind: "embedding",
    title: "Embedding",
    description: "/v1/embeddings. Priced per token, and they generate none.",
  },
  {
    kind: "ocr",
    title: "Document extraction",
    description: "/v1/ocr. Priced per page rather than per token.",
  },
  {
    kind: "image",
    title: "Image",
    description: "/v1/images/generations. Often priced per image.",
  },
];


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
      // Worth a column on a laptop, not worth the price column on a phone.
      hideBelow: "lg",
      render: (model) => <Capabilities model={model} />,
    },
    {
      key: "provider",
      header: "Provider",
      // The "Provider off" badge is the reason this is `md` and not `lg`: a
      // model behind a deactivated provider is unreachable, and that has to be
      // visible before someone spends time wondering why.
      hideBelow: "md",
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
      hideBelow: "lg",
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
      hideBelow: "sm",
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
            {(() => {
              const rows = models.data?.items ?? [];
              if (rows.length === 0) {
                return (
                  <Table
                    columns={columns}
                    rows={[]}
                    rowKey={(model) => model.id}
                    empty={
                      paged.query
                        ? "No model matches that."
                        : "No models catalogued. Use Discover to see what the provider offers."
                    }
                    caption="Catalogued models."
                  />
                );
              }
              // A kind the console does not know about must still be visible:
              // the gateway's enum can gain a value before this file does, and
              // a model that exists but renders nowhere is worse than one in a
              // section headed by its raw name.
              const known = new Set(KIND_SECTIONS.map((section) => section.kind));
              const unknown = rows.filter((model) => !known.has(model.kind));
              const sections = [
                ...KIND_SECTIONS.map((section) => ({
                  ...section,
                  rows: rows.filter((model) => model.kind === section.kind),
                })),
                ...(unknown.length
                  ? [{ kind: "other" as ModelKind, title: "Other", description: "", rows: unknown }]
                  : []),
              ].filter((section) => section.rows.length > 0);

              return sections.map((section, index) => (
                <div
                  key={section.kind}
                  // Two sections in a flush card abutted: the first table's last
                  // row ran straight into the next heading, so nothing marked
                  // where one kind ended. The strong rule, like the table's own
                  // header rule, because this separates *kinds* of row.
                  className={index > 0 ? "border-t border-line" : undefined}
                >
                  {/* `px-5` is the table's cell padding, not a number picked to
                      look right: the heading sat on the card's edge, one inset
                      short of the "Model" column it heads. */}
                  <div className="px-5 pt-5 pb-3">
                    <h2 className="text-md font-semibold leading-tight">
                      {section.title}{" "}
                      <span className={`${MUTED} text-base font-normal`}>
                        ({section.rows.length})
                      </span>
                    </h2>
                    {section.description && (
                      <p className={`${MUTED} mt-1 text-sm`}>{section.description}</p>
                    )}
                  </div>
                  <Table
                    columns={columns}
                    rows={section.rows}
                    rowKey={(model) => model.id}
                    caption={`${section.title} models, their current price and who may use them.`}
                  />
                </div>
              ));
            })()}
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
      {/* Every dialog on every other screen stacks its content with FORM's
          rhythm. The three in this file did not, so their blocks sat flush
          against one another — on the discovery dialog a select, a checkbox and
          a warning read as one undifferentiated column. */}
      <div className={FORM}>
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
      </div>
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
      <div className={FORM}>
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
      </div>
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
  // nothing is fetched until one is chosen — the provider is the first choice
  // here, and the only one that has to be made before anything can be shown.
  //
  // Filling missing prices from the community catalogue (ADR 0053) is a
  // *refinement* of that answer, not a fork in the road: the model list always
  // comes from the provider, and this decides only whether a price the provider
  // left out is taken from LiteLLM's MIT file. Asking it first, as a modal
  // before the provider was even known, made an operator choose between two
  // sources for a provider they had not named yet — and the wrong choice
  // returned an empty screen rather than an explanation.
  const [fillMissing, setFillMissing] = useState(false);
  // Which slice of the provider's catalogue to ask for.
  //
  // Not cosmetic, and the reason it exists is a genuine surprise: Cortecs'
  // `/v1/models` **defaults to `tag=Instruct`**, so a request that looks
  // unfiltered is filtered. Eleven embedding models and three OCR models sat in
  // that endpoint while this console reported the provider offered none of
  // either. Free text rather than a fixed list, because the vocabulary is the
  // counterparty's and one compiled here would go stale the first time they
  // add to it.
  const [tag, setTag] = useState("");
  const discovery = useDiscovery(open && providerId ? providerId : null, fillMissing, tag);
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
      // Dropped on a handset all the same: this dialog is about adopting a
      // model *at a price*, and keeping every column meant the price scrolled
      // off the right-hand edge — the one figure the screen exists to show.
      hideBelow: "md",
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
      hideBelow: "sm",
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
        row.input_per_mtok && row.currency ? (
          <>
            {`${formatMoney(row.input_per_mtok, row.currency, { exact })} / ${formatMoney(
              row.output_per_mtok ?? "0",
              row.currency,
              { exact },
            )}`}
            {/* Only the community figures are marked. A badge on every row
                would be noise; the question is which of these prices a third
                party supplied, and that is the answer to it. */}
            {row.price_source === "community" && (
              <span className="ml-2 align-middle">
                <Badge>community</Badge>
              </span>
            )}
          </>
        ) : (
          <span className={MUTED}>—</span>
        ),
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
                {
                  providerId,
                  upstreamModels: selected,
                  fillMissingPrices: fillMissing,
                  tag,
                },
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
      <div className={FORM}>
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

        <Input
          label="Catalogue tag"
          value={tag}
          onChange={(event) => {
            setTag(event.target.value);
            setSelected([]);
          }}
          placeholder="Instruct"
          hint="Cortecs filters its catalogue by tag and defaults to Instruct — ask for Embedding or OCR to see those."
        />

        {/* Off by default: a provider's own catalogue is the authority where one
            exists, and this is only needed for the APIs that publish nothing.
            Ticking it never overwrites a price the provider published, and each
            filled row is badged `community` — both facts the screen shows by
            doing rather than by explaining. The first version said all of that in
            three lines of prose above the list, on a dialog that has to fit a
            phone: the rationale belongs here, in the comment. */}
        <label className="flex cursor-pointer items-baseline gap-2">
          <input
            type="checkbox"
            checked={fillMissing}
            onChange={(event) => {
              setFillMissing(event.target.checked);
              setSelected([]);
            }}
          />
          <span>
            Fill missing prices from LiteLLM
            <span className={`${MUTED} ml-1 text-sm`}>(community)</span>
          </span>
        </label>

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
                  ? `${result.name} imported${
                      result.priced
                        ? result.price_source === "community"
                          ? " with a community price"
                          : " with its price"
                        : ""
                    }`
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
                {/* Ours on the left, the id it asks for on the right. It read
                    "demo-model (gpt-4o-mini)", which looks like one model with
                    two names rather than one of ours aimed at an id the provider
                    does not have — and the id is the half you have to fix. The
                    arrow carries that direction in the space a sentence wanted
                    two lines for; the notice's own title already says what the
                    problem is, so the row does not repeat it. */}
                {discovery.data.missing_upstream.map((row) => (
                  <div key={row.id} className="flex flex-wrap items-baseline gap-x-2">
                    <Link to={`/admin/models/${row.id}`}>{row.name}</Link>
                    <span aria-hidden>→</span>
                    <code className={CODE}>{row.upstream_model}</code>
                    {/* A deactivated model cannot be called, so this row is a
                        tidy-up rather than a request waiting to fail. */}
                    {!row.is_active && <Badge>inactive</Badge>}
                    <span className="sr-only">is no longer offered by this provider</span>
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
      </div>
    </Dialog>
  );
}
