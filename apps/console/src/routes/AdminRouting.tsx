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
  RowActions,
} from "@llmp/ui";
import type { Column } from "@llmp/ui";
import { useState } from "react";
import { Link } from "react-router";
import { ModelAccess } from "../components/ModelAccess";
import { PageHeader } from "../components/PageHeader";
import {
  useAliases,
  useCreateAlias,
  useDeleteAlias,
  useModels,
  useUpdateAlias,
} from "../lib/admin";
import { CHIPS, CODE, FORM, FORM_ROW, MUTED, PAGE, ROW_ACTIONS } from "../lib/layout";
import { Message } from "../components/Message";
import { m } from "../paraglide/messages.js";
import type { AdminAlias, AliasTarget, AliasUnavailableReason, ModelKind } from "../lib/types";
import { useOptionalToast } from "../lib/toast";

/**
 * The Routing tab: the aliases an administrator has defined (ADR 0101).
 *
 * Separate from the catalogue on purpose. A model has a provider, an upstream
 * name and a price; an alias has none of those and a target instead, and every
 * column the Models screen renders would be empty or wrong for one. What they
 * *do* share is the mechanism that matters — `is_public` and the group and user
 * grants — so this screen reuses the model page's visibility controls rather
 * than growing a second set that would drift from the first.
 *
 * What an administrator is looking at here is the one thing the feature makes
 * possible that they could not do before: offer a model under terms different
 * from the model's own. That is why the target is the prominent column and the
 * access summary is beside it rather than buried in a page.
 */

/** Message functions, not strings: the table is built once, at import. */
const REASONS: Record<AliasUnavailableReason, { label: () => string; body: () => string }> = {
  target_deleted: {
    label: () => m["admin_routing.badge.target_deleted"](),
    body: () => m["admin_routing.reason.target_deleted"](),
  },
  target_inactive: {
    label: () => m["admin_routing.badge.target_inactive"](),
    body: () => m["admin_routing.reason.target_inactive"](),
  },
  target_provider_inactive: {
    label: () => m["admin_routing.badge.target_provider_inactive"](),
    body: () => m["admin_routing.reason.target_provider_inactive"](),
  },
  target_is_alias: {
    label: () => m["admin_routing.badge.target_is_alias"](),
    body: () => m["admin_routing.reason.target_is_alias"](),
  },
};

/** The rules the gateway offers, as the picker lists them. The gateway
 *  validates the name, so a rule added there needs a line here and nothing
 *  else; one this table does not know renders as its own name. */
const RULES = ["first_available", "weighted", "cheapest"] as const;

const RULE_LABELS: Record<(typeof RULES)[number], () => string> = {
  first_available: () => m["admin_routing.rule.first_available"](),
  weighted: () => m["admin_routing.rule.weighted"](),
  cheapest: () => m["admin_routing.rule.cheapest"](),
};

/**
 * The reason's words, or a generic pair for a code this console does not know
 * yet: a newer gateway may add one, and an unknown code must not take the
 * whole page down (a plain table lookup returned undefined and `.label()`
 * threw, leaving a blank screen).
 */
function reasonOf(code: string): { label: () => string; body: () => string } {
  return (
    (REASONS as Record<string, { label: () => string; body: () => string } | undefined>)[code] ?? {
      label: () => m["admin_routing.badge.unknown_reason"]({ code }),
      body: () => m["admin_routing.reason.unknown_reason"]({ code }),
    }
  );
}

const RULE_HINTS: Record<(typeof RULES)[number], () => string> = {
  first_available: () => m["admin_routing.rule.first_available_hint"](),
  weighted: () => m["admin_routing.rule.weighted_hint"](),
  cheapest: () => m["admin_routing.rule.cheapest_hint"](),
};

function ruleLabel(rule: string): string {
  return (RULE_LABELS as Record<string, (() => string) | undefined>)[rule]?.() ?? rule;
}

/** Whole minutes, which is how an administrator thinks about a session. */
function minutesOf(seconds: number): number {
  return Math.round(seconds / 60);
}

