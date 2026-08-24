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
import { formatMoney } from "@llmp/ui";
import type { ReactNode } from "react";
import { useState } from "react";
import {
  useCreateModel,
  useDiscovery,
  useGroups,
  useImportModels,
  useModelAccess,
  useModels,
  useProviders,
  useUpdateModel,
  useUserModelAccess,
  useUsers,
} from "../lib/admin";
import { usePaginated } from "../lib/paging";
import type { AdminModel, DiscoveredModel, ModelKind } from "../lib/types";
import { PageHeader } from "../components/PageHeader";
import styles from "./Admin.module.css";

export function AdminModels() {
  // A provider catalogue import can bring hundreds of models in one click, so
  // this is one of the two screens that genuinely needs the pager.
  const paged = usePaginated();
  const models = useModels(paged.page);
  const update = useUpdateModel();

  const [creating, setCreating] = useState(false);
  const [access, setAccess] = useState<AdminModel | null>(null);
  const [editing, setEditing] = useState<AdminModel | null>(null);
  const [discovering, setDiscovering] = useState(false);

  const columns: Column<AdminModel>[] = [
    {
      key: "name",
      header: "Model",
      render: (model) => (
        <>
          <div>
            {model.name}{" "}
            {model.kind !== "chat" && <Badge tone="accent">{model.kind}</Badge>}
          </div>
          <div className={`${styles.muted} ${styles.code}`}>{model.upstream_model}</div>
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
      header: "Access",
      render: (model) =>
        model.granted_to.length === 0 && model.granted_to_users.length === 0 ? (
          <span className={styles.muted}>nobody</span>
        ) : (
          <div className={styles.chips}>
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
      render: (model) =>
        model.is_active ? <Badge tone="ok">Active</Badge> : <Badge>Inactive</Badge>,
    },
    {
      key: "actions",
      header: "",
      render: (model) => (
        <div className={styles.rowActions}>
          <Button onClick={() => setAccess(model)}>Access</Button>
          <Button onClick={() => setEditing(model)}>Edit</Button>
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

      <Card>
        <div className={styles.searchRow}>
          <div className={styles.grow}>
            <Input
              label="Search"
              value={paged.search}
              onChange={(event) => paged.setSearch(event.target.value)}
              placeholder="model name or upstream id"
              hint={
                models.data
                  ? `${models.data.total.toLocaleString()} matching`
                  : "Matches both names, so a provider's list can be reconciled against ours."
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
      <EditModelDialog model={editing} onClose={() => setEditing(null)} />
      <AccessDialog model={access} onClose={() => setAccess(null)} />
      <DiscoveryDialog open={discovering} onClose={() => setDiscovering(false)} />
    </div>
  );
}

/**
 * What a model accepts, produces and can do.
 *
 * Three lists rather than a column of yes/no flags, because the provider
 * documents its feature set as open — "current values include json_mode,
 * reasoning and tools" — and a fixed set of checkboxes would silently hide
 * whatever it added last week (ADR 0031).
 *
 * An empty set reads "not stated", never "cannot". Nobody has described most
 * of these models yet, and rendering that as a row of crosses would turn an
 * absence of information into a claim.
 */
function Capabilities({ model }: { model: AdminModel }) {
  const inputs = model.input_modalities.filter((item) => item !== "text");
  const shown = [...inputs, ...model.supported_features];

  if (shown.length === 0) {
    return <span className={styles.muted}>not stated</span>;
  }
  return (
    <div className={styles.chips}>
      {shown.map((item) => (
        <Badge key={item}>{item.replace(/_/g, " ")}</Badge>
      ))}
    </div>
  );
}

const asList = (value: string): string[] =>
  value
    .split(",")
    .map((item) => item.trim())
    .filter(Boolean);

interface Vocabulary {
  value: string;
  /** A plain-English name for a value whose own name does not give it away. */
  gloss?: string;
}

/**
 * The values that get a checkbox.
 *
 * Not a design opinion: this is every value the reference provider's catalogue
 * actually uses, counted across all 107 models it offers. `input_modalities`
 * is never anything but text, image or audio; `supported_features` is never
 * anything but tools, json_mode or reasoning. A checkbox for a value no
 * provider emits is clutter, and one missing for a value they do emit is the
 * bug this is meant to avoid.
 *
 * The two extra output modalities come from this gateway's own model kinds
 * rather than from the catalogue — nothing in it produces embeddings or
 * images, but migration 0006 backfills both and an operator cataloguing such a
 * model by hand needs to say so.
 *
 * These lists are a convenience, never a filter. Anything outside them is
 * typed into the Other box and kept verbatim, which is the whole point of
 * ADR 0031: the capability most worth hearing about is the one the provider
 * added last week, and a vocabulary compiled today would discard exactly that.
 */
const KNOWN_INPUTS: readonly Vocabulary[] = [
  { value: "text" },
  { value: "image", gloss: "vision" },
  { value: "audio" },
];

const KNOWN_OUTPUTS: readonly Vocabulary[] = [
  { value: "text" },
  { value: "image" },
  { value: "embeddings" },
];

const KNOWN_FEATURES: readonly Vocabulary[] = [
  { value: "tools", gloss: "function calling" },
  { value: "json_mode", gloss: "structured output" },
  { value: "reasoning" },
];

/**
 * A capability set: checkboxes for the values providers actually use, and a
 * comma-separated box for everything else.
 *
 * The Other box is not a fallback nobody is expected to reach. It is seeded
 * with whatever the import found that has no checkbox, so an unrecognised
 * capability is *visible and editable* rather than quietly absent — a value
 * with nowhere to render would be dropped by the first save, and the operator
 * would have destroyed information by opening a dialog and clicking Save.
 */
function CapabilityPicker({
  label,
  otherLabel,
  hint,
  known,
  value,
  onChange,
}: {
  label: string;
  otherLabel: string;
  hint: ReactNode;
  known: readonly Vocabulary[];
  value: string[];
  onChange: (next: string[]) => void;
}) {
  const vocabulary = known.map((entry) => entry.value);

  // Seeded once, then owned by the input. Deriving this from `value` on every
  // render would fight the person typing: splitting on the comma they just
  // pressed and joining the result back removes it before they type the next
  // word. The dialog remounts this component when it loads a different model.
  const [other, setOther] = useState(() =>
    value.filter((item) => !vocabulary.includes(item)).join(", "),
  );

  // Set union rather than concatenation: typing a value into Other that also
  // has a checkbox should tick it, not list it twice.
  const emit = (ticked: string[], extras: string) =>
    onChange([...new Set([...ticked, ...asList(extras)])]);

  return (
    <fieldset className={styles.capabilities}>
      <legend className={styles.capabilitiesLegend}>{label}</legend>
      <div className={styles.checkList}>
        {known.map((entry) => (
          <label key={entry.value} className={styles.checkItem}>
            <input
              type="checkbox"
              checked={value.includes(entry.value)}
              onChange={(event) =>
                emit(
                  vocabulary.filter((item) =>
                    item === entry.value ? event.target.checked : value.includes(item),
                  ),
                  other,
                )
              }
            />
            <span>
              {entry.value}
              {entry.gloss && <span className={styles.muted}> ({entry.gloss})</span>}
            </span>
          </label>
        ))}
      </div>
      <Input
        label={otherLabel}
        value={other}
        onChange={(event) => {
          setOther(event.target.value);
          emit(
            value.filter((item) => vocabulary.includes(item)),
            event.target.value,
          );
        }}
        placeholder="comma separated"
        hint={hint}
      />
    </fieldset>
  );
}

/**
 * Correcting what the catalogue claimed.
 *
 * Discovery imports the provider's own description, and a provider's catalogue
 * is a claim rather than a contract — a model advertised as supporting tool
 * calling may do it badly or not at all. Without somewhere to record that, the
 * only options are to believe it or to stop importing.
 *
 * `kind` is editable for the sharper version of the same problem: it is
 * inferred from modality tags, and inferring it wrong takes a model off the
 * only route that would serve it.
 */
function EditModelDialog({ model, onClose }: { model: AdminModel | null; onClose: () => void }) {
  const update = useUpdateModel();
  const [kind, setKind] = useState<ModelKind>("chat");
  const [inputs, setInputs] = useState<string[]>([]);
  const [outputs, setOutputs] = useState<string[]>([]);
  const [features, setFeatures] = useState<string[]>([]);
  const [context, setContext] = useState("");
  const [loadedFor, setLoadedFor] = useState<string | null>(null);

  // Keyed remount: without this the fields keep the previous model's values.
  if (model && loadedFor !== model.id) {
    setLoadedFor(model.id);
    setKind(model.kind);
    setInputs(model.input_modalities);
    setOutputs(model.output_modalities);
    setFeatures(model.supported_features);
    setContext(model.context_window ? String(model.context_window) : "");
  }

  const submit = () => {
    if (!model) return;
    update.mutate(
      {
        id: model.id,
        kind,
        input_modalities: inputs,
        output_modalities: outputs,
        supported_features: features,
        context_window: context === "" ? null : Number(context),
      },
      { onSuccess: onClose },
    );
  };

  return (
    <Dialog
      open={model !== null}
      title={`Edit ${model?.name ?? ""}`}
      onClose={onClose}
      footer={
        <>
          <Button onClick={onClose}>Cancel</Button>
          <Button variant="primary" busy={update.isPending} onClick={submit}>
            Save
          </Button>
        </>
      }
    >
      {update.error ? (
        <Notice tone="danger">
          {update.error instanceof Error ? update.error.message : "Unknown error."}
        </Notice>
      ) : null}

      <Select
        label="Kind"
        value={kind}
        onChange={(event) => setKind(event.target.value as ModelKind)}
        hint="Which routes will serve it. Discovery infers this from the provider's
          modality tags and can get it wrong."
      >
        <option value="chat">Chat — /v1/chat/completions, /v1/responses, /v1/messages</option>
        <option value="embedding">Embedding — /v1/embeddings</option>
        <option value="image">Image — /v1/images/generations</option>
      </Select>

      {/* Remounted per model: each picker seeds its Other box once, from the
          capabilities the model arrived with. */}
      <CapabilityPicker
        key={`inputs-${loadedFor}`}
        label="Accepts"
        otherLabel="Other input modalities"
        hint="What can be sent to it."
        known={KNOWN_INPUTS}
        value={inputs}
        onChange={setInputs}
      />
      <CapabilityPicker
        key={`outputs-${loadedFor}`}
        label="Produces"
        otherLabel="Other output modalities"
        hint="What comes back."
        known={KNOWN_OUTPUTS}
        value={outputs}
        onChange={setOutputs}
      />
      <CapabilityPicker
        key={`features-${loadedFor}`}
        label="Features"
        otherLabel="Other features"
        hint="Not a fixed list — whatever the provider reports is kept, so a capability
          it added last week is not silently dropped."
        known={KNOWN_FEATURES}
        value={features}
        onChange={setFeatures}
      />
      <Input
        label="Context window"
        type="number"
        min="1"
        value={context}
        onChange={(event) => setContext(event.target.value)}
        hint="Tokens. Leave empty if unknown."
      />

      <Notice tone="info">
        These describe the model to callers on <code>/v1/models</code>; they do not change
        what the provider will actually accept. Re-running Discover does not overwrite an
        edit — it only reports models we do not already carry.
      </Notice>
    </Dialog>
  );
}


function CreateModelDialog({ open, onClose }: { open: boolean; onClose: () => void }) {
  const create = useCreateModel();
  const providers = useProviders();
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
      title="Add a model"
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
                    setName("");
                    setUpstream("");
                    setInputs([]);
                    setOutputs([]);
                    setFeatures([]);
                    setGeneration((current) => current + 1);
                    onClose();
                  },
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
        hint="What callers send as `model`. Cannot be changed later — usage rows record it."
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
        hint="Decides which routes will serve it. A model asked for on the wrong one is
          refused with a message naming the right one."
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
        hint="Anything the provider reports. Not limited to the boxes above."
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
          No active provider. Add one on the Providers page first — a model has to
          resolve to an endpoint.
        </Notice>
      )}

      <Notice tone="info">
        No group is granted access, and no price is set. Both are deliberate: a new
        model is invisible until someone chooses to expose it, and an unpriced model
        would record a cost of zero.
      </Notice>
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
function AccessDialog({ model, onClose }: { model: AdminModel | null; onClose: () => void }) {
  const groups = useGroups();
  const access = useModelAccess();
  const userAccess = useUserModelAccess();
  const finder = usePaginated(20);

  // The directory is searched, not downloaded. Nothing is requested until
  // something is typed: opening this dialog to grant one person access should
  // not pull a page of accounts nobody asked about.
  const users = useUsers(finder.page, finder.query.trim().length > 0);
  const matching = finder.query.trim() ? (users.data?.items ?? []) : [];
  const granted = model?.granted_to_users ?? [];

  return (
    <Dialog open={model !== null} title={`Access · ${model?.name ?? ""}`} onClose={onClose}>
      {access.error ? (
        <Notice tone="danger">
          {access.error instanceof Error ? access.error.message : "Unknown error."}
        </Notice>
      ) : null}

      <p className={styles.muted}>Groups</p>
      {groups.isPending ? (
        <Spinner />
      ) : (
        <div className={styles.checkList}>
          {(groups.data?.items ?? []).map((group) => {
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

      <p className={styles.muted}>
        Individual people, in addition to their groups. Everyone already granted is listed
        below; search to add someone else.
      </p>
      {granted.length > 0 && (
        <div className={styles.checkList}>
          {granted.map((email) => (
            <label key={email} className={styles.checkItem}>
              <input
                type="checkbox"
                checked
                disabled={userAccess.isPending}
                onChange={() => {
                  // Revoking needs the id, and the grant list carries only the
                  // label — so find the account by searching for it. Exact,
                  // because an email is unique.
                  const match = users.data?.items.find((entry) => entry.email === email);
                  if (match && model) {
                    userAccess.mutate({ userId: match.id, modelId: model.id, grant: false });
                  } else {
                    finder.setSearch(email);
                  }
                }}
              />
              <span>{email}</span>
            </label>
          ))}
        </div>
      )}
      <Input
        label="Find a person"
        hideLabel
        value={finder.search}
        onChange={(event) => finder.setSearch(event.target.value)}
        placeholder="email, name or subject"
      />
      {userAccess.error ? (
        <Notice tone="danger">
          {userAccess.error instanceof Error ? userAccess.error.message : "Unknown error."}
        </Notice>
      ) : null}
      <div className={styles.checkList}>
        {matching.map((user) => {
          const has = granted.includes(user.email ?? "");
          return (
            <label key={user.id} className={styles.checkItem}>
              <input
                type="checkbox"
                checked={has}
                disabled={userAccess.isPending}
                onChange={() =>
                  model && userAccess.mutate({ userId: user.id, modelId: model.id, grant: !has })
                }
              />
              <span>{user.email ?? user.display_name ?? user.subject}</span>
            </label>
          );
        })}
        {matching.length === 0 && (
          <span className={styles.muted}>
            {!finder.query.trim()
              ? granted.length === 0
                ? "No individual grants."
                : "Type to find someone else."
              : users.isFetching
                ? "Searching…"
                : "Nobody matches that."}
          </span>
        )}
        {users.data && users.data.total > matching.length && (
          <span className={styles.muted}>
            Showing {matching.length} of {users.data.total.toLocaleString()} matches — narrow
            the search to see the rest.
          </span>
        )}
      </div>

      <Notice tone="info">
        Absence of a grant means no access — there is no global allow-all. A person
        may reach a model through their group or personally; removing one leaves the
        other. Revoking takes effect on the next request.
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
  const providers = useProviders();
  const [providerId, setProviderId] = useState("");
  // "What is on offer" is only a meaningful question about one endpoint, so
  // nothing is fetched until one is chosen.
  const discovery = useDiscovery(open && providerId ? providerId : null);
  const importModels = useImportModels();
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
          <span className={styles.muted}>not stated</span>
        ) : (
          <div className={styles.chips}>
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
          <span className={styles.muted}>—</span>
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
            disabled={selected.length === 0 || !providerId}
            onClick={() =>
              importModels.mutate(
                { providerId, upstreamModels: selected },
                { onSuccess: () => setSelected([]) },
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
