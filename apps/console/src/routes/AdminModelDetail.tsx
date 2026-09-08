import {
  Badge,
  Button,
  Card,
  Dialog,
  Input,
  Money,
  Notice,
  Select,
  Spinner,
  Table,
  
} from "@llmp/ui";
import type { Column } from "@llmp/ui";
import type { ReactNode } from "react";
import { useState } from "react";
import { Link, useNavigate, useParams } from "react-router";
import {
  CapabilityPicker,
  KNOWN_FEATURES,
  KNOWN_INPUTS,
  KNOWN_OUTPUTS,
} from "../components/CapabilityPicker";
import { PageHeader } from "../components/PageHeader";
import {
  useCreatePrice,
  useDeleteModel,
  useGroups,
  useModel,
  useModelAccess,
  usePrices,
  useUpdateModel,
  useUserModelAccess,
  useUsers,
} from "../lib/admin";
import {
  CHIPS,
  CHECK_ITEM,
  CHECK_LIST,
  CODE,
  DETAIL_LABEL,
  DETAIL_VALUE,
  DETAILS,
  FORM,
  FORM_ROW,
  MUTED,
  PAGE,
} from "../lib/layout";
import { usePaginated } from "../lib/paging";
import type { AdminModel, ModelKind, Price } from "../lib/types";
import { useOptionalToast } from "../lib/toast";


/**
 * One model, and everything that is true of it.
 *
 * Pricing used to be its own screen, with its own model picker. That made the
 * two halves of one question — "what is this model, and what does it cost?" —
 * into two navigations and a re-selection, and it made the dangerous state
 * (catalogued, granted, unpriced, therefore billing zero) something you had to
 * go and look for on another page. They are one page now, and the price is
 * beside the grants that decide who can spend it.
 *
 * A page rather than a bigger dialog: there is a price *history* here, and a
 * dialog that scrolls is a dialog that should have been a page. It also makes
 * the model addressable — a link in a ticket, a bookmark, a reload that lands
 * where it left off.
 */
export function AdminModelDetail() {
  const { modelId = null } = useParams();
  const model = useModel(modelId);

  if (model.isPending) {
    return (
      <div className={PAGE}>
        <Spinner label="Loading the model" />
      </div>
    );
  }

  if (model.error || !model.data) {
    return (
      <div className={PAGE}>
        <Notice tone="danger" title="Could not load this model">
          {model.error instanceof Error ? model.error.message : "Unknown error."}{" "}
          <Link to="/admin/models">Back to the catalogue</Link>
        </Notice>
      </div>
    );
  }

  return <ModelPage model={model.data} />;
}