export function AdminRouting() {
  const aliases = useAliases();
  const create = useCreateAlias();
  const update = useUpdateAlias();
  const remove = useDeleteAlias();
  const toast = useOptionalToast();

  const [editing, setEditing] = useState<AdminAlias | null>(null);
  const [creating, setCreating] = useState(false);
  const [deleting, setDeleting] = useState<AdminAlias | null>(null);

  const columns: Column<AdminAlias>[] = [
    {
      key: "name",
      header: m["admin_routing.column.alias"](),
      render: (alias) => (
        <>
          <div>
            {alias.display_name || alias.name}{" "}
            {/* The kind is the target's, so it is read-only on the form. Shown
                here as a badge because "which surfaces serve this" is the first
                question about a name. */}
            <Badge tone="accent">{alias.kind}</Badge>
          </div>
          {alias.display_name && <div className={CODE}>{alias.name}</div>}
          {alias.description && <div className={MUTED}>{alias.description}</div>}
        </>
      ),
    },
    {
      key: "target",
      header: m["admin_routing.column.target"](),
      render: (alias) =>
        alias.targets.length === 0 ? (
          <span className={MUTED}>{m["admin_routing.form.target_none"]()}</span>
        ) : (
          <>
            {/* In the order the rule starts from. Each is a real link to the
                model, because "what is `fast` actually?" is the question this
                column exists to answer and it should be one click. */}
            <ol className="m-0 list-none p-0">
              {alias.targets.map((target) => (
                <li key={target.id} className="flex flex-wrap items-center gap-1">
                  <Link to={`/admin/models/${target.id}`}>{target.name}</Link>
                  {alias.route_rule === "weighted" ? (
                    <span className={MUTED}>
                      {m["admin_routing.row.weight"]({ weight: target.weight })}
                    </span>
                  ) : null}
                  {!target.available && target.unavailable_reason ? (
                    <Badge tone="danger">{reasonOf(target.unavailable_reason).label()}</Badge>
                  ) : null}
                </li>
              ))}
            </ol>
            {alias.targets.length > 1 ? (
              <div className={MUTED}>
                {ruleLabel(alias.route_rule)}
                {" · "}
                {alias.route_sticky_ttl_seconds > 0
                  ? m["admin_routing.row.sticky"]({
                      minutes: minutesOf(alias.route_sticky_ttl_seconds),
                    })
                  : m["admin_routing.row.not_sticky"]()}
                {alias.route_rule === "cheapest" &&
                (alias.route_price_input_weight !== 1 || alias.route_price_output_weight !== 1)
                  ? ` · ${m["admin_routing.row.price_weights"]({
                      input: alias.route_price_input_weight,
                      output: alias.route_price_output_weight,
                    })}`
                  : null}
              </div>
            ) : null}
          </>
        ),
    },
    {
      key: "access",
      header: m["admin_routing.column.access"](),
      hideBelow: "lg",
      render: (alias) =>
        alias.granted_to.length === 0 && alias.granted_to_users.length === 0 ? (
          <span className={MUTED}>{m["admin_routing.access.nobody"]()}</span>
        ) : (
          <div className={CHIPS}>
            {alias.granted_to.map((group) => (
              <Badge key={group}>{group}</Badge>
            ))}
            {alias.granted_to_users.map((user) => (
              <Badge key={user} tone="accent">
                {user}
              </Badge>
            ))}
          </div>
        ),
    },
    {
      key: "status",
      header: m["admin_routing.column.status"](),
      hideBelow: "sm",
      render: (alias) => (
        <div className={CHIPS}>
          {alias.status === "available" ? (
            <Badge tone="ok">{m["admin_routing.badge.available"]()}</Badge>
          ) : (
            // The reason beside the state, never instead of it: "Unavailable"
            // on its own is not something an administrator can act on, which is
            // the whole point of ADR 0101's three named reasons.
            <Badge tone="danger">
              {m["admin_routing.badge.unavailable"]()}
              {alias.unavailable_reason
                ? ` — ${reasonOf(alias.unavailable_reason).label()}`
                : ""}
            </Badge>
          )}
          {alias.is_active ? (
            <Badge tone="ok">{m["admin_routing.badge.active"]()}</Badge>
          ) : (
            <Badge>{m["admin_routing.badge.inactive"]()}</Badge>
          )}
          {alias.is_public && <Badge tone="accent">{m["admin_routing.badge.public"]()}</Badge>}
        </div>
      ),
    },
    {
      key: "actions",
      header: "",
      render: (alias) => (
        <div className={ROW_ACTIONS}>
          <Button variant="secondary" onClick={() => setEditing(alias)}>
            {m["admin_routing.action.edit"]()}
          </Button>
          <RowActions
            label={m["table.row_actions"]({ name: alias.name })}
            actions={[
              {
                key: "toggle",
                label: alias.is_active
                  ? m["admin_routing.action.deactivate"]()
                  : m["admin_routing.action.activate"](),
                onSelect: () =>
                  update.mutate(
                    { id: alias.id, is_active: !alias.is_active },
                    {
                      onSuccess: () =>
                        toast?.add({ title: m["admin_routing.toast.updated"](), type: "success" }),
                      onError: () =>
                        toast?.add({
                          title: m["admin_routing.toast.update_failed"](),
                          type: "error",
                        }),
                    },
                  ),
                disabled: update.isPending && update.variables?.id === alias.id,
              },
              {
                key: "delete",
                label: m["admin_routing.action.delete"](),
                onSelect: () => setDeleting(alias),
                destructive: true,
              },
            ]}
          />
        </div>
      ),
    },
  ];

  const rows = aliases.data ?? [];

  return (
    <div className={PAGE}>
      <PageHeader
        title={m["admin_routing.title"]()}
        subtitle={m["admin_routing.subtitle"]()}
        actions={
          <Button variant="primary" onClick={() => setCreating(true)}>
            {m["admin_routing.add_alias"]()}
          </Button>
        }
      />

      {/* The gateway's own sentences, verbatim. Every one of them names a fix
          an administrator can make, so rewriting them here would only lose
          information. */}
      {create.error ? (
        <Notice tone="danger" title={m["admin_routing.toast.create_failed"]()}>
          {/* i18n: server text — the API's own error message. */}
          {create.error instanceof Error ? create.error.message : m["admin_routing.unknown_error"]()}
        </Notice>
      ) : null}
      {update.error ? (
        <Notice tone="danger" title={m["admin_routing.toast.update_failed"]()}>
          {/* i18n: server text — the API's own error message. */}
          {update.error instanceof Error ? update.error.message : m["admin_routing.unknown_error"]()}
        </Notice>
      ) : null}
      {remove.error ? (
        <Notice tone="danger" title={m["admin_routing.toast.delete_failed"]()}>
          {/* i18n: server text — the API's own error message. */}
          {remove.error instanceof Error ? remove.error.message : m["admin_routing.unknown_error"]()}
        </Notice>
      ) : null}

      {/* The three states, explained once here rather than as a tooltip on each
          badge: an unavailable alias still exists, still has its grants, and is
          still answering requests with a 503. */}
      {rows
        .filter((alias) => alias.status === "unavailable" && alias.unavailable_reason)
        .map((alias) => (
          <Notice key={alias.id} tone="danger" title={`${alias.name} — ${reasonOf(alias.unavailable_reason!).label()}`}>
            {reasonOf(alias.unavailable_reason!).body()}
          </Notice>
        ))}

      <Card flush>
        {aliases.isPending ? (
          <Spinner label={m["admin_routing.loading"]()} />
        ) : aliases.error ? (
          <Notice tone="danger" title={m["admin_routing.load_error.title"]()}>
            {/* i18n: server text — the API's own error message. */}
            {aliases.error instanceof Error
              ? aliases.error.message
              : m["admin_routing.unknown_error"]()}
          </Notice>
        ) : (
          <Table
            columns={columns}
            rows={rows}
            rowKey={(alias) => alias.id}
            empty={m["admin_routing.empty"]()}
            caption={m["admin_routing.caption"]()}
          />
        )}
      </Card>

      {creating || editing ? (
        <AliasDialog
          alias={editing}
          busy={create.isPending || update.isPending}
          onClose={() => {
            setCreating(false);
            setEditing(null);
          }}
          onSubmit={(body) => {
            if (editing) {
              update.mutate(
                { id: editing.id, ...body },
                {
                  onSuccess: () => {
                    toast?.add({
                      title: m["admin_routing.toast.updated"](),
                      type: "success",
                    });
                    setEditing(null);
                  },
                  onError: () =>
                    toast?.add({
                      title: m["admin_routing.toast.update_failed"](),
                      type: "error",
                    }),
                },
              );
            } else {
              create.mutate(body, {
                onSuccess: () => {
                  toast?.add({ title: m["admin_routing.toast.created"](), type: "success" });
                  setCreating(false);
                },
                onError: () =>
                  toast?.add({
                    title: m["admin_routing.toast.create_failed"](),
                    type: "error",
                  }),
              });
            }
          }}
        />
      ) : null}

      <Dialog
        open={deleting !== null}
        onClose={() => setDeleting(null)}
        title={m["admin_routing.delete.title"]({ name: deleting?.name ?? "" })}
        footer={
          <>
            <Button onClick={() => setDeleting(null)}>{m["model_detail.delete.cancel"]()}</Button>
            <Button
              variant="danger"
              busy={remove.isPending}
              onClick={() =>
                deleting &&
                remove.mutate(deleting.id, {
                  onSuccess: () => {
                    toast?.add({ title: m["admin_routing.toast.deleted"](), type: "success" });
                    setDeleting(null);
                  },
                  onError: () =>
                    toast?.add({
                      title: m["admin_routing.toast.delete_failed"](),
                      type: "error",
                    }),
                })
              }
            >
              {m["admin_routing.delete.confirm"]()}
            </Button>
          </>
        }
      >
        <p>
          <Message
            message={m["admin_routing.delete.body"]}
            markup={{ code: (children) => <code className={CODE}>{children}</code> }}
            inputs={{ name: deleting?.name ?? "" }}
          />
        </p>
      </Dialog>
    </div>
  );
}

