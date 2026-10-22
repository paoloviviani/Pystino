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
import { formatNumber } from "../lib/format";
import { Message } from "../components/Message";
import { m } from "../paraglide/messages.js";
import {
  useCatalogueTags,
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
import type {
  AdminModel,
  DiscoveredModel,
  ManualImportPrice,
  ModelKind,
} from "../lib/types";
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
/** Message functions, not strings: the list is built once, at import. */
const KIND_SECTIONS: {
  kind: ModelKind;
  title: () => string;
  description: () => string;
}[] = [
  {
    kind: "chat",
    title: m["admin_models.section.chat"],
    description: m["admin_models.section.chat_description"],
  },
  {
    kind: "embedding",
    title: m["admin_models.section.embedding"],
    description: m["admin_models.section.embedding_description"],
  },
  {
    kind: "ocr",
    title: m["admin_models.section.ocr"],
    description: m["admin_models.section.ocr_description"],
  },
  {
    kind: "decision",
    title: m["admin_models.section.decision"],
    description: m["admin_models.section.decision_description"],
  },
  {
    kind: "image",
    title: m["admin_models.section.image"],
    description: m["admin_models.section.image_description"],
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
  // Rows this screen must not render as a catalogue entry.
  //
  // Search tiers are a different concept and have their own screen
  // (ADR 0071): they answer /v1/search and were rendered here as though a
  // model kind, which is how an operator came to manage a depth among the
  // prices-per-million.
  //
  // The deployment's own infrastructure is the other: the extractor's
  // `markitdown` row exists so grants, prices and the ledger have something
  // to hang on, and it is seeded by default (migration 0041) — rendering it
  // beside the catalogue made the screen present plumbing as a choice, with
  // a "no price" warning attached that is the design and not an oversight.
  // Hidden here, not gone: the row is still served by `/api/admin/models`,
  // so the API stays complete and a future management screen can filter the
  // other way, exactly as the search screen does.
  const hiddenRowIds = new Set(
    (models.data?.items ?? [])
      .filter(
        (model) => model.kind === "search" || model.provider_kind === "internal",
      )
      .map((model) => model.id),
  );

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
      header: m["admin_models.column.model"](),
      render: (model) => (
        <>
          <div>
            {/* The name is the link, not only the button beside it: it is what
                someone points at, and a real anchor is what makes "open in a new
                tab" and "copy link" work at all. */}
            {/* A display name, when set, leads and the id follows: the id is
                what a request carries and what every report is grouped by, so
                it has to stay visible beside the friendlier name rather than
                be replaced by it. */}
            <Link to={`/admin/models/${model.id}`}>
              {model.display_name || model.name}
            </Link>{" "}
            {model.kind !== "chat" && <Badge tone="accent">{model.kind}</Badge>}
          </div>
          {model.display_name && (
            <div className={CODE}>{model.name}</div>
          )}
          <div className={`${MUTED} ${CODE}`}>{model.upstream_model}</div>
        </>
      ),
    },
    {
      key: "capabilities",
      header: m["admin_models.column.capabilities"](),
      // Worth a column on a laptop, not worth the price column on a phone.
      hideBelow: "lg",
      render: (model) => <Capabilities model={model} />,
    },
    {
      key: "provider",
      header: m["admin_models.column.provider"](),
      // The "Provider off" badge is the reason this is `md` and not `lg`: a
      // model behind a deactivated provider is unreachable, and that has to be
      // visible before someone spends time wondering why.
      hideBelow: "md",
      render: (model) => (
        <>
          <div>{model.provider_name}</div>
          {/* Deactivating a provider silently takes every model behind it out
              of service. The catalogue is where that has to be visible. */}
          {!model.provider_is_active && (
            <Badge tone="danger">{m["admin_models.badge.provider_off"]()}</Badge>
          )}
        </>
      ),
    },
    {
      key: "price",
      header: m["admin_models.column.price"](),
      numeric: true,
      render: (model) =>
        model.current_price ? (
          <>
            <div>
              {m["admin_models.price.in"]({
                rate: formatMoney(model.current_price.input_per_mtok, model.current_price.currency, {
                  exact,
                }),
              })}
            </div>
            <div className={MUTED}>
              {m["admin_models.price.out"]({
                rate: formatMoney(
                  model.current_price.output_per_mtok,
                  model.current_price.currency,
                  { exact },
                ),
              })}
            </div>
          </>
        ) : (
          // An unpriced model serves happily and records a cost of zero, which
          // is a quiet way to give away money. Worth flagging in the list.
          <Badge tone="warn">{m["admin_models.badge.no_price"]()}</Badge>
        ),
    },
    {
      key: "access",
      header: m["admin_models.column.access"](),
      hideBelow: "lg",
      render: (model) =>
        model.granted_to.length === 0 && model.granted_to_users.length === 0 ? (
          <span className={MUTED}>{m["admin_models.access.nobody"]()}</span>
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
      header: m["admin_models.column.status"](),
      hideBelow: "sm",
      render: (model) => (
        <div className={CHIPS}>
          {model.is_active ? (
            <Badge tone="ok">{m["admin_models.badge.active"]()}</Badge>
          ) : (
            <Badge>{m["admin_models.badge.inactive"]()}</Badge>
          )}
          {model.is_public && <Badge tone="accent">{m["admin_models.badge.public"]()}</Badge>}
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
          <Button onClick={() => navigate(`/admin/models/${model.id}`)}>
            {m["admin_models.action.edit"]()}
          </Button>
          <Button
            busy={update.isPending && update.variables?.id === model.id}
            onClick={() =>
              update.mutate(
                { id: model.id, is_active: !model.is_active },
                {
                  onSuccess: () =>
                    toast?.add({ title: m["admin_models.toast.updated"](), type: "success" }),
                  onError: () =>
                    toast?.add({
                      title: m["admin_models.toast.update_failed"](),
                      type: "error",
                    }),
                },
              )
            }
          >
            {model.is_active
              ? m["admin_models.action.deactivate"]()
              : m["admin_models.action.activate"]()}
          </Button>
          <Button variant="ghost" className="text-danger" onClick={() => setDeleting(model)}>
            {m["admin_models.action.delete"]()}
          </Button>
        </div>
      ),
    },
  ];

  return (
    <div className={PAGE}>
      <PageHeader
        title={m["admin_models.title"]()}
        subtitle={m["admin_models.subtitle"]()}
        actions={
          <>
            <Button onClick={() => setDiscovering(true)}>{m["admin_models.discover"]()}</Button>
            <Button variant="primary" onClick={() => setCreating(true)}>
              {m["admin_models.add_model"]()}
            </Button>
          </>
        }
      />

      {update.error ? (
        <Notice tone="danger" title={m["admin_models.load_error.update"]()}>
          {/* i18n: server text — the API's own error message. */}
          {update.error instanceof Error ? update.error.message : m["admin_models.unknown_error"]()}
        </Notice>
      ) : null}

      {deleteModel.error ? (
        <Notice tone="danger" title={m["admin_models.load_error.delete"]()}>
          {/* i18n: server text — the API's own error message. */}
          {deleteModel.error instanceof Error
            ? deleteModel.error.message
            : m["admin_models.unknown_error"]()}
        </Notice>
      ) : null}

      <Card>
        {/* The search row and its growing field are a one-off shape (a lone
            input that must shrink before the row wraps), so they are inline
            here rather than constants in lib/layout. */}
        <div className="flex flex-wrap items-end gap-3">
          <div className="min-w-56 flex-1">
            <Input
              label={m["admin_models.search"]()}
              value={paged.search}
              onChange={(event) => paged.setSearch(event.target.value)}
              placeholder={m["admin_models.search_placeholder"]()}
              hint={
                models.data
                  ? m["admin_models.search_hint_matching"]({ count: models.data.total })
                  : m["admin_models.search_hint_default"]()
              }
            />
          </div>
        </div>
      </Card>

      <Card flush>
        {models.isPending ? (
          <Spinner label={m["admin_models.loading"]()} />
        ) : models.error ? (
          <Notice tone="danger" title={m["admin_models.load_error.title"]()}>
            {/* i18n: server text — the API's own error message. */}
            {models.error instanceof Error ? models.error.message : m["admin_models.unknown_error"]()}
          </Notice>
        ) : (
          <>
            {(() => {
              const all = models.data?.items ?? [];
              if (all.length === 0) {
                return (
                  <Table
                    columns={columns}
                    rows={[]}
                    rowKey={(model) => model.id}
                    empty={
                      paged.query
                        ? m["admin_models.empty_query"]()
                        : m["admin_models.empty"]()
                    }
                    caption={m["admin_models.caption"]()}
                  />
                );
              }
              // A kind the console does not know about must still be visible:
              // the gateway's enum can gain a value before this file does, and
              // a model that exists but renders nowhere is worse than one in a
              // section headed by its raw name.
              const rows = (models.data?.items ?? []).filter(
                (model) => !hiddenRowIds.has(model.id),
              );
              const known = new Set(KIND_SECTIONS.map((section) => section.kind));
              const unknown = rows.filter((model) => !known.has(model.kind));
              const sections = [
                ...KIND_SECTIONS.map((section) => ({
                  ...section,
                  rows: rows.filter((model) => model.kind === section.kind),
                })),
                ...(unknown.length
                  ? [
                      {
                        kind: "other" as ModelKind,
                        title: () => m["admin_models.section.other"](),
                        description: () => "",
                        rows: unknown,
                      },
                    ]
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
                      {section.title()}{" "}
                      <span className={`${MUTED} text-base font-normal`}>
                        ({formatNumber(section.rows.length)})
                      </span>
                    </h2>
                    {section.description() && (
                      <p className={`${MUTED} mt-1 text-sm`}>{section.description()}</p>
                    )}
                  </div>
                  <Table
                    columns={columns}
                    rows={section.rows}
                    rowKey={(model) => model.id}
                    caption={m["admin_models.section.caption"]({ section: section.title() })}
                  />
                </div>
              ));
            })()}
            <Pagination
              total={models.data?.total ?? 0}
              limit={paged.limit}
              offset={paged.offset}
              onOffsetChange={paged.setOffset}
              noun={m["admin_models.noun"]()}
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
      title={m["admin_models.delete.title"]({
        name: model?.name ?? m["admin_models.delete.title_fallback"](),
      })}
      onClose={onClose}
      footer={
        <>
          <Button onClick={onClose}>{m["admin_models.delete.cancel"]()}</Button>
          <Button
            variant="danger"
            busy={del.isPending}
            onClick={() =>
              model &&
              del.mutate(model.id, {
                onSuccess: () => {
                  toast?.add({ title: m["admin_models.delete.toast_done"](), type: "success" });
                  onClose();
                },
                onError: () =>
                  toast?.add({
                    title: m["admin_models.delete.toast_failed"](),
                    type: "error",
                  }),
              })
            }
          >
            {m["admin_models.delete.confirm"]()}
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
            {/* i18n: server text — the API's own error message. */}
            {del.error instanceof Error ? del.error.message : m["admin_models.unknown_error"]()}
          </Notice>
        ) : null}
        <p>
          <Message
            message={m["admin_models.delete.body"]}
            markup={{ code: (children) => <code className={CODE}>{children}</code> }}
            inputs={{ name: model?.name ?? "" }}
          />
        </p>
        <p className={MUTED}>{m["admin_models.delete.spend_note"]()}</p>
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
  const choices = (providers.data?.items ?? []).filter(
    // Search backends are not catalogue importers: their grant anchor is
    // created with the provider, and a tier is a body field (ADR 0071). They
    // have their own screen. Internal rows are the deployment's own plumbing
    // (the extractor's markitdown row): a fixed builtin catalogue, nothing to
    // adopt from and nothing a hand-made model could route to — the model
    // list hides those rows for the same reason.
    (provider) => provider.is_active && provider.kind !== "search" && provider.kind !== "internal",
  );

  return (
    <Dialog
      open={open}
      title={m["admin_models.create.title"]()}
      onClose={onClose}
      footer={
        <>
          <Button onClick={onClose}>{m["admin_models.create.cancel"]()}</Button>
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
                    toast?.add({ title: m["admin_models.create.toast_done"](), type: "success" });
                    setName("");
                    setUpstream("");
                    setInputs([]);
                    setOutputs([]);
                    setFeatures([]);
                    setGeneration((current) => current + 1);
                    onClose();
                  },
                  onError: () =>
                    toast?.add({
                      title: m["admin_models.create.toast_failed"](),
                      type: "error",
                    }),
                },
              )
            }
          >
            {m["admin_models.create.submit"]()}
          </Button>
        </>
      }
    >
      <div className={FORM}>
        {create.error ? (
          <Notice tone="danger">
            {/* i18n: server text — the API's own error message. */}
            {create.error instanceof Error ? create.error.message : m["admin_models.unknown_error"]()}
          </Notice>
        ) : null}

        {/* First question first, the same order discovery asks in: a model is
            routed through one endpoint (ADR 0027), so nothing after this —
            not the name's shape, not what the upstream id will even look
            like — is answerable before it. Sitting last, below fields the
            answer constrains, it read as an afterthought rather than the
            decision the rest hangs on. */}
        <Select
          label={m["admin_models.create.provider"]()}
          value={providerId}
          onChange={(e) => setProviderId(e.target.value)}
        >
          <option value="">{m["admin_models.create.choose_provider"]()}</option>
          {choices.map((provider) => (
            <option key={provider.id} value={provider.id}>
              {provider.name} — {provider.base_url}
            </option>
          ))}
        </Select>
        {choices.length === 0 && !providers.isPending && (
          <Notice tone="warn">{m["admin_models.create.no_provider"]()}</Notice>
        )}

        <Input
          label={m["admin_models.create.name"]()}
          value={name}
          onChange={(e) => setName(e.target.value)}
          placeholder={m["admin_models.create.name_placeholder"]()}
          hint={m["admin_models.create.name_hint"]()}
        />
        <Input
          label={m["admin_models.create.upstream"]()}
          value={upstream}
          onChange={(e) => setUpstream(e.target.value)}
          placeholder={m["admin_models.create.upstream_placeholder"]()}
          hint={m["admin_models.create.upstream_hint"]()}
        />

        <Select
          label={m["admin_models.create.kind"]()}
          value={kind}
          onChange={(e) => setKind(e.target.value as ModelKind)}
          hint={m["admin_models.create.kind_hint"]()}
        >
          <option value="chat">{m["admin_models.kind.chat"]()}</option>
          <option value="embedding">{m["admin_models.kind.embedding"]()}</option>
          <option value="image">{m["admin_models.kind.image"]()}</option>
          <option value="decision">{m["admin_models.kind.decision"]()}</option>
        </Select>

        {/* Describing it now rather than importing-then-editing. Left empty this
            reads "not stated", which is honest for a model nobody has described. */}
        <CapabilityPicker
          key={`inputs-${generation}`}
          label={m["admin_models.create.accepts"]()}
          otherLabel={m["admin_models.create.accepts_other"]()}
          hint={m["admin_models.create.accepts_hint"]()}
          known={KNOWN_INPUTS}
          value={inputs}
          onChange={setInputs}
        />
        <CapabilityPicker
          key={`outputs-${generation}`}
          label={m["admin_models.create.produces"]()}
          otherLabel={m["admin_models.create.produces_other"]()}
          hint={m["admin_models.create.produces_hint"]()}
          known={KNOWN_OUTPUTS}
          value={outputs}
          onChange={setOutputs}
        />
        <CapabilityPicker
          key={`features-${generation}`}
          label={m["admin_models.create.features"]()}
          otherLabel={m["admin_models.create.features_other"]()}
          hint={m["admin_models.create.features_hint"]()}
          known={KNOWN_FEATURES}
          value={features}
          onChange={setFeatures}
        />

        {/* Both are deliberate: a new model is invisible until someone chooses to
            expose it, and an unpriced model would record a cost of zero. */}
        <Notice tone="info">{m["admin_models.create.starting_note"]()}</Notice>
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

// -- the discovery dialog, and the hand-typed price a row can carry ----------
// A provider that publishes no price for a model does not make the model
// unimportable; it makes the price the operator's to supply. These pieces hold
// that price while it is typed: one draft per row, judged by shape, sent as
// the string that was typed.

/** One row's hand-typed price, while it is being typed. Strings throughout:
 * what the operator typed is what the import sends, and the gateway stores. */
interface ManualPriceDraft {
  input: string;
  output: string;
  perPage: string;
  cacheRead: string;
  cacheWrite: string;
  currency: string;
}

const EMPTY_DRAFT: ManualPriceDraft = {
  input: "",
  output: "",
  perPage: "",
  cacheRead: "",
  cacheWrite: "",
  currency: "",
};

/** Kinds whose price row can carry cache rates here: the token-priced ones whose
 * prompt a provider may serve from a cache. The gateway takes them on any kind,
 * as the model page's price form does; an OCR row is priced per page and has no
 * cached slice to price. Embedding is asked too (owner, 2026-10-18), although
 * the providers seen so far report no cached embedding tokens. */
const CACHE_KINDS = new Set(["chat", "embedding"]);
const takesCacheRates = (row: DiscoveredModel) => CACHE_KINDS.has(row.kind);

/**
 * The rates a kind is metered on, mirrored from the gateway's `_KIND_RATES` so
 * a row asks for what the import will demand and no more: a chat model bills
 * output tokens too, an embedding model generates none, an OCR model charges
 * by the page, and a search tier is metered per request — never priced per
 * token at all.
 */
const RATE_FIELDS: Record<string, ("input" | "output" | "perPage")[]> = {
  chat: ["input", "output"],
  embedding: ["input"],
  image: ["input"],
  ocr: ["perPage"],
  decision: ["input", "output"],
};

const RATE_LABELS: Record<string, () => string> = {
  input: m["admin_models.discover.manual_rate.input"],
  output: m["admin_models.discover.manual_rate.output"],
  perPage: m["admin_models.discover.manual_rate.per_page"],
};

/** Money is a string end to end, so a draft is judged by shape and never by
 * parsing it into a float: digits and one decimal point, nothing else. */
const AMOUNT_PATTERN = /^\d+(\.\d+)?$/;
const CURRENCY_PATTERN = /^[A-Za-z]{3}$/;

function rateError(value: string): string | null {
  if (value === "") return m["admin_models.discover.manual_rate.required"]();
  return AMOUNT_PATTERN.test(value) ? null : m["admin_models.discover.manual_rate.amount_invalid"]();
}

/** A cache rate is optional: empty is fine and sends nothing; anything typed is
 * judged by shape like the other rates. */
function optionalRateError(value: string): string | null {
  if (value === "") return null;
  return AMOUNT_PATTERN.test(value) ? null : m["admin_models.discover.manual_rate.amount_invalid"]();
}

function currencyError(value: string): string | null {
  if (value === "") return null;
  return CURRENCY_PATTERN.test(value)
    ? null
    : m["admin_models.discover.manual_rate.currency_invalid"]();
}

/** The payload for a hand-typed price, or null while it is incomplete — the
 * Import button is held until every ticked unpriced row has one. The currency
 * is omitted when empty, which the gateway reads as its billing currency:
 * the same default the model page's price form sends, and the only one the
 * console can offer, because the deployment's currency is not readable here. */
function manualPricePayload(
  row: DiscoveredModel,
  draft: ManualPriceDraft | undefined,
): ManualImportPrice | undefined {
  if (!draft) return undefined;
  const fields = RATE_FIELDS[row.kind] ?? ["input"];
  if (fields.some((field) => draft[field] === "")) return undefined;
  if (currencyError(draft.currency) !== null) return undefined;
  if (takesCacheRates(row) && (optionalRateError(draft.cacheRead) || optionalRateError(draft.cacheWrite)))
    return undefined;
  const price: ManualImportPrice = {};
  if (fields.includes("input")) price.input_per_mtok = draft.input;
  if (fields.includes("output")) price.output_per_mtok = draft.output;
  if (fields.includes("perPage")) price.per_page = draft.perPage;
  if (takesCacheRates(row)) {
    if (draft.cacheRead !== "") price.cache_read_per_mtok = draft.cacheRead;
    if (draft.cacheWrite !== "") price.cache_write_per_mtok = draft.cacheWrite;
  }
  if (draft.currency !== "") price.currency = draft.currency.toUpperCase();
  return price;
}

/** Why a row cannot be imported yet, in words, or null when it can. */
function manualPriceProblem(
  row: DiscoveredModel,
  draft: ManualPriceDraft | undefined,
): string | null {
  if (!draft) return m["admin_models.discover.manual_rate.enter_price"]();
  const fields = RATE_FIELDS[row.kind] ?? ["input"];
  for (const field of fields) {
    const error = rateError(draft[field]);
    if (error) {
      return m["admin_models.discover.manual_rate.field_problem"]({
        label: RATE_LABELS[field] ?? field,
        error,
      });
    }
  }
  if (takesCacheRates(row)) {
    for (const [label, value] of [
      [m["admin_models.discover.manual_rate.cache_read"], draft.cacheRead],
      [m["admin_models.discover.manual_rate.cache_write"], draft.cacheWrite],
    ] as const) {
      const error = optionalRateError(value);
      if (error) {
        return m["admin_models.discover.manual_rate.field_problem"]({ label, error });
      }
    }
  }
  const currency = currencyError(draft.currency);
  return currency
    ? m["admin_models.discover.manual_rate.currency_problem"]({ error: currency })
    : null;
}

/**
 * The price fields for one unpriced row, shown under its name once the row is
 * ticked. They sit in the row rather than in a second dialog because the price
 * is part of the decision the checkbox asks for, and stacked full-width so a
 * phone — where this dialog is used — gets one field per line instead of the
 * narrow column the reason text used to wrap into.
 */
function ManualPriceFields({
  row,
  draft,
  onChange,
}: {
  row: DiscoveredModel;
  draft: ManualPriceDraft;
  onChange: (next: ManualPriceDraft) => void;
}) {
  const fields = RATE_FIELDS[row.kind] ?? ["input"];
  return (
    <div className="mt-2 flex w-full flex-col gap-2">
      {fields.map((field) => (
        <Input
          key={field}
          label={RATE_LABELS[field]?.() ?? field}
          type="text"
          inputMode="decimal"
          autoComplete="off"
          value={draft[field]}
          onChange={(event) => onChange({ ...draft, [field]: event.target.value })}
          error={rateError(draft[field]) ?? undefined}
          hint={m["admin_models.discover.manual_rate.rate_hint"]()}
        />
      ))}
      {takesCacheRates(row) && (
        <>
          <Input
            label={m["admin_models.discover.manual_rate.cache_read"]()}
            type="text"
            inputMode="decimal"
            autoComplete="off"
            value={draft.cacheRead}
            onChange={(event) => onChange({ ...draft, cacheRead: event.target.value })}
            error={optionalRateError(draft.cacheRead) ?? undefined}
            hint={m["admin_models.discover.manual_rate.cache_hint"]()}
          />
          <Input
            label={m["admin_models.discover.manual_rate.cache_write"]()}
            type="text"
            inputMode="decimal"
            autoComplete="off"
            value={draft.cacheWrite}
            onChange={(event) => onChange({ ...draft, cacheWrite: event.target.value })}
            error={optionalRateError(draft.cacheWrite) ?? undefined}
          />
        </>
      )}
      <Input
        label={m["admin_models.discover.manual_rate.currency"]()}
        type="text"
        autoComplete="off"
        value={draft.currency}
        onChange={(event) => onChange({ ...draft, currency: event.target.value })}
        error={currencyError(draft.currency) ?? undefined}
        hint={m["admin_models.discover.manual_rate.currency_hint"]()}
      />
    </div>
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
  // The words the dropdown offers, read live from the provider when its plugin
  // knows how to ask for the whole catalogue (`supported`); free text stays
  // for the providers that do not.
  const tags = useCatalogueTags(open && providerId ? providerId : null);
  const importModels = useImportModels();
  const toast = useOptionalToast();
  const [selected, setSelected] = useState<string[]>([]);
  // The price typed on an unpriced row, keyed by upstream id, reset whenever
  // the catalogue under review changes — a draft carried across providers (or
  // across a fill tick that changes which rows need one) would be a price
  // applied to a row nobody reviewed it against.
  const [manualPrices, setManualPrices] = useState<Record<string, ManualPriceDraft>>({});
  const forgetManualPrices = () => setManualPrices({});

  const choices = (providers.data?.items ?? []).filter(
    // Search backends are not catalogue importers: their grant anchor is
    // created with the provider, and a tier is a body field (ADR 0071). They
    // have their own screen. Internal rows are the deployment's own plumbing,
    // not endpoints to adopt a catalogue from.
    (provider) => provider.is_active && provider.kind !== "search" && provider.kind !== "internal",
  );

  // A row the provider listed and nobody prices. The only reason discovery
  // ever blocks a row is that missing price, and that is now supplied here —
  // so such a row is tickable, with its price fields on the row itself.
  // Search tiers stay out: their meter counts requests, so no price belongs on
  // one, whatever the row says. Anything else the server refuses is its call,
  // reported per item.
  const needsManualPrice = (row: DiscoveredModel) =>
    row.kind !== "search" && row.price_source === null && row.blocked_reason !== null;

  const toggle = (id: string) =>
    setSelected((current) =>
      current.includes(id) ? current.filter((entry) => entry !== id) : [...current, id],
    );

  const rowsById = new Map(
    (discovery.data?.available ?? []).map((row) => [row.upstream_model, row]),
  );
  const selectedRows = selected
    .map((id) => rowsById.get(id))
    .filter((row) => row !== undefined);
  // Every ticked unpriced row must carry a complete price before the import
  // can go: the server would refuse it per item, and a button that goes
  // nowhere is the honest version of that refusal.
  const manualBlocked = selectedRows.some(
    (row) => needsManualPrice(row) && manualPriceProblem(row, manualPrices[row.upstream_model]) !== null,
  );
  // The first ticked unpriced row whose price is complete — the source for
  // "apply this price to all ticked", for a provider with many models at one
  // rate. Only offered when there is something to copy onto.
  const priceSourceRow = selectedRows.find(
    (row) =>
      needsManualPrice(row) &&
      manualPriceProblem(row, manualPrices[row.upstream_model]) === null &&
      manualPrices[row.upstream_model] !== undefined,
  );
  const copyableCount = selectedRows.filter(needsManualPrice).length;

  const applyPriceToAll = () => {
    if (!priceSourceRow) return;
    const source = manualPrices[priceSourceRow.upstream_model];
    if (!source) return;
    setManualPrices((current) => {
      const next: Record<string, ManualPriceDraft> = { ...current };
      for (const row of selectedRows) {
        if (needsManualPrice(row)) next[row.upstream_model] = source;
      }
      return next;
    });
  };

  const columns: Column<DiscoveredModel>[] = [
    {
      key: "pick",
      header: "",
      render: (row) => (
        <input
          type="checkbox"
          checked={selected.includes(row.upstream_model)}
          disabled={row.blocked_reason !== null && !needsManualPrice(row)}
          aria-label={m["admin_models.discover.import_aria"]({ name: row.suggested_name })}
          onChange={() => toggle(row.upstream_model)}
        />
      ),
    },
    {
      key: "model",
      header: m["admin_models.discover.column.offered"](),
      render: (row) => (
        <>
          <div>{row.suggested_name}</div>
          <div className={`${MUTED} ${CODE}`}>{row.upstream_model}</div>
          {row.blocked_reason && (
            <div className={MUTED}>
              {/* Not "cannot import": a price typed below the name is what
                  makes the row importable, and the reason beside it says so. */}
              <Badge tone="warn">
                {m["admin_models.discover.badge.no_price_yet"]()}
              </Badge>{" "}
              {/* i18n: server text — the provider's own reason. */}
              {row.blocked_reason}
            </div>
          )}
          {selected.includes(row.upstream_model) && needsManualPrice(row) && (
            <ManualPriceFields
              row={row}
              draft={manualPrices[row.upstream_model] ?? EMPTY_DRAFT}
              onChange={(next) =>
                setManualPrices((current) => ({ ...current, [row.upstream_model]: next }))
              }
            />
          )}
        </>
      ),
    },
    {
      key: "capabilities",
      header: m["admin_models.column.capabilities"](),
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
          <span className={MUTED}>{m["capabilities.not_stated"]()}</span>
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
      header: m["admin_models.discover.column.context"](),
      numeric: true,
      hideBelow: "sm",
      render: (row) =>
        row.context_window ? (
          formatNumber(row.context_window)
        ) : (
          <span className={MUTED}>—</span>
        ),
    },
    {
      key: "price",
      header: m["admin_models.discover.column.price"](),
      numeric: true,
      render: (row) =>
        row.kind === "search" ? (
          // A search tier is metered per request, never per token — there is
          // no rate to show, and a dash would read as "unknown".
          <span className={MUTED}>{m["admin_models.discover.per_request"]()}</span>
        ) : row.input_per_mtok && row.currency ? (
          <>
            {m["admin_models.discover.price_pair"]({
              rin: formatMoney(row.input_per_mtok, row.currency, { exact }),
              rout: formatMoney(row.output_per_mtok ?? "0", row.currency, { exact }),
            })}
            {/* Only the community figures are marked. A badge on every row
                would be noise; the question is which of these prices a third
                party supplied, and that is the answer to it. */}
            {/* i18n: server text — the price source is the API's enum value. */}
            {row.price_source === "community" && (
              <span className="ml-2 align-middle">
                <Badge>community</Badge> {/* i18n: server text — the API's enum value. */}
              </span>
            )}
          </>
        ) : (
          <span className={MUTED}>—</span>
        ),
    },
  ];

  const searchColumns: Column<DiscoveredModel>[] = columns.filter(
    (column) => column.key !== "context",
  );

  return (
    <Dialog
      open={open}
      title={m["admin_models.discover.title"]()}
      onClose={onClose}
      footer={
        <>
          <Button onClick={onClose}>{m["admin_models.discover.close"]()}</Button>
          <Button
            variant="primary"
            busy={importModels.isPending}
            disabled={selected.length === 0 || !providerId || manualBlocked}
            onClick={() =>
              importModels.mutate(
                {
                  providerId,
                  models: selected.map((upstream_model) => {
                    const row = rowsById.get(upstream_model);
                    const manual_price =
                      row && needsManualPrice(row)
                        ? manualPricePayload(row, manualPrices[upstream_model])
                        : undefined;
                    return manual_price ? { upstream_model, manual_price } : { upstream_model };
                  }),
                  fillMissingPrices: fillMissing,
                  tag,
                },
                {
                  onSuccess: () => {
                    toast?.add({
                      title: m["admin_models.discover.toast_done"](),
                      type: "success",
                    });
                    setSelected([]);
                    forgetManualPrices();
                  },
                  onError: () =>
                    toast?.add({
                      title: m["admin_models.discover.toast_failed"](),
                      type: "error",
                    }),
                },
              )
            }
          >
            {m["admin_models.discover.import"]({ count: selected.length || "" })}
          </Button>
        </>
      }
    >
      <div className={FORM}>
        <Select
          label={m["admin_models.discover.provider"]()}
          value={providerId}
          onChange={(event) => {
            setProviderId(event.target.value);
            // A tag is the counterparty's vocabulary — the one that meant
            // something to the endpoint just left. Carrying it here would ask
            // this provider for a slice it may not have, and read as an
            // empty catalogue rather than the mismatch it is.
            setTag("");
            setSelected([]);
            forgetManualPrices();
          }}
        >
          <option value="">{m["admin_models.discover.choose_provider"]()}</option>
          {choices.map((provider) => (
            <option key={provider.id} value={provider.id}>
              {provider.name}
            </option>
          ))}
        </Select>

        {tags.data?.supported ? (
          <Select
            label={m["admin_models.discover.tag"]()}
            value={tag}
            onChange={(event) => {
              setTag(event.target.value);
              setSelected([]);
              forgetManualPrices();
            }}
            hint={m["admin_models.discover.tag_hint_live"]()}
          >
            {/* The empty choice is not "no tag": it is the provider's own
                default slice, which for Cortecs is Instruct without saying
                so. Naming it "default" rather than "none" kept the dropdown
                honest about what an untagged fetch actually returns. */}
            <option value="">{m["admin_models.discover.tag_default"]()}</option>
            {tags.data.tags.map((choice) => (
              <option key={choice} value={choice}>
                {choice}
              </option>
            ))}
          </Select>
        ) : (
          <Input
            label={m["admin_models.discover.tag"]()}
            value={tag}
            onChange={(event) => {
              setTag(event.target.value);
              setSelected([]);
              forgetManualPrices();
            }}
            placeholder={m["admin_models.discover.tag_placeholder"]()}
            hint={m["admin_models.discover.tag_hint_free"]()}
          />
        )}

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
              forgetManualPrices();
            }}
          />
          <span>
            {m["admin_models.discover.fill_missing"]()}
            <span className={`${MUTED} ml-1 text-sm`}>
              {m["admin_models.discover.community_gloss"]()}
            </span>
          </span>
        </label>

        {providerId && discovery.isPending && (
          <Spinner label={m["admin_models.discover.asking"]()} />
        )}
        {discovery.error ? (
          <Notice tone="danger" title={m["admin_models.discover.load_error.title"]()}>
            {/* i18n: server text — the API's own error message. */}
            {discovery.error instanceof Error
              ? discovery.error.message
              : m["admin_models.unknown_error"]()}
          </Notice>
        ) : null}

            {importModels.data && (
              <Notice tone="info" title={m["admin_models.discover.import_finished"]()}>
                {importModels.data.results.map((result) => (
                  <div key={result.upstream_model}>
                    {result.imported
                      ? result.priced
                        ? result.price_source === "community"
                          ? m["admin_models.discover.result_imported_community"]({
                              // `name` can be null when the provider offered a
                              // model with no name to suggest; the string it
                              // would have interpolated is what the message gets.
                              name: String(result.name),
                            })
                          : result.price_source === "manual"
                            ? m["admin_models.discover.result_imported_manual"]({
                                name: String(result.name),
                              })
                            : m["admin_models.discover.result_imported_priced"]({
                                name: String(result.name),
                              })
                        : m["admin_models.discover.result_imported_unpriced"]({
                            name: String(result.name),
                          })
                      : // i18n: server text — the skip reason is the provider's.
                        m["admin_models.discover.result_skipped"]({
                          name: result.upstream_model,
                          reason: String(result.reason),
                        })}
                    {/* i18n: server text — a manual price the catalogue or the
                        community file outranked is the API's own sentence. A
                        skipped row's reason is already in the message above. */}
                    {result.imported && result.reason ? ` — ${result.reason}` : ""}
                  </div>
                ))}
              </Notice>
            )}

        {discovery.data && (
          <>
            {/* The dangerous direction of drift: still served, no longer offered.
                These fail only when someone calls them. */}
            {discovery.data.missing_upstream.length > 0 && (
              <Notice tone="warn" title={m["admin_models.discover.missing_title"]()}>
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
                    {!row.is_active && <Badge>{m["admin_models.discover.badge.inactive"]()}</Badge>}
                    <span className="sr-only">{m["admin_models.discover.missing_sr"]()}</span>
                  </div>
                ))}
              </Notice>
            )}

            {/* Two sections, not one list: a search tier and a chat model are
                different things that answer the same prompt differently — one
                is metered per request with no rate, the other priced per
                million tokens. Mixing them made the search tiers look like
                unpriced models nobody could import. The kind tag on each
                discovered row is what splits them. */}
            {(() => {
              const chatRows = discovery.data.available.filter((row) => row.kind !== "search");
              const searchRows = discovery.data.available.filter((row) => row.kind === "search");
              return (
                <>
                  {priceSourceRow && copyableCount > 1 && (
                    <div className="flex items-baseline gap-2">
                      {/* A provider with many models at one rate would
                          otherwise mean retyping the same figures per row. The
                          copy overwrites the other ticked rows' drafts, which
                          is what "apply" says; each row's own validation still
                          holds before the import can go. */}
                      <Button onClick={applyPriceToAll}>
                        {m["admin_models.discover.apply_to_all"]()}
                      </Button>
                    </div>
                  )}
                  <Table
                    columns={columns}
                    rows={chatRows}
                    rowKey={(row) => row.upstream_model}
                    empty={m["admin_models.discover.empty"]()}
                    caption={m["admin_models.discover.caption_models"]({ count: chatRows.length })}
                  />
                  {searchRows.length > 0 && (
                    <div className="mt-4">
                      <h4 className="mb-1 text-sm font-semibold">
                        {m["admin_models.discover.search_tiers"]()}
                      </h4>
                      <p className={`${MUTED} mb-2 text-sm`}>
                        <Message
                          message={m["admin_models.discover.search_tiers_body"]}
                          markup={{ code: (children) => <code className={CODE}>{children}</code> }}
                        />
                      </p>
                      <Table
                        columns={searchColumns}
                        rows={searchRows}
                        rowKey={(row) => row.upstream_model}
                        caption={m["admin_models.discover.caption_tiers"]({
                          count: searchRows.length,
                        })}
                      />
                    </div>
                  )}
                </>
              );
            })()}
          </>
        )}
      </div>
    </Dialog>
  );
}