function ModelPage({ model }: { model: AdminModel }) {
  const update = useUpdateModel();
  const del = useDeleteModel();
  const navigate = useNavigate();
  const toast = useOptionalToast();
  const [confirming, setConfirming] = useState(false);

  return (
    <div className={PAGE}>
      <PageHeader
        title={model.name}
        subtitle={
          <>
            <Link to="/admin/models">Models</Link> · served as{" "}
            <code className={CODE}>{model.upstream_model}</code> by {model.provider_name}
          </>
        }
        actions={
          <>
            <Button
              busy={update.isPending}
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
            <Button variant="ghost" onClick={() => setConfirming(true)}>
              Delete
            </Button>
          </>
        }
      />

      {update.error ? (
        <Notice tone="danger" title="Could not update the model">
          {update.error instanceof Error ? update.error.message : "Unknown error."}
        </Notice>
      ) : null}

      <Dialog
        open={confirming}
        title={`Delete ${model.name}`}
        onClose={() => setConfirming(false)}
        footer={
          <>
            <Button onClick={() => setConfirming(false)}>Cancel</Button>
            <Button
              variant="danger"
              busy={del.isPending}
              onClick={() =>
                del.mutate(model.id, {
                  onSuccess: () => {
                    toast?.add({ title: "Model deleted", type: "success" });
                    navigate("/admin/models");
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
          Removed from the catalogue and from{" "}
          <code className={CODE}>/v1/models</code>; its prices and access grants go with it.
          This cannot be undone.
        </p>
        <p className={MUTED}>
          Recorded spend is unaffected: past usage keeps the model's name and stays attributed to
          the people and groups that ran it.
        </p>
      </Dialog>

      {/* The two states worth interrupting someone about, and they are not
          symmetrical: an inactive model serves nobody and is obvious the moment
          anyone tries it, while an unpriced one serves everybody and records a
          cost of zero, which nobody notices until the reconciliation. */}
      {/* A price appended now applies from now on; it cannot rewrite what
          earlier requests were charged. */}
      {!model.current_price && (
        <Notice tone="warn" title="This model has no price">
          It will serve requests and record a cost of zero. Append a price below.
        </Notice>
      )}
      {!model.provider_is_active && (
        <Notice tone="danger" title={`${model.provider_name} is deactivated`}>
          Every model behind it is out of service.
        </Notice>
      )}

      <Card title="Details">
        <dl className={DETAILS}>
          <Detail label="Status">
            {model.is_active ? <Badge tone="ok">Active</Badge> : <Badge>Inactive</Badge>}
          </Detail>
          <Detail label="Visibility">
            <div className={CHIPS}>
              {model.is_public ? <Badge tone="accent">Public</Badge> : <Badge>Private</Badge>}
              <Button
                busy={update.isPending && update.variables?.is_public !== undefined}
                onClick={() =>
                  update.mutate(
                    { id: model.id, is_public: !model.is_public },
                    {
                      onSuccess: () =>
                        toast?.add({ title: "Model updated", type: "success" }),
                      onError: () =>
                        toast?.add({ title: "Could not update the model", type: "error" }),
                    },
                  )
                }
              >
                {model.is_public ? "Restrict to grants" : "Make public"}
              </Button>
            </div>
          </Detail>
          <Detail label="Name callers send">
            <code className={CODE}>{model.name}</code>
          </Detail>
          <Detail label="Upstream model">
            <code className={CODE}>{model.upstream_model}</code>
          </Detail>
          <Detail label="Provider">
            <Link to="/admin/providers">{model.provider_name}</Link>
          </Detail>
        </dl>
      </Card>

      <Describe model={model} />
      <Pricing model={model} />
      <Access model={model} />
    </div>
  );
}

function Detail({ label, children }: { label: string; children: ReactNode }) {
  return (
    <>
      <dt className={DETAIL_LABEL}>{label}</dt>
      <dd className={DETAIL_VALUE}>{children}</dd>
    </>
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
function Describe({ model }: { model: AdminModel }) {
  const update = useUpdateModel();
  const toast = useOptionalToast();
  const [kind, setKind] = useState<ModelKind>(model.kind);
  const [inputs, setInputs] = useState<string[]>(model.input_modalities);
  const [outputs, setOutputs] = useState<string[]>(model.output_modalities);
  const [features, setFeatures] = useState<string[]>(model.supported_features);
  const [context, setContext] = useState(
    model.context_window ? String(model.context_window) : "",
  );

  return (
    <Card
      title="Capabilities"
      description="What callers are told on /v1/models. Discover never overwrites an edit."
    >
      <div className={FORM}>
        {update.error ? (
          <Notice tone="danger">
            {update.error instanceof Error ? update.error.message : "Unknown error."}
          </Notice>
        ) : null}
        {update.isSuccess && !update.isPending && <Notice tone="info">Saved.</Notice>}

        <Select
          label="Kind"
          value={kind}
          onChange={(event) => setKind(event.target.value as ModelKind)}
          hint="Which routes will serve it. Discovery can infer it wrong."
        >
          <option value="chat">Chat — /v1/chat/completions, /v1/responses, /v1/messages</option>
          <option value="embedding">Embedding — /v1/embeddings</option>
          <option value="image">Image — /v1/images/generations</option>
        </Select>

        <CapabilityPicker
          label="Accepts"
          otherLabel="Other input modalities"
          hint="What can be sent to it."
          known={KNOWN_INPUTS}
          value={inputs}
          onChange={setInputs}
        />
        <CapabilityPicker
          label="Produces"
          otherLabel="Other output modalities"
          hint="What comes back."
          known={KNOWN_OUTPUTS}
          value={outputs}
          onChange={setOutputs}
        />
        <CapabilityPicker
          label="Features"
          otherLabel="Other features"
          hint="Whatever the provider reports is kept."
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

        <div>
          <Button
            variant="primary"
            busy={update.isPending}
            onClick={() =>
              update.mutate(
                {
                  id: model.id,
                  kind,
                  input_modalities: inputs,
                  output_modalities: outputs,
                  supported_features: features,
                  context_window: context === "" ? null : Number(context),
                },
                {
                  onSuccess: () =>
                    toast?.add({ title: "Capabilities saved", type: "success" }),
                  onError: () =>
                    toast?.add({ title: "Could not save the capabilities", type: "error" }),
                },
              )
            }
          >
            Save capabilities
          </Button>
        </div>
      </div>
    </Card>
  );
}

/**
 * Price history and the form that appends to it.
 *
 * There is no edit, and that is the design rather than a missing feature: a
 * price is effective-dated and append-only, so a future date schedules a change
 * and a past one cannot rewrite what already-recorded requests cost (ADR 0008).
 */
function Pricing({ model }: { model: AdminModel }) {
  const prices = usePrices(model.id);
  const now = new Date();

  const columns: Column<Price>[] = [
    {
      key: "effective",
      header: "Effective from",
      render: (price) => (
        <>
          <div>{formatDateTime(price.effective_from)}</div>
          {new Date(price.effective_from) > now && <Badge tone="accent">Scheduled</Badge>}
        </>
      ),
    },
    {
      key: "input",
      header: "Input / Mtok",
      numeric: true,
      render: (price) => <Money amount={price.input_per_mtok} currency={price.currency} />,
    },
    {
      key: "output",
      header: "Output / Mtok",
      numeric: true,
      render: (price) => <Money amount={price.output_per_mtok} currency={price.currency} />,
    },
    // Cache rates and the per-image rate are all optional, and a dash is not the
    // same claim as a zero: "not priced this way" and "free" are different
    // facts, and one of them is a decision somebody made.
    {
      key: "cache",
      header: "Cache read / write",
      numeric: true,
      render: (price) =>
        price.cache_read_per_mtok || price.cache_write_per_mtok ? (
          <>
            <div>
              <Rate amount={price.cache_read_per_mtok} currency={price.currency} />
            </div>
            <div className={MUTED}>
              <Rate amount={price.cache_write_per_mtok} currency={price.currency} />
            </div>
          </>
        ) : (
          <span className={MUTED}>—</span>
        ),
    },
    {
      key: "image",
      header: "Per image",
      numeric: true,
      // Both unit rates share a column: a model is priced per image or per
      // page, never both, and two mostly-empty columns on a narrow screen
      // pushed the source badge off the edge. The header names whichever this
      // model uses.
      render: (price) => <Rate amount={price.per_image} currency={price.currency} />,
      hideBelow: "md",
    },
    {
      key: "page",
      header: "Per page",
      numeric: true,
      render: (price) => <Rate amount={price.per_page} currency={price.currency} />,
      hideBelow: "md",
    },
    {
      key: "search",
      header: "Per search",
      numeric: true,
      render: (price) => <Rate amount={price.per_search} currency={price.currency} />,
      hideBelow: "lg",
    },
    {
      key: "source",
      header: "Source",
      render: (price) => <Badge>{price.source}</Badge>,
    },
  ];

  return (
    <>
      <Card
        title="Price history"
        flush
        description="Newest first. A correction is a new row."
      >
        {prices.isPending ? (
          <Spinner />
        ) : prices.error ? (
          <Notice tone="danger" title="Could not load the price history">
            {prices.error instanceof Error ? prices.error.message : "Unknown error."}
          </Notice>
        ) : (
          <Table
            columns={columns}
            rows={prices.data?.items ?? []}
            rowKey={(price) => price.id}
            empty="No price set. This model records a cost of zero."
            caption={`Price history for ${model.name}.`}
          />
        )}
      </Card>
      <AppendPrice model={model} />
    </>
  );
}

function Rate({ amount, currency }: { amount: string | null; currency: string }) {
  if (!amount) return <span className={MUTED}>—</span>;
  return <Money amount={amount} currency={currency} />;
}

function AppendPrice({ model }: { model: AdminModel }) {
  const create = useCreatePrice();
  const toast = useOptionalToast();
  const [input, setInput] = useState("");
  const [output, setOutput] = useState("");
  const [cacheRead, setCacheRead] = useState("");
  const [cacheWrite, setCacheWrite] = useState("");
  const [perImage, setPerImage] = useState("");
  const [perPage, setPerPage] = useState("");
  const [perSearch, setPerSearch] = useState("");
  const [effective, setEffective] = useState("");

  const submit = () => {
    create.mutate(
      {
        modelId: model.id,
        input_per_mtok: input,
        output_per_mtok: output,
        // Omitted rather than sent as zero: zero is a real price meaning
        // "free", and a model with no cache rate is billed at the input rate,
        // which is a different statement from being billed nothing.
        cache_read_per_mtok: cacheRead === "" ? null : cacheRead,
        cache_write_per_mtok: cacheWrite === "" ? null : cacheWrite,
        per_image: perImage === "" ? null : perImage,
        per_page: perPage === "" ? null : perPage,
        per_search: perSearch === "" ? null : perSearch,
        // A datetime-local value carries no zone; converting through Date makes
        // the browser's zone explicit rather than letting the server guess.
        effective_from: effective ? new Date(effective).toISOString() : null,
      },
      {
        onSuccess: () => {
          toast?.add({ title: "Price appended", type: "success" });
          setInput("");
          setOutput("");
          setCacheRead("");
          setCacheWrite("");
          setPerImage("");
          setPerPage("");
          setPerSearch("");
          setEffective("");
        },
        onError: () => toast?.add({ title: "Could not append the price", type: "error" }),
      },
    );
  };

  return (
    <Card title="New price">
      <div className={FORM}>
        {create.error ? (
          <Notice tone="danger">
            {create.error instanceof Error ? create.error.message : "Unknown error."}
          </Notice>
        ) : null}
        {create.isSuccess && !create.isPending && (
          <Notice tone="info">Price appended. It applies from its effective date.</Notice>
        )}

        <div className={FORM_ROW}>
          <Input
            label="Input per Mtok"
            type="number"
            min="0"
            step="0.000001"
            value={input}
            onChange={(event) => setInput(event.target.value)}
            hint="In the gateway's billing currency"
          />
          <Input
            label="Output per Mtok"
            type="number"
            min="0"
            step="0.000001"
            value={output}
            onChange={(event) => setOutput(event.target.value)}
          />
          <Input
            label="Cache read per Mtok"
            type="number"
            min="0"
            step="0.000001"
            value={cacheRead}
            onChange={(event) => setCacheRead(event.target.value)}
            hint="Empty bills cached input at the full input rate."
          />
          <Input
            label="Cache write per Mtok"
            type="number"
            min="0"
            step="0.000001"
            value={cacheWrite}
            onChange={(event) => setCacheWrite(event.target.value)}
          />
          <Input
            label="Per image"
            type="number"
            min="0"
            step="0.000001"
            value={perImage}
            onChange={(event) => setPerImage(event.target.value)}
            hint="Image models only, on top of any token rates."
          />
          <Input
            label="Per page"
            type="number"
            min="0"
            step="0.0000001"
            value={perPage}
            onChange={(event) => setPerPage(event.target.value)}
            hint="OCR models, which charge by the page and usually nothing per token."
          />
          <Input
            label="Per search"
            type="number"
            min="0"
            step="0.0000001"
            value={perSearch}
            onChange={(event) => setPerSearch(event.target.value)}
            hint="Provider-side web search, charged per search on top of tokens.
              Providers publish it per thousand — $10 per 1,000 is 0.01 here."
          />
          <Input
            label="Effective from"
            type="datetime-local"
            value={effective}
            onChange={(event) => setEffective(event.target.value)}
            hint="Leave empty for now. A future date schedules the change."
          />
        </div>

        <div>
          <Button
            variant="primary"
            busy={create.isPending}
            disabled={input === "" || output === ""}
            onClick={submit}
          >
            Append price
          </Button>
        </div>
      </div>
    </Card>
  );
}

/**
 * Who may use this model.
 *
 * Groups and individuals are unioned at request time, so they are shown
 * together rather than as alternatives — a person may reach a model through
 * their group or personally, and removing one leaves the other.
 */
function Access({ model }: { model: AdminModel }) {
  const groups = useGroups();
  const access = useModelAccess();
  const userAccess = useUserModelAccess();
  const finder = usePaginated(20);

  // The directory is searched, not downloaded. Nothing is requested until
  // something is typed: granting one person access should not pull a page of
  // accounts nobody asked about.
  const users = useUsers(finder.page, finder.query.trim().length > 0);
  const matching = finder.query.trim() ? (users.data?.items ?? []) : [];
  const granted = model.granted_to_users;

  return (
    <Card title="Access" description="No grant means no access; there is no allow-all.">
      {access.error ? (
        <Notice tone="danger">
          {access.error instanceof Error ? access.error.message : "Unknown error."}
        </Notice>
      ) : null}

      <p className={MUTED}>Groups</p>
      {groups.isPending ? (
        <Spinner />
      ) : (
        <div className={CHECK_LIST}>
          {(groups.data?.items ?? []).map((group) => {
            const has = group.models.includes(model.name);
            return (
              <label key={group.id} className={CHECK_ITEM}>
                <input
                  type="checkbox"
                  checked={has}
                  disabled={access.isPending}
                  onChange={() =>
                    access.mutate({ groupId: group.id, modelId: model.id, grant: !has })
                  }
                />
                <span>{group.name}</span>
              </label>
            );
          })}
        </div>
      )}

      <p className={MUTED}>Individuals, in addition to their groups.</p>
      {granted.length > 0 && (
        <div className={CHECK_LIST}>
          {granted.map((email) => (
            <label key={email} className={CHECK_ITEM}>
              <input
                type="checkbox"
                checked
                disabled={userAccess.isPending}
                onChange={() => {
                  // Revoking needs the id, and the grant list carries only the
                  // label — so find the account by searching for it. Exact,
                  // because an email is unique.
                  const match = users.data?.items.find((entry) => entry.email === email);
                  if (match) {
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
        label="Person search"
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
      <div className={CHECK_LIST}>
        {matching.map((user) => {
          const has = granted.includes(user.email ?? "");
          return (
            <label key={user.id} className={CHECK_ITEM}>
              <input
                type="checkbox"
                checked={has}
                disabled={userAccess.isPending}
                onChange={() =>
                  userAccess.mutate({ userId: user.id, modelId: model.id, grant: !has })
                }
              />
              <span>{user.email ?? user.display_name ?? user.subject}</span>
            </label>
          );
        })}
        {matching.length === 0 && (
          <span className={MUTED}>
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
          <span className={MUTED}>
            Showing {matching.length} of {users.data.total.toLocaleString()} matches. Narrow
            the search to see the rest.
          </span>
        )}
      </div>

      {/* Deactivating the model instead takes it away from everyone at once, and
          keeps historical spend attributable. */}
      <Notice tone="info">Revoking takes effect on the next request.</Notice>
    </Card>
  );
}

function formatDateTime(iso: string): string {
  return new Date(iso).toLocaleString(undefined, {
    year: "numeric",
    month: "short",
    day: "numeric",
    hour: "2-digit",
    minute: "2-digit",
  });
}