interface TargetRow {
  /** Stable across re-ordering, so React keeps each row's focus. */
  key: number;
  modelId: string;
  weight: string;
}

/**
 * Create and edit, as one form.
 *
 * The route is a list: each row picks a model and, under the weighted rule, a
 * share. Order is the operator's order (first available starts from the top; a
 * tie under the other rules falls back to it). The kind follows the first
 * target on create and is fixed on edit: the gateway refuses a route whose
 * targets serve different API surfaces (ADR 0101, "Designed for routing"), so
 * the pickers simply do not offer them — a control that could be set to
 * something the server would reject is worse than one that cannot be.
 */
function AliasDialog({
  alias,
  busy,
  onClose,
  onSubmit,
}: {
  alias: AdminAlias | null;
  busy: boolean;
  onClose: () => void;
  onSubmit: (body: {
    name: string;
    display_name: string | null;
    description: string | null;
    targets: { model_id: string; weight: number }[];
    route_rule: string;
    route_sticky_ttl_seconds?: number;
    route_price_input_weight: number;
    route_price_output_weight: number;
    is_public: boolean;
  }) => void;
}) {
  // The catalogue, at the console's maximum page size: an administrator's
  // target is a model they know the name of, and a picker that cannot find it
  // is a picker that cannot be used.
  const models = useModels({ limit: 200 });

  const [name, setName] = useState(alias?.name ?? "");
  const [displayName, setDisplayName] = useState(alias?.display_name ?? "");
  const [description, setDescription] = useState(alias?.description ?? "");
  const [isPublic, setIsPublic] = useState(alias?.is_public ?? false);
  const [rule, setRule] = useState<string>(alias?.route_rule ?? "first_available");
  const initialMinutes = minutesOf(alias?.route_sticky_ttl_seconds ?? 3600);
  const [minutes, setMinutes] = useState(String(initialMinutes));
  const [inputWeight, setInputWeight] = useState(String(alias?.route_price_input_weight ?? 1));
  const [outputWeight, setOutputWeight] = useState(String(alias?.route_price_output_weight ?? 1));
  const [nextKey, setNextKey] = useState(1000);
  const [rows, setRows] = useState<TargetRow[]>(() =>
    alias && alias.targets.length > 0
      ? alias.targets.map((target, index) => ({
          key: index,
          modelId: target.id,
          weight: String(target.weight),
        }))
      : [{ key: 0, modelId: "", weight: "1" }],
  );

  const all = models.data?.items ?? [];
  const candidates = all.filter(
    (model) => model.kind !== "search" && model.provider_kind !== "internal" && model.is_active,
  );
  // An alias being edited may point at models this picker does not offer —
  // deactivated, internal, or a search tier — and dropping one from its row
  // would silently rewrite the target to whatever happens to be first.
  const current: AliasTarget[] = alias?.targets ?? [];

  const firstChosen = rows.find((row) => row.modelId !== "")?.modelId ?? "";
  const chosenKind: ModelKind | null =
    alias?.kind ??
    candidates.find((model) => model.id === firstChosen)?.kind ??
    null;
  const offered = chosenKind ? candidates.filter((model) => model.kind === chosenKind) : candidates;

  const setRow = (key: number, change: Partial<TargetRow>) =>
    setRows((list) => list.map((row) => (row.key === key ? { ...row, ...change } : row)));
  const move = (index: number, by: -1 | 1) =>
    setRows((list) => {
      const to = index + by;
      if (to < 0 || to >= list.length) return list;
      const copy = [...list];
      [copy[index], copy[to]] = [copy[to]!, copy[index]!];
      return copy;
    });

  const chosen = rows.filter((row) => row.modelId !== "");
  const weightsValid = chosen.every((row) => {
    const weight = Number(row.weight);
    return Number.isInteger(weight) && weight >= 1 && weight <= 1000;
  });
  const minutesValid = /^\d+$/.test(minutes) && Number(minutes) <= 43200;
  const priceWeightValid = (value: string) =>
    /^\d+$/.test(value) && Number(value) >= 1 && Number(value) <= 1000;
  const priceWeightsValid = priceWeightValid(inputWeight) && priceWeightValid(outputWeight);

  const submit = () => {
    onSubmit({
      name: name.trim(),
      display_name: displayName.trim() || null,
      description: description.trim() || null,
      targets: chosen.map((row) => ({ model_id: row.modelId, weight: Number(row.weight) })),
      route_rule: rule,
      // Only when the administrator changed it, so opening and saving an alias
      // whose window is not a whole number of minutes does not round it.
      ...(Number(minutes) !== initialMinutes || alias === null
        ? { route_sticky_ttl_seconds: Number(minutes) * 60 }
        : {}),
      route_price_input_weight: Number(inputWeight),
      route_price_output_weight: Number(outputWeight),
      is_public: isPublic,
    });
  };

  const known = (RULES as readonly string[]).includes(rule);

  return (
    <Dialog
      open
      onClose={onClose}
      title={alias ? m["admin_routing.action.edit"]() : m["admin_routing.action.create"]()}
      footer={
        <>
          <Button onClick={onClose}>{m["model_detail.delete.cancel"]()}</Button>
          <Button
            variant="primary"
            busy={busy}
            disabled={name.trim() === "" || chosen.length === 0 || !weightsValid || !minutesValid || !priceWeightsValid}
            onClick={submit}
          >
            {alias ? m["admin_routing.action.save"]() : m["admin_routing.action.create"]()}
          </Button>
        </>
      }
    >
      <div className={FORM}>
        <Input
          label={m["admin_routing.form.name"]()}
          value={name}
          onChange={(event) => setName(event.target.value)}
          hint={<Message message={m["admin_routing.form.name_hint"]} markup={{ code: (c) => <code className={CODE}>{c}</code> }} />}
        />
        <Input
          label={m["admin_routing.form.display_name"]()}
          value={displayName}
          onChange={(event) => setDisplayName(event.target.value)}
          hint={m["admin_routing.form.display_name_hint"]()}
        />
        <Input
          label={m["admin_routing.form.description"]()}
          value={description}
          onChange={(event) => setDescription(event.target.value)}
        />

        <fieldset className="m-0 flex flex-col gap-3 border-0 p-0">
          <legend className="mb-1 text-base font-medium">{m["admin_routing.form.targets"]()}</legend>
          <p className={MUTED}>{m["admin_routing.form.targets_hint"]()}</p>
          {rows.map((row, index) => {
            // The row's current model stays selectable even when it is not a
            // candidate today (see `current`), and nothing another row already
            // picked is offered twice: the gateway refuses a duplicate.
            const orphan = current.find(
              (target) => target.id === row.modelId && !candidates.some((c) => c.id === target.id),
            );
            return (
              <div key={row.key} className={FORM_ROW}>
                <Select
                  label={m["admin_routing.form.target_n"]({ n: index + 1 })}
                  value={row.modelId}
                  onChange={(event) => setRow(row.key, { modelId: event.target.value })}
                >
                  <option value="">{m["admin_routing.form.target_none"]()}</option>
                  {orphan ? (
                    <option value={orphan.id}>
                      {orphan.name} — {m["admin_routing.badge.target_inactive"]()}
                    </option>
                  ) : null}
                  {offered
                    .filter(
                      (model) =>
                        model.id === row.modelId ||
                        !rows.some((other) => other.key !== row.key && other.modelId === model.id),
                    )
                    .map((model) => (
                      <option key={model.id} value={model.id}>
                        {model.display_name ? `${model.display_name} (${model.name})` : model.name}
                      </option>
                    ))}
                </Select>
                {rule === "weighted" ? (
                  <Input
                    label={m["admin_routing.form.weight_n"]({ n: index + 1 })}
                    type="number"
                    min={1}
                    max={1000}
                    value={row.weight}
                    onChange={(event) => setRow(row.key, { weight: event.target.value })}
                  />
                ) : null}
                <div className={ROW_ACTIONS}>
                  <Button
                    variant="ghost"
                    disabled={index === 0}
                    aria-label={m["admin_routing.form.move_up"]({ n: index + 1 })}
                    onClick={() => move(index, -1)}
                  >
                    ↑
                  </Button>
                  <Button
                    variant="ghost"
                    disabled={index === rows.length - 1}
                    aria-label={m["admin_routing.form.move_down"]({ n: index + 1 })}
                    onClick={() => move(index, 1)}
                  >
                    ↓
                  </Button>
                  <Button
                    variant="ghost"
                    disabled={rows.length === 1}
                    aria-label={m["admin_routing.form.remove_target"]({ n: index + 1 })}
                    onClick={() => setRows((list) => list.filter((r) => r.key !== row.key))}
                  >
                    ✕
                  </Button>
                </div>
              </div>
            );
          })}
          <div>
            <Button
              onClick={() => {
                setRows((list) => [...list, { key: nextKey, modelId: "", weight: "1" }]);
                setNextKey((key) => key + 1);
              }}
            >
              {m["admin_routing.form.add_target"]()}
            </Button>
          </div>
        </fieldset>

        <div className={FORM_ROW}>
          <Select
            label={m["admin_routing.form.rule"]()}
            value={rule}
            onChange={(event) => setRule(event.target.value)}
            hint={known ? RULE_HINTS[rule as (typeof RULES)[number]]() : undefined}
          >
            {/* A rule this console does not know, kept selectable so opening
                and saving an alias does not rewrite it to the default. */}
            {known ? null : <option value={rule}>{rule}</option>}
            {RULES.map((value) => (
              <option key={value} value={value}>
                {RULE_LABELS[value]()}
              </option>
            ))}
          </Select>
          <Input
            label={m["admin_routing.form.sticky"]()}
            type="number"
            min={0}
            max={43200}
            value={minutes}
            onChange={(event) => setMinutes(event.target.value)}
            hint={m["admin_routing.form.sticky_hint"]()}
          />
        </div>

        {/* Only the cheapest rule reads them; hidden otherwise rather than
            shown disabled, because they mean nothing to the other rules. */}
        {rule === "cheapest" ? (
          <div className={FORM_ROW}>
            <Input
              label={m["admin_routing.form.price_input_weight"]()}
              type="number"
              min={1}
              max={1000}
              value={inputWeight}
              onChange={(event) => setInputWeight(event.target.value)}
              hint={m["admin_routing.form.price_weight_hint"]()}
            />
            <Input
              label={m["admin_routing.form.price_output_weight"]()}
              type="number"
              min={1}
              max={1000}
              value={outputWeight}
              onChange={(event) => setOutputWeight(event.target.value)}
            />
          </div>
        ) : null}

        {/* Read-only on purpose, and said to be: it is the targets' kind, and
            the server refuses a route that would mix them. Showing it as a
            disabled control with the reason next to it is what turns "why can't
            I pick the embedding model?" from a support ticket into a sentence
            on the screen. */}
        <Input
          label={m["admin_routing.form.kind"]()}
          value={chosenKind ?? ""}
          readOnly
          disabled
          hint={m["admin_routing.form.kind_hint"]()}
        />

        <label className="flex items-center gap-2">
          <input
            type="checkbox"
            checked={isPublic}
            onChange={(event) => setIsPublic(event.target.checked)}
          />
          <span>{m["admin_routing.badge.public"]()}</span>
        </label>
      </div>

      {/* The same visibility controls the model page has, by id. Not reimplemented
          here for the reason that matters: an alias is a `models` row, so the
          grant endpoints take its id unchanged and there is nothing alias-shaped
          about granting access to one. */}
      {alias ? (
        <ModelAccess
          id={alias.id}
          name={alias.name}
          grantedToUsers={alias.granted_to_users}
          revokedNote={() => m["model_detail.access.revoke_note"]()}
          cardDescription={() => m["admin_routing.form.description"]()}
        />
      ) : null}

      {candidates.length === 0 && !alias ? (
        <Notice tone="warn">{m["admin_routing.form.no_targets"]()}</Notice>
      ) : null}
    </Dialog>
  );
}
