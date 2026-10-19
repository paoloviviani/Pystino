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
import type { AdminAlias, AliasUnavailableReason, ModelKind } from "../lib/types";
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
        alias.target ? (
          // A real link to the model, because "what is `fast` actually?" is the
          // question this column exists to answer and it should be one click.
          <Link to={`/admin/models/${alias.target.id}`}>{alias.target.name}</Link>
        ) : (
          <span className={MUTED}>{m["admin_routing.form.target_none"]()}</span>
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
                ? ` — ${REASONS[alias.unavailable_reason].label()}`
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
          <Button onClick={() => setEditing(alias)}>{m["admin_routing.action.edit"]()}</Button>
          <Button
            busy={update.isPending && update.variables?.id === alias.id}
            onClick={() =>
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
              )
            }
          >
            {alias.is_active
              ? m["admin_routing.action.deactivate"]()
              : m["admin_routing.action.activate"]()}
          </Button>
          <Button variant="ghost" className="text-danger" onClick={() => setDeleting(alias)}>
            {m["admin_routing.action.delete"]()}
          </Button>
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
          <Notice key={alias.id} tone="danger" title={`${alias.name} — ${REASONS[alias.unavailable_reason!].label()}`}>
            {REASONS[alias.unavailable_reason!].body()}
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

/**
 * Create and edit, as one form.
 *
 * The target is chosen from active, non-alias models, and the kind follows it.
 * Retargeting across kinds is refused by the gateway (ADR 0101, "Designed for
 * routing": the targets of one alias must be interchangeable), so the picker
 * simply does not offer them — a control that could be set to something the
 * server would reject is worse than one that cannot be set to it.
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
    target_model_id: string;
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
  const [targetId, setTargetId] = useState(alias?.target?.id ?? "");
  const [isPublic, setIsPublic] = useState(alias?.is_public ?? false);

  const all = models.data?.items ?? [];
  const candidates = all.filter(
    (model) => model.kind !== "search" && model.provider_kind !== "internal" && model.is_active,
  );
  // **Retargeting is locked to the alias's kind; creating is not.** A new alias
  // takes its kind from whatever it points at, so the picker may offer every
  // kind — that is how an operator gets a chat alias or an embedding alias
  // without a second screen. An existing alias already has a kind its callers
  // were promised, and ADR 0101 refuses a retarget that would change it, so
  // offering the others here would only produce a 400 after the save.
  const retargetable =
    alias === null ? candidates : candidates.filter((model) => model.kind === alias.kind);
  const selected = candidates.find((model) => model.id === targetId);

  // An alias being edited may point at a model this picker does not offer —
  // deactivated, internal, or a search tier — and dropping it from the list
  // would silently rewrite the target to whatever happens to be first.
  const orphan =
    alias?.target && !candidates.some((model) => model.id === alias.target!.id)
      ? alias.target
      : null;

  const kind: ModelKind | null = selected?.kind ?? alias?.kind ?? null;

  const submit = () => {
    onSubmit({
      name: name.trim(),
      display_name: displayName.trim() || null,
      description: description.trim() || null,
      target_model_id: targetId,
      is_public: isPublic,
    });
  };

  return (
    <Dialog
      open
      onClose={onClose}
      title={
        alias
          ? m["admin_routing.action.edit"]()
          : m["admin_routing.action.create"]()
      }
      footer={
        <>
          <Button onClick={onClose}>{m["model_detail.delete.cancel"]()}</Button>
          <Button
            variant="primary"
            busy={busy}
            disabled={name.trim() === "" || targetId === ""}
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

        <div className={FORM_ROW}>
          <Select
            label={m["admin_routing.form.target"]()}
            value={targetId}
            onChange={(event) => setTargetId(event.target.value)}
            hint={m["admin_routing.form.target_hint"]()}
          >
            <option value="">{m["admin_routing.form.target_none"]()}</option>
            {/* The current target, shown even when it is not a candidate today,
                so an administrator opening an alias whose target was deactivated
                sees where it points instead of an empty select that would
                rewrite it to the first option on save. */}
            {orphan ? (
              <option value={orphan.id}>
                {orphan.name} — {m["admin_routing.badge.target_inactive"]()}
              </option>
            ) : null}
            {retargetable.map((model) => (
              <option key={model.id} value={model.id}>
                {model.display_name ? `${model.display_name} (${model.name})` : model.name}
              </option>
            ))}
          </Select>
        </div>

        {/* Read-only on purpose, and said to be: it is the target's kind, and
            the server refuses a retarget that would change it. Showing it as a
            disabled control with the reason next to it is what turns "why can't
            I pick the embedding model?" from a support ticket into a sentence
            on the screen. */}
        <Input
          label={m["admin_routing.form.kind"]()}
          value={kind ?? ""}
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