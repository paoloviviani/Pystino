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
  SummaryStrip,
  Table,
  
} from "@llmp/ui";
import type { Column } from "@llmp/ui";
import { formatMoney, useExactMoney } from "@llmp/ui";
import type { ReactNode } from "react";
import { useState } from "react";
import { Link, useNavigate, useParams } from "react-router";
import {
  CapabilityPicker,
  KNOWN_FEATURES,
  KNOWN_INPUTS,
  KNOWN_OUTPUTS,
} from "../components/CapabilityPicker";
import { ModelAccess } from "../components/ModelAccess";
import { PageHeader } from "../components/PageHeader";
import { formatDateTime, formatNumber } from "../lib/format";
import { Message } from "../components/Message";
import { m } from "../paraglide/messages.js";
import {
  useCreatePrice,
  useDeleteModel,
  useModel,
  usePrices,
  useUpdateModel,
} from "../lib/admin";
import {
  CHIPS,
  CODE,
  DETAIL_LABEL,
  DETAIL_VALUE,
  DETAILS,
  FORM,
  FORM_ROW,
  MUTED,
  PAGE,
} from "../lib/layout";
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
        <Spinner label={m["model_detail.loading"]()} />
      </div>
    );
  }

  if (model.error || !model.data) {
    return (
      <div className={PAGE}>
        <Notice tone="danger" title={m["model_detail.load_error.title"]()}>
          {/* i18n: server text — the API's own error message. */}
          {model.error instanceof Error ? model.error.message : m["model_detail.unknown_error"]()}{" "}
          <Link to="/admin/models">{m["model_detail.back_link"]()}</Link>
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
  // The name callers send is the identity of the model — a request carries it,
  // every report is grouped by it — so it is shown, never edited here. What an
  // operator names freely is the display name, which is what a picker shows.
  const [displayName, setDisplayName] = useState(model.display_name ?? "");
  const displayNameSaved = (displayName.trim() || null) === (model.display_name ?? null);
  // Always an array: the gateway sends `[]` on the listing as well as the
  // detail, so this cannot be undefined on a deployment whose gateway is behind
  // and a `.length` here would crash the page rather than hide a section.
  const pointingHere = model.aliases_pointing_here ?? [];

  function saveDisplayName(value: string | null) {
    update.mutate(
      { id: model.id, display_name: value },
      {
        onSuccess: () =>
          toast?.add({ title: m["model_detail.toast.name_saved"](), type: "success" }),
        onError: () =>
          toast?.add({ title: m["model_detail.toast.name_failed"](), type: "error" }),
      },
    );
  }

  return (
    <div className={PAGE}>
      <PageHeader
        title={model.name}
        subtitle={
          <Message
            message={m["model_detail.subtitle"]}
            markup={{
              link: (children) => <Link to="/admin/models">{children}</Link>,
              code: (children) => <code className={CODE}>{children}</code>,
            }}
            inputs={{
              // i18n: server text — the model's own names, the provider's own name.
              upstream: model.upstream_model,
              provider: model.provider_name,
            }}
          />
        }
        actions={
          <>
            <Button
              busy={update.isPending}
              onClick={() =>
                update.mutate(
                  { id: model.id, is_active: !model.is_active },
                  {
                    onSuccess: () =>
                      toast?.add({ title: m["model_detail.toast.updated"](), type: "success" }),
                    onError: () =>
                      toast?.add({
                        title: m["model_detail.toast.update_failed"](),
                        type: "error",
                      }),
                  },
                )
              }
            >
              {model.is_active
                ? m["model_detail.action.deactivate"]()
                : m["model_detail.action.activate"]()}
            </Button>
            <Button variant="ghost" onClick={() => setConfirming(true)}>
              {m["model_detail.action.delete"]()}
            </Button>
          </>
        }
      />

      {update.error ? (
        <Notice tone="danger" title={m["model_detail.toast.update_failed"]()}>
          {/* i18n: server text — the API's own error message. */}
          {update.error instanceof Error ? update.error.message : m["model_detail.unknown_error"]()}
        </Notice>
      ) : null}

      <Dialog
        open={confirming}
        title={m["model_detail.delete.title"]({ name: model.name })}
        onClose={() => setConfirming(false)}
        footer={
          <>
            <Button onClick={() => setConfirming(false)}>
              {m["model_detail.delete.cancel"]()}
            </Button>
            <Button
              variant="danger"
              busy={del.isPending}
              onClick={() =>
                del.mutate(model.id, {
                  onSuccess: () => {
                    toast?.add({ title: m["model_detail.toast.deleted"](), type: "success" });
                    navigate("/admin/models");
                  },
                  onError: () =>
                    toast?.add({
                      title: m["model_detail.toast.delete_failed"](),
                      type: "error",
                    }),
                })
              }
            >
              {m["model_detail.delete.confirm"]()}
            </Button>
          </>
        }
      >
        {del.error ? (
          <Notice tone="danger">
            {/* i18n: server text — the API's own error message. */}
            {del.error instanceof Error ? del.error.message : m["model_detail.unknown_error"]()}
          </Notice>
        ) : null}
        <p>
          <Message
            message={m["model_detail.delete.body"]}
            markup={{ code: (children) => <code className={CODE}>{children}</code> }}
          />
        </p>
        <p className={MUTED}>{m["model_detail.delete.spend_note"]()}</p>
        {/* What is about to stop working, by name. Not a warning about the
            aliases themselves — they survive, with their grants, and read as
            unavailable; this is the list of names that will start answering 503,
            because "delete this model" is not a complete statement of what it
            costs and this is where it is completed (ADR 0101, decision 8). */}
        {pointingHere.length > 0 ? (
          <Notice tone="warn">
            <p>
              {pointingHere.length === 1
                ? m["model_detail.aliases.affected_one"]()
                : m["model_detail.aliases.affected_many"]({
                    count: formatNumber(pointingHere.length),
                  })}
            </p>
            <ul>
              {pointingHere.map((alias) => (
                <li key={alias.id}>
                  <code className={CODE}>{alias.name}</code>
                </li>
              ))}
            </ul>
            <p>{m["model_detail.aliases.affected_note"]()}</p>
          </Notice>
        ) : null}
      </Dialog>

      {/* The two states worth interrupting someone about, and they are not
          symmetrical: an inactive model serves nobody and is obvious the moment
          anyone tries it, while an unpriced one serves everybody and records a
          cost of zero, which nobody notices until the reconciliation. */}
      {/* A price appended now applies from now on; it cannot rewrite what
          earlier requests were charged. */}
      {!model.current_price && (
        <Notice tone="warn" title={m["model_detail.no_price.title"]()}>
          {m["model_detail.no_price.body"]()}
        </Notice>
      )}
      {!model.provider_is_active && (
        <Notice tone="danger" title={m["model_detail.provider_inactive.title"]({ provider: model.provider_name })}>
          {m["model_detail.provider_inactive.body"]()}
        </Notice>
      )}

      <Card title={m["model_detail.details_card"]()}>
        <dl className={DETAILS}>
          <Detail label={m["model_detail.detail.status"]()}>
            {model.is_active ? (
              <Badge tone="ok">{m["model_detail.badge.active"]()}</Badge>
            ) : (
              <Badge>{m["model_detail.badge.inactive"]()}</Badge>
            )}
          </Detail>
          <Detail label={m["model_detail.detail.visibility"]()}>
            <div className={CHIPS}>
              {model.is_public ? (
                <Badge tone="accent">{m["model_detail.badge.public"]()}</Badge>
              ) : (
                <Badge>{m["model_detail.badge.private"]()}</Badge>
              )}
              <Button
                busy={update.isPending && update.variables?.is_public !== undefined}
                onClick={() =>
                  update.mutate(
                    { id: model.id, is_public: !model.is_public },
                    {
                      onSuccess: () =>
                        toast?.add({ title: m["model_detail.toast.updated"](), type: "success" }),
                      onError: () =>
                        toast?.add({
                          title: m["model_detail.toast.update_failed"](),
                          type: "error",
                        }),
                    },
                  )
                }
              >
                {model.is_public
                  ? m["model_detail.action.restrict"]()
                  : m["model_detail.action.make_public"]()}
              </Button>
            </div>
          </Detail>
          <Detail label={m["model_detail.detail.callers_name"]()}>
            <code className={CODE}>{model.name}</code>
          </Detail>
          <Detail label={m["model_detail.detail.display_name"]()}>
            <div className="flex flex-wrap items-center gap-2">
              <div className="min-w-48 flex-1">
                <Input
                  label={m["model_detail.detail.display_name"]()}
                  value={displayName}
                  placeholder={model.name}
                  onChange={(event) => setDisplayName(event.target.value)}
                  hint={m["model_detail.display_name_hint"]()}
                />
              </div>
              <Button
                busy={update.isPending}
                disabled={displayNameSaved}
                onClick={() => saveDisplayName(displayName.trim() || null)}
              >
                {m["model_detail.action.save_display_name"]()}
              </Button>
              {model.display_name && (
                <Button
                  variant="ghost"
                  onClick={() => {
                    setDisplayName("");
                    saveDisplayName(null);
                  }}
                >
                  {m["model_detail.action.clear_display_name"]()}
                </Button>
              )}
            </div>
          </Detail>
          <Detail label={m["model_detail.detail.upstream"]()}>
            <code className={CODE}>{model.upstream_model}</code>
          </Detail>
          <Detail label={m["model_detail.detail.provider"]()}>
            <Link to="/admin/providers">{model.provider_name}</Link>
          </Detail>
        </dl>
      </Card>

      <Describe model={model} />
      <Pricing model={model} />
      <ModelAccess
        id={model.id}
        name={model.name}
        grantedToUsers={model.granted_to_users}
        revokedNote={() => m["model_detail.access.revoke_note"]()}
        cardDescription={() => m["model_detail.access.description"]()}
      />

      {/* The reverse direction over the routing table: which names resolve to
          **this** model. Here because deleting or deactivating it is what
          breaks them, and an administrator should not have to know that an
          alias exists to be told one is about to stop working (ADR 0101). */}
      {pointingHere.length > 0 ? (
        <Card
          title={m["model_detail.aliases.title"]()}
          description={m["model_detail.aliases.description"]()}
        >
          <div className={CHIPS}>
            {pointingHere.map((alias) => (
              <Link key={alias.id} to="/admin/routing" className="underline">
                <code className={CODE}>{alias.name}</code>
              </Link>
            ))}
          </div>
        </Card>
      ) : null}
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
  const [maxInput, setMaxInput] = useState(
    model.max_input_tokens ? String(model.max_input_tokens) : "",
  );
  const [maxOutput, setMaxOutput] = useState(
    model.max_output_tokens ? String(model.max_output_tokens) : "",
  );

  return (
    <Card
      title={m["model_detail.describe.card"]()}
      description={m["model_detail.describe.description"]()}
    >
      <div className={FORM}>
        {update.error ? (
          <Notice tone="danger">
            {/* i18n: server text — the API's own error message. */}
            {update.error instanceof Error ? update.error.message : m["model_detail.unknown_error"]()}
          </Notice>
        ) : null}
        {update.isSuccess && !update.isPending && (
          <Notice tone="info">{m["model_detail.describe.saved"]()}</Notice>
        )}

        <Select
          label={m["model_detail.describe.kind"]()}
          value={kind}
          onChange={(event) => setKind(event.target.value as ModelKind)}
          hint={m["model_detail.describe.kind_hint"]()}
        >
          <option value="chat">{m["model_detail.kind.chat"]()}</option>
          <option value="embedding">{m["model_detail.kind.embedding"]()}</option>
          <option value="image">{m["model_detail.kind.image"]()}</option>
          <option value="transcription">{m["model_detail.kind.transcription"]()}</option>
          <option value="decision">{m["model_detail.kind.decision"]()}</option>
        </Select>

        <CapabilityPicker
          label={m["model_detail.describe.accepts"]()}
          otherLabel={m["model_detail.describe.accepts_other"]()}
          hint={m["model_detail.describe.accepts_hint"]()}
          known={KNOWN_INPUTS}
          value={inputs}
          onChange={(next) => {
            // "Images in tool results" means nothing without image input, so
            // it is dropped with it rather than saved on a model that cannot use it.
            if (!next.includes("image")) {
              setFeatures((current) => current.filter((item) => item !== "tool_images"));
            }
            setInputs(next);
          }}
        />
        <CapabilityPicker
          label={m["model_detail.describe.produces"]()}
          otherLabel={m["model_detail.describe.produces_other"]()}
          hint={m["model_detail.describe.produces_hint"]()}
          known={KNOWN_OUTPUTS}
          value={outputs}
          onChange={setOutputs}
        />
        <CapabilityPicker
          label={m["model_detail.describe.features"]()}
          otherLabel={m["model_detail.describe.features_other"]()}
          hint={m["model_detail.describe.features_hint"]()}
          known={KNOWN_FEATURES}
          value={features}
          onChange={setFeatures}
          inputs={inputs}
        />
        <Input
          label={m["model_detail.describe.context_window"]()}
          type="number"
          min="1"
          value={context}
          onChange={(event) => setContext(event.target.value)}
          hint={m["model_detail.describe.context_hint"]()}
        />
        <Input
          label={m["model_detail.describe.max_input"]()}
          type="number"
          min="1"
          value={maxInput}
          onChange={(event) => setMaxInput(event.target.value)}
          hint={m["model_detail.describe.max_input_hint"]()}
        />
        <Input
          label={m["model_detail.describe.max_output"]()}
          type="number"
          min="1"
          value={maxOutput}
          onChange={(event) => setMaxOutput(event.target.value)}
          hint={m["model_detail.describe.max_output_hint"]()}
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
                  max_input_tokens: maxInput === "" ? null : Number(maxInput),
                  max_output_tokens: maxOutput === "" ? null : Number(maxOutput),
                },
                {
                  onSuccess: () =>
                    toast?.add({ title: m["model_detail.describe.toast_saved"](), type: "success" }),
                  onError: () =>
                    toast?.add({
                      title: m["model_detail.describe.toast_failed"](),
                      type: "error",
                    }),
                },
              )
            }
          >
            {m["model_detail.describe.save"]()}
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
  // A rate is money too, so the strip's current-rate line follows the reader's
  // precision preference — read by hand rather than via <Money> because it
  // sits inside a sentence.
  const exact = useExactMoney();
  const items = prices.data?.items ?? [];
  // Newest first, so the head of the list is what requests are charged now
  // (or scheduled to be, when its date is in the future).
  const current = items[0];

  const columns: Column<Price>[] = [
    {
      key: "effective",
      header: m["model_detail.price.column.effective"](),
      render: (price) => (
        <>
          <div>{formatDateTime(price.effective_from)}</div>
          {new Date(price.effective_from) > now && (
            <Badge tone="accent">{m["model_detail.price.scheduled"]()}</Badge>
          )}
        </>
      ),
    },
    {
      key: "input",
      header: m["model_detail.price.column.input"](),
      numeric: true,
      render: (price) => <Money amount={price.input_per_mtok} currency={price.currency} />,
    },
    {
      key: "output",
      header: m["model_detail.price.column.output"](),
      numeric: true,
      render: (price) => <Money amount={price.output_per_mtok} currency={price.currency} />,
    },
    // Cache rates and the per-image rate are all optional, and a dash is not the
    // same claim as a zero: "not priced this way" and "free" are different
    // facts, and one of them is a decision somebody made.
    {
      key: "cache",
      header: m["model_detail.price.column.cache"](),
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
      header: m["model_detail.price.column.image"](),
      numeric: true,
      // Both unit rates share a column: a model is priced per image or per
      // page, never both, and two mostly-empty columns on a narrow screen
      // pushed the source badge off the edge. The header names whichever this
      // model uses.
      render: (price) =>
        price.per_image_by_size && Object.keys(price.per_image_by_size).length > 0 ? (
          <span>
            <Rate amount={price.per_image} currency={price.currency} />
            {/* i18n: literal (sizes and amounts, not text) */}
            <span className="block text-dense text-ink-muted">
              {Object.entries(price.per_image_by_size)
                .map(([size, rate]) => `${size} ${rate}`)
                .join(" · ")}
            </span>
          </span>
        ) : (
          <Rate amount={price.per_image} currency={price.currency} />
        ),
      hideBelow: "md",
    },
    {
      key: "page",
      header: m["model_detail.price.column.page"](),
      numeric: true,
      render: (price) => <Rate amount={price.per_page} currency={price.currency} />,
      hideBelow: "md",
    },
    {
      key: "minute",
      header: m["model_detail.price.column.minute"](),
      numeric: true,
      render: (price) => <Rate amount={price.per_minute} currency={price.currency} />,
      hideBelow: "md",
    },
    {
      key: "search",
      header: m["model_detail.price.column.search"](),
      numeric: true,
      render: (price) => <Rate amount={price.per_search} currency={price.currency} />,
      hideBelow: "lg",
    },
    {
      key: "source",
      header: m["model_detail.price.column.source"](),
      // i18n: server text — the price's source is the API's enum value.
      render: (price) => <Badge>{price.source}</Badge>,
    },
  ];

  return (
    <>
      <Card
        title={m["model_detail.price.card"]()}
        flush
        description={m["model_detail.price.description"]()}
      >
        {prices.isPending ? (
          <Spinner />
        ) : prices.error ? (
          <Notice tone="danger" title={m["model_detail.price.load_error.title"]()}>
            {/* i18n: server text — the API's own error message. */}
            {prices.error instanceof Error ? prices.error.message : m["model_detail.unknown_error"]()}
          </Notice>
        ) : (
          <>
            <div className="p-5">
              <SummaryStrip
                headline={m["model_detail.price.count"]({ count: items.length })}
                detail={
                  current
                    ? m["model_detail.price.current"]({
                        rates: m["model_detail.price.current_rates"]({
                          // Money is a string end to end; `formatMoney` reads it
                          // without parsing it, and the message carries both
                          // figures as the parameters they are.
                          rin: formatMoney(current.input_per_mtok, current.currency, { exact }),
                          rout: formatMoney(current.output_per_mtok, current.currency, { exact }),
                        }),
                      })
                    : m["model_detail.price.no_current"]()
                }
                active={items.length > 0}
              />
            </div>
            <Table
              columns={columns}
              rows={items}
              rowKey={(price) => price.id}
              empty={m["model_detail.price.empty"]()}
              caption={m["model_detail.price.caption"]({ name: model.name })}
            />
          </>
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

/**
 * `1024x1024=0.04, 1792x1024=0.08` into `{ "1024x1024": "0.04", ... }`.
 * Empty is `null` (no per-size rates); anything unreadable is `undefined`, which
 * the form shows as an error and refuses to send. Amounts stay strings.
 */
export function parseSizeRates(text: string): Record<string, string> | null | undefined {
  const trimmed = text.trim();
  if (trimmed === "") return null;
  const rates: Record<string, string> = {};
  for (const part of trimmed.split(",")) {
    const match = /^\s*([A-Za-z0-9_-]{1,32})\s*=\s*(\d+(?:\.\d+)?)\s*$/.exec(part);
    const size = match?.[1];
    const rate = match?.[2];
    if (size === undefined || rate === undefined) return undefined;
    rates[size.toLowerCase()] = rate;
  }
  return rates;
}

function AppendPrice({ model }: { model: AdminModel }) {
  const create = useCreatePrice();
  const toast = useOptionalToast();
  const [input, setInput] = useState("");
  const [output, setOutput] = useState("");
  const [cacheRead, setCacheRead] = useState("");
  const [cacheWrite, setCacheWrite] = useState("");
  const [perImage, setPerImage] = useState("");
  const [perSize, setPerSize] = useState("");
  const [perPage, setPerPage] = useState("");
  const [perMinute, setPerMinute] = useState("");
  const [perSearch, setPerSearch] = useState("");
  const [effective, setEffective] = useState("");

  const sizes = parseSizeRates(perSize);

  const submit = () => {
    if (sizes === undefined) return;
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
        per_image_by_size: sizes,
        per_page: perPage === "" ? null : perPage,
        per_minute: perMinute === "" ? null : perMinute,
        per_search: perSearch === "" ? null : perSearch,
        // A datetime-local value carries no zone; converting through Date makes
        // the browser's zone explicit rather than letting the server guess.
        effective_from: effective ? new Date(effective).toISOString() : null,
      },
      {
        onSuccess: () => {
          toast?.add({ title: m["model_detail.append.toast_done"](), type: "success" });
          setInput("");
          setOutput("");
          setCacheRead("");
          setCacheWrite("");
          setPerImage("");
          setPerSize("");
          setPerPage("");
          setPerMinute("");
          setPerSearch("");
          setEffective("");
        },
        onError: () =>
          toast?.add({ title: m["model_detail.append.toast_failed"](), type: "error" }),
      },
    );
  };

  return (
    <Card title={m["model_detail.append.card"]()}>
      <div className={FORM}>
        {create.error ? (
          <Notice tone="danger">
            {/* i18n: server text — the API's own error message. */}
            {create.error instanceof Error ? create.error.message : m["model_detail.unknown_error"]()}
          </Notice>
        ) : null}
        {create.isSuccess && !create.isPending && (
          <Notice tone="info">{m["model_detail.append.saved"]()}</Notice>
        )}

        <div className={FORM_ROW}>
          <Input
            label={m["model_detail.append.input"]()}
            type="number"
            min="0"
            step="0.000001"
            value={input}
            onChange={(event) => setInput(event.target.value)}
            hint={m["model_detail.append.input_hint"]()}
          />
          <Input
            label={m["model_detail.append.output"]()}
            type="number"
            min="0"
            step="0.000001"
            value={output}
            onChange={(event) => setOutput(event.target.value)}
          />
          <Input
            label={m["model_detail.append.cache_read"]()}
            type="number"
            min="0"
            step="0.000001"
            value={cacheRead}
            onChange={(event) => setCacheRead(event.target.value)}
            hint={m["model_detail.append.cache_read_hint"]()}
          />
          <Input
            label={m["model_detail.append.cache_write"]()}
            type="number"
            min="0"
            step="0.000001"
            value={cacheWrite}
            onChange={(event) => setCacheWrite(event.target.value)}
          />
          <Input
            label={m["model_detail.append.per_image"]()}
            type="number"
            min="0"
            step="0.000001"
            value={perImage}
            onChange={(event) => setPerImage(event.target.value)}
            hint={m["model_detail.append.per_image_hint"]()}
          />
          {model.kind === "image" && (
            <Input
              label={m["model_detail.append.per_image_by_size"]()}
              value={perSize}
              placeholder={m["model_detail.append.per_image_by_size_placeholder"]()}
              onChange={(event) => setPerSize(event.target.value)}
              hint={m["model_detail.append.per_image_by_size_hint"]()}
              error={sizes === undefined ? m["model_detail.append.per_image_by_size_invalid"]() : undefined}
            />
          )}
          <Input
            label={m["model_detail.append.per_page"]()}
            type="number"
            min="0"
            step="0.0000001"
            value={perPage}
            onChange={(event) => setPerPage(event.target.value)}
            hint={m["model_detail.append.per_page_hint"]()}
          />
          <Input
            label={m["model_detail.append.per_minute"]()}
            type="number"
            min="0"
            step="0.0000001"
            value={perMinute}
            onChange={(event) => setPerMinute(event.target.value)}
            hint={m["model_detail.append.per_minute_hint"]()}
          />
          <Input
            label={m["model_detail.append.per_search"]()}
            type="number"
            min="0"
            step="0.0000001"
            value={perSearch}
            onChange={(event) => setPerSearch(event.target.value)}
            hint={m["model_detail.append.per_search_hint"]()}
          />
          <Input
            label={m["model_detail.append.effective"]()}
            type="datetime-local"
            value={effective}
            onChange={(event) => setEffective(event.target.value)}
            hint={m["model_detail.append.effective_hint"]()}
          />
        </div>

        <div>
          <Button
            variant="primary"
            busy={create.isPending}
            disabled={input === "" || output === ""}
            onClick={submit}
          >
            {m["model_detail.append.submit"]()}
          </Button>
        </div>
      </div>
    </Card>
  );
}


