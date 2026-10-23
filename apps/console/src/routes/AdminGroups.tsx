/**
 * Group management (ADR 0050).
 *
 * Its own screen rather than a card at the foot of Users, because the two
 * listings answer different questions: Users is "who exists and what can they
 * sign in with", Groups is "what do people belong to and what may those
 * groups use". A card under the first made groups feel like a property of
 * users; they are a thing of their own, with their own search, their own page
 * of rows, and model access granted from the Models screen.
 */

import {
  Badge,
  Button,
  Card,
  EmptyState,
  Input,
  Notice,
  Pagination,
  Select,
  Spinner,
  Table,
  RowActions,
} from "@llmp/ui";
import type { Column } from "@llmp/ui";
import { useState } from "react";
import { useChatEnabled } from "../lib/queries";
import {
  type SeenGroupPage,
  useCreateGroup,
  useDeleteGroup,
  useGroups,
  useMapSeenGroup,
  useOidcPolicy,
  useSeenGroupAction,
  useSeenGroups,
  useUpdateGroup,
  useUpdateOidcPolicy,
} from "../lib/admin";
import { CHIPS, MUTED, PAGE, ROW_ACTIONS } from "../lib/layout";
import { usePaginated } from "../lib/paging";
import type { AdminGroup, SeenGroup, SeenGroupImport } from "../lib/types";
import { PageHeader } from "../components/PageHeader";
import { Dialog } from "@llmp/ui";
import { useAddGroupMember, useGroupMembers, useRemoveGroupMember, useUsers } from "../lib/admin";
import { FORM } from "../lib/layout";
import { useOptionalToast } from "../lib/toast";
import { formatNumber, formatDate } from "../lib/format";
import { Message } from "../components/Message";
import { m } from "../paraglide/messages.js";

export function AdminGroups() {
  const paged = usePaginated();
  const groups = useGroups(paged.page);
  const updateGroup = useUpdateGroup();
  const toast = useOptionalToast();
  const chatEnabled = useChatEnabled();

  const [creating, setCreating] = useState(false);
  const [managing, setManaging] = useState<AdminGroup | null>(null);
  const [deleting, setDeleting] = useState<AdminGroup | null>(null);

  const page = groups.data;
  const rows = page?.items ?? [];

  const flipCapability = (
    group: AdminGroup,
    knob: "code_agents_allowed" | "api_keys_allowed" | "local_connectors_allowed",
    value: boolean,
  ) =>
    updateGroup.mutate(
      { id: group.id, body: { [knob]: value } },
      {
        onError: (caught: unknown) =>
          toast?.add({
            // i18n: server text — the API's own error message.
            title: caught instanceof Error
              ? caught.message
              : m["admin_groups.toast.update_failed"](),
            type: "error",
          }),
      },
    );

  const columns: Column<AdminGroup>[] = [
    {
      key: "who",
      header: m["admin_groups.column.group"](),
      render: (group) => (
        <>
          <div>{group.name}</div>
          {group.description && <div className={MUTED}>{group.description}</div>}
        </>
      ),
    },
    {
      key: "source",
      header: m["admin_groups.column.source"](),
      // The source is the editability: a manual group's membership is this
      // screen's to change, an OIDC group's belongs to the directory. Showing
      // it as a badge on every row is cheaper than a refusal nobody expected.
      // `source` is the API's enum value, shown as it arrives.
      render: (group) => <Badge tone={group.source === "manual" ? "accent" : "neutral"}>{group.source}</Badge>,
    },
    {
      key: "members",
      header: m["admin_groups.column.members"](),
      numeric: true,
      render: (group) => formatNumber(group.member_count),
    },
    {
      key: "capabilities",
      header: m["admin_groups.column.capabilities"](),
      // ADR 0096: the two toggles. A flag grants a capability to the group's
      // active members and never withholds one, so the toggle is the whole
      // editor — no dialog, no confirm. The mutation invalidates the users
      // too, so a badge there changes with the flag that decides it.
      render: (group) => (
        <div className="flex flex-col gap-1.5">
          {chatEnabled && (
          <label className="inline-flex items-center gap-1.5 text-sm">
            <input
              type="checkbox"
              aria-label={m["admin_groups.capability.agents_aria"]({ name: group.name })}
              checked={group.code_agents_allowed}
              onChange={(event) =>
                flipCapability(group, "code_agents_allowed", event.target.checked)
              }
              disabled={updateGroup.isPending}
              className="accent-accent"
            />
            {m["admin_groups.capability.agents"]()}
          </label>
          )}
          <label className="inline-flex items-center gap-1.5 text-sm">
            <input
              type="checkbox"
              aria-label={m["admin_groups.capability.api_keys_aria"]({ name: group.name })}
              checked={group.api_keys_allowed}
              onChange={(event) =>
                flipCapability(group, "api_keys_allowed", event.target.checked)
              }
              disabled={updateGroup.isPending}
              className="accent-accent"
            />
            {m["admin_groups.capability.api_keys"]()}
          </label>
          {chatEnabled && (
          <label className="inline-flex items-center gap-1.5 text-sm">
            <input
              type="checkbox"
              aria-label={m["admin_groups.capability.local_connectors_aria"]({ name: group.name })}
              checked={group.local_connectors_allowed}
              onChange={(event) =>
                flipCapability(group, "local_connectors_allowed", event.target.checked)
              }
              disabled={updateGroup.isPending}
              className="accent-accent"
            />
            {m["admin_groups.capability.local_connectors"]()}
          </label>
          )}
        </div>
      ),
    },
    {
      key: "models",
      header: m["admin_groups.column.models"](),
      render: (group) =>
        group.models.length === 0 ? (
          <span className={MUTED}>{m["admin_groups.models_none"]()}</span>
        ) : (
          <div className={CHIPS}>
            {/* An alias is a name this deployment defined and resolves to a
                model. It sits in the same list because it is granted the same
                way, and it is badged because an operator comparing "Research
                may use gpt-4o-mini" with "Research may use fast" needs to know
                those are not the same kind of thing (ADR 0101). */}
            {group.models.map((model) =>
              group.aliases.includes(model) ? (
                <Badge key={model} tone="accent">
                  {model}
                  <span className={MUTED}> {m["admin_groups.models.alias_badge"]()}</span>
                </Badge>
              ) : (
                <Badge key={model}>{model}</Badge>
              ),
            )}
          </div>
        ),
    },
    {
      key: "actions",
      header: "",
      render: (group) => (
        <div className={ROW_ACTIONS}>
          <Button variant="secondary" onClick={() => setManaging(group)}>
            {m["admin_groups.action.members"]()}
          </Button>
          <RowActions
            label={m["table.row_actions"]({ name: group.name })}
            actions={[
              {
                key: "delete",
                label: m["admin_groups.action.delete"](),
                onSelect: () => setDeleting(group),
                destructive: true,
              },
            ]}
          />
        </div>
      ),
    },
  ];

  return (
    <div className={PAGE}>
      <PageHeader
        title={m["admin_groups.title"]()}
        subtitle={m["admin_groups.subtitle"]()}
        actions={
          <Button variant="primary" onClick={() => setCreating(true)}>
            {m["admin_groups.new_group"]()}
          </Button>
        }
      />

      {groups.error ? (
        <Notice tone="danger" title={m["admin_groups.load_error.title"]()}>
          {/* i18n: server text — the API's own error message. */}
          {groups.error instanceof Error ? groups.error.message : m["admin_groups.unknown_error"]()}
        </Notice>
      ) : null}

      <Card>
        <div className="flex flex-wrap items-end gap-3">
          <div className="min-w-56 flex-1">
            <Input
              label={m["admin_groups.search"]()}
              value={paged.search}
              onChange={(event) => paged.setSearch(event.target.value)}
              placeholder={m["admin_groups.search_placeholder"]()}
              hint={
                page
                  ? m["admin_groups.search_hint_matching"]({ count: page.total })
                  : m["admin_groups.search_hint_global"]()
              }
            />
          </div>
        </div>
      </Card>

      <Card flush>
        {groups.isPending ? (
          <Spinner label={m["admin_groups.loading"]()} />
        ) : (
          <>
            <Table
              columns={columns}
              rows={rows}
              rowKey={(group) => group.id}
              empty={paged.query ? m["admin_groups.empty_query"]() : m["admin_groups.empty"]()}
              caption={m["admin_groups.caption"]()}
            />
            <Pagination
              total={page?.total ?? 0}
              limit={paged.limit}
              offset={paged.offset}
              onOffsetChange={paged.setOffset}
              noun={m["admin_groups.noun"]()}
              busy={groups.isFetching}
            />
          </>
        )}
      </Card>

      <SeenGroups />

      <CreateGroupDialog open={creating} onClose={() => setCreating(false)} />
      <GroupMembersDialog group={managing} onClose={() => setManaging(null)} />
      <DeleteGroupDialog group={deleting} onClose={() => setDeleting(null)} />
    </div>
  );
}

/**
 * Creating a group (ADR 0050). Manual by birth: the name an operator types
 * here is the name nobody's identity provider will reconcile away.
 */
function CreateGroupDialog({ open, onClose }: { open: boolean; onClose: () => void }) {
  const create = useCreateGroup();
  const toast = useOptionalToast();
  const [name, setName] = useState("");
  const [description, setDescription] = useState("");

  const close = () => {
    setName("");
    setDescription("");
    create.reset();
    onClose();
  };

  return (
    <Dialog
      open={open}
      title={m["admin_groups.create.title"]()}
      onClose={close}
      footer={
        <>
          <Button onClick={close}>{m["admin_groups.create.cancel"]()}</Button>
          <Button
            variant="primary"
            busy={create.isPending}
            disabled={!name.trim()}
            onClick={() =>
              create.mutate(
                { name: name.trim(), description: description.trim() || undefined },
                {
                  onSuccess: (group) => {
                    toast?.add({
                      title: m["admin_groups.create.toast_done"]({ name: group.name }),
                      type: "success",
                    });
                    close();
                  },
                  onError: () =>
                    toast?.add({
                      title: m["admin_groups.create.toast_failed"](),
                      type: "error",
                    }),
                },
              )
            }
          >
            {m["admin_groups.create.submit"]()}
          </Button>
        </>
      }
    >
      {create.error ? (
        <Notice tone="danger">
          {/* i18n: server text — the API's own error message. */}
          {create.error instanceof Error ? create.error.message : m["admin_groups.unknown_error"]()}
        </Notice>
      ) : null}
      <div className={FORM}>
        <Input
          label={m["admin_groups.create.name"]()}
          value={name}
          onChange={(event) => setName(event.target.value)}
          hint={m["admin_groups.create.name_hint"]()}
        />
        <Input
          label={m["admin_groups.create.description"]()}
          value={description}
          onChange={(event) => setDescription(event.target.value)}
          hint={m["admin_groups.create.description_hint"]()}
        />
      </div>
    </Dialog>
  );
}

/**
 * The membership editor for one manual group.
 *
 * Adding is search-first: a directory that outgrows one page must not be
 * scrolled by hand, and the same server-side search the Users screen uses is
 * what makes "add someone" one lookup instead of a scroll.
 */
function GroupMembersDialog({ group, onClose }: { group: AdminGroup | null; onClose: () => void }) {
  const [adding, setAdding] = useState(false);
  const add = useAddGroupMember(group?.id ?? null);
  const remove = useRemoveGroupMember(group?.id ?? null);
  const toast = useOptionalToast();

  const memberPaging = usePaginated();
  const members = useGroupMembers(group?.id ?? null, memberPaging.page);
  const pickerPaging = usePaginated();
  const picker = useUsers(pickerPaging.page, adding && pickerPaging.query !== "");

  return (
    <Dialog
      open={group !== null}
      title={
        group ? m["admin_groups.members.title"]({ name: group.name }) : m["admin_groups.members.title_plain"]()
      }
      onClose={onClose}
      footer={<Button onClick={onClose}>{m["admin_groups.members.done"]()}</Button>}
    >
      <div className={FORM}>
        {adding ? (
          <div className="flex flex-wrap items-end gap-3">
            <div className="min-w-56 flex-1">
              <Input
                label={m["admin_groups.members.add_search"]()}
                value={pickerPaging.search}
                onChange={(event) => pickerPaging.setSearch(event.target.value)}
                placeholder={m["admin_groups.members.add_placeholder"]()}
                autoFocus
              />
            </div>
            <Button variant="ghost" onClick={() => setAdding(false)}>
              {m["admin_groups.members.cancel"]()}
            </Button>
          </div>
        ) : (
          <Button variant="primary" onClick={() => setAdding(true)}>
            {m["admin_groups.members.add_member"]()}
          </Button>
        )}

        {adding && pickerPaging.query !== "" ? (
          picker.isPending ? (
            <Spinner label={m["admin_groups.members.searching"]()} />
          ) : (
            <div className="flex flex-col gap-1">
              {(picker.data?.items ?? []).map((user) => (
                <div
                  key={user.id}
                  className="flex items-center justify-between gap-2 rounded-md border border-line p-2"
                >
                  <div className="min-w-0">
                    <div className="truncate text-sm">
                      {user.display_name || user.email || user.subject}
                    </div>
                    <div className="truncate text-xs text-ink-faint">
                      {user.email ?? user.subject}
                    </div>
                  </div>
                  <Button
                    variant="ghost"
                    busy={add.isPending && add.variables === user.id}
                    onClick={() =>
                      add.mutate(user.id, {
                        onSuccess: () => {
                          toast?.add({
                            title: m["admin_groups.members.toast_added"](),
                            type: "success",
                          });
                          setAdding(false);
                          pickerPaging.setSearch("");
                        },
                        onError: () =>
                          toast?.add({
                            title: m["admin_groups.members.toast_add_failed"](),
                            type: "error",
                          }),
                      })
                    }
                  >
                    {m["admin_groups.members.add"]()}
                  </Button>
                </div>
              ))}
              {(picker.data?.items ?? []).length === 0 && (
                <p className="m-0 text-sm text-ink-muted">
                  {m["admin_groups.members.no_match"]()}
                </p>
              )}
            </div>
          )
        ) : null}

        {members.isPending ? (
          <Spinner label={m["admin_groups.members.loading"]()} />
        ) : (
          <div className="flex flex-col gap-1">
            {(members.data?.items ?? []).map((user) => (
              <div
                key={user.id}
                className="flex items-center justify-between gap-2 rounded-md border border-line p-2"
              >
                <div className="min-w-0">
                  <div className="flex items-center gap-2">
                    <span className="truncate text-sm">
                      {user.display_name || user.email || user.subject}
                    </span>
                    {/* Which of these the directory will still be deciding at
                        the next sign-in (ADR 0057). "Why is this person
                        still in this group" is asked on exactly this screen. */}
                    {user.membership_source === "oidc" && (
                      <Badge tone="neutral">{m["admin_groups.members.from_directory"]()}</Badge>
                    )}
                  </div>
                  <div className="truncate text-xs text-ink-faint">
                    {user.email ?? user.subject}
                  </div>
                </div>
                <Button
                  variant="ghost"
                  className="text-danger"
                  busy={remove.isPending && remove.variables === user.id}
                  onClick={() =>
                    remove.mutate(user.id, {
                      onSuccess: () =>
                        toast?.add({
                          title: m["admin_groups.members.toast_removed"](),
                          type: "success",
                        }),
                      onError: () =>
                        toast?.add({
                          title: m["admin_groups.members.toast_remove_failed"](),
                          type: "error",
                        }),
                    })
                  }
                >
                  {m["admin_groups.members.remove"]()}
                </Button>
              </div>
            ))}
            {(members.data?.items ?? []).length === 0 && (
              <p className="m-0 text-sm text-ink-muted">{m["admin_groups.members.none_yet"]()}</p>
            )}
          </div>
        )}
      </div>
    </Dialog>
  );
}

/**
 * Deleting a group, with its two consequences said in place: model access
 * dies with it, and an IdP group comes back at the next login.
 */
function DeleteGroupDialog({ group, onClose }: { group: AdminGroup | null; onClose: () => void }) {
  const remove = useDeleteGroup();
  const toast = useOptionalToast();

  return (
    <Dialog
      open={group !== null}
      title={m["admin_groups.delete.title"]({
        name: group?.name ?? m["admin_groups.delete.title_fallback"](),
      })}
      onClose={onClose}
      footer={
        <>
          <Button onClick={onClose}>{m["admin_groups.delete.cancel"]()}</Button>
          <Button
            variant="danger"
            busy={remove.isPending}
            onClick={() =>
              group &&
              remove.mutate(group.id, {
                onSuccess: () => {
                  toast?.add({ title: m["admin_groups.delete.toast_done"](), type: "success" });
                  onClose();
                },
                onError: () =>
                  toast?.add({
                    title: m["admin_groups.delete.toast_failed"](),
                    type: "error",
                  }),
              })
            }
          >
            {m["admin_groups.delete.confirm"]()}
          </Button>
        </>
      }
    >
      <p>{m["admin_groups.delete.body"]()}</p>
      {group?.source === "oidc" && <IdpGroupDeleteNotice />}
    </Dialog>
  );
}

/** What deleting a directory's group leads to depends on the import mode:
  * under `auto` the next sign-in recreates it (as a self-mapping); under
  * `manual` its name only comes back to the "Seen" list below, to map again
  * or dismiss. */
 function IdpGroupDeleteNotice() {
  const seen = useSeenGroups({ limit: 1 });
  return seen.data?.group_import === "auto" ? (
    <Notice tone="warn" title={m["admin_groups.delete.idp_title"]()}>
      {m["admin_groups.delete.idp_auto_body"]()}
    </Notice>
  ) : (
    <Notice tone="info" title={m["admin_groups.delete.idp_title"]()}>
      {m["admin_groups.delete.idp_manual_body"]()}
    </Notice>
  );
}

/**
 * Group names the identity provider reported that nothing maps yet
 * (GATEWAY_OIDC__GROUP_IMPORT=manual, the default). A real directory reports
 * every group a person is in: one GitLab sign-in used to create 67 groups.
 * Now they wait here, most-carried first, for Map or Dismiss.
 */
function SeenGroups() {
  const paged = usePaginated();
  const [showDismissed, setShowDismissed] = useState(false);
  const seen = useSeenGroups(paged.page, showDismissed);
  const act = useSeenGroupAction();
  const policy = useOidcPolicy();
  const unmap = useUpdateOidcPolicy();
  const toast = useOptionalToast();
  const [mapping, setMapping] = useState<SeenGroup | null>(null);

  const page = seen.data;
  const rows = page?.items ?? [];

  const run = (row: SeenGroup, action: "dismiss" | "restore") =>
    act.mutate(
      { id: row.id, action },
      {
        onSuccess: () => {
          toast?.add({
            title:
              action === "dismiss"
                ? m["admin_groups.seen.toast_ignored"]({ name: row.name })
                : m["admin_groups.seen.toast_relisted"]({ name: row.name }),
            type: "success",
          });
        },
        onError: (error) =>
          toast?.add({
            title:
              action === "dismiss"
                ? m["admin_groups.seen.toast_dismiss_failed"]({ name: row.name })
                : m["admin_groups.seen.toast_restore_failed"]({ name: row.name }),
            // i18n: server text — the API's own error message.
            description: error instanceof Error ? error.message : undefined,
            type: "error",
          }),
      },
    );

  const busy = (row: SeenGroup, action: string) =>
    act.isPending && act.variables?.id === row.id && act.variables.action === action;

  const actions = (row: SeenGroup) => (
    <>
      <Button variant="ghost" onClick={() => setMapping(row)}>
        {m["admin_groups.seen.map"]()}
      </Button>
      {row.dismissed ? (
        <Button variant="ghost" busy={busy(row, "restore")} onClick={() => run(row, "restore")}>
          {m["admin_groups.seen.restore"]()}
        </Button>
      ) : (
        <Button variant="ghost" busy={busy(row, "dismiss")} onClick={() => run(row, "dismiss")}>
          {m["admin_groups.seen.ignore"]()}
        </Button>
      )}
    </>
  );

  const columns: Column<SeenGroup>[] = [
    {
      key: "name",
      header: m["admin_groups.seen.column.name"](),
      render: (row) => (
        <>
          {/* Directory paths (gitlab/acme/ml-research) wrap at any point only
              when they must, not letter by letter in a narrow column. */}
          <div className="[overflow-wrap:anywhere]">{row.name}</div>
          {row.provider && <div className={MUTED}>{row.provider}</div>}
          {/* On a phone the actions column is dropped and the buttons sit
              under the name, where there is room for both. */}
          <div className="-ml-3 mt-1 flex gap-1 sm:hidden">{actions(row)}</div>
        </>
      ),
    },
    {
      key: "people",
      header: m["admin_groups.seen.column.people"](),
      numeric: true,
      render: (row) => formatNumber(row.people),
    },
    {
      key: "seen",
      header: m["admin_groups.seen.column.last_seen"](),
      hideBelow: "sm",
      render: (row) => formatDate(row.last_seen_at),
    },
    {
      key: "actions",
      header: "",
      hideBelow: "sm",
      render: (row) => <div className={ROW_ACTIONS}>{actions(row)}</div>,
    },
  ];

  return (
    <Card
      title={m["admin_groups.seen.card"]()}
      description={page ? <ModeLine page={page} /> : null}
    >
      {seen.error ? (
        <Notice tone="danger" title={m["admin_groups.seen.load_error.title"]()}>
          {/* i18n: server text — the API's own error message. */}
          {seen.error instanceof Error ? seen.error.message : m["admin_groups.unknown_error"]()}
        </Notice>
      ) : null}
      {/* ADR 0095 §4: the mappings this panel decides, with their Unmap.
          Memberships granted through a mapping are the directory's to
          reconcile, so they go at the next sign-in — the confirm says so. */}
      {policy.data && policy.data.group_mappings.length > 0 ? (
        <div className="mb-4">
          <p className="mb-2 text-sm font-medium">{m["admin_groups.seen.mappings_heading"]()}</p>
          <div className={CHIPS}>
            {policy.data.group_mappings.map((rule) => (
              <span
                key={rule.idp}
                className="inline-flex items-center gap-1.5 rounded-full border border-line px-3 py-1 text-xs"
              >
                <span className="[overflow-wrap:anywhere]">{rule.idp}</span>
                <span aria-hidden>→</span>
                <span className="font-medium [overflow-wrap:anywhere]">{rule.local}</span>
                <button
                  type="button"
                  aria-label={m["admin_groups.seen.unmap_aria"]({ idp: rule.idp })}
                  className="text-danger"
                  onClick={() => {
                    const reason = `Unmapped ${rule.idp}`;
                    unmap.mutate(
                      {
                        group_mappings: policy.data!.group_mappings.filter(
                          (candidate) => candidate.idp !== rule.idp,
                        ),
                        reason,
                      },
                      {
                        onSuccess: () =>
                          toast?.add({
                            title: m["admin_groups.seen.toast_unmapped"]({ idp: rule.idp }),
                            description: m["admin_groups.seen.unmap_note"](),
                            type: "success",
                          }),
                        onError: (error) =>
                          toast?.add({
                            title: m["admin_groups.seen.toast_unmap_failed"](),
                            // i18n: server text — the API's own error message.
                            description: error instanceof Error ? error.message : undefined,
                            type: "error",
                          }),
                      },
                    );
                  }}
                >
                  {m["admin_groups.seen.unmap"]()}
                </button>
              </span>
            ))}
          </div>
        </div>
      ) : null}
      <div className={FORM}>
        <div className="flex flex-wrap items-start gap-3">
          <div className="min-w-56 flex-1">
            <Input
              label={m["admin_groups.search"]()}
              value={paged.search}
              onChange={(event) => paged.setSearch(event.target.value)}
              placeholder={m["admin_groups.seen.search_placeholder"]()}
              hint={
                page
                  ? showDismissed
                    ? m["admin_groups.seen.hint_dismissed"]({ count: page.total })
                    : m["admin_groups.seen.hint_waiting"]({ count: page.total })
                  : undefined
              }
            />
          </div>
          {/* Aligned with the input, not its label. */}
          <Button className="mt-6" onClick={() => setShowDismissed((value) => !value)}>
            {showDismissed
              ? m["admin_groups.seen.show_waiting"]()
              : m["admin_groups.seen.show_dismissed"]()}
          </Button>
        </div>
        {seen.isPending ? (
          <Spinner label={m["admin_groups.seen.loading"]()} />
        ) : rows.length === 0 && !paged.query ? (
          <EmptyState
            title={
              showDismissed
                ? m["admin_groups.seen.empty_dismissed_title"]()
                : m["admin_groups.seen.empty_waiting_title"]()
            }
            detail={
              showDismissed
                ? m["admin_groups.seen.empty_dismissed_detail"]()
                : m["admin_groups.seen.empty_waiting_detail"]()
            }
          />
        ) : (
          <>
            <Table
              columns={columns}
              rows={rows}
              rowKey={(row) => row.id}
              empty={m["admin_groups.seen.empty_query"]()}
              caption={m["admin_groups.seen.caption"]()}
            />
            <Pagination
              total={page?.total ?? 0}
              limit={paged.limit}
              offset={paged.offset}
              onOffsetChange={paged.setOffset}
              noun={m["admin_groups.seen.noun"]()}
              busy={seen.isFetching}
            />
          </>
        )}
      </div>
      <MapSeenDialog seen={mapping} onClose={() => setMapping(null)} />
    </Card>
  );
}

/** Map one seen IdP name: the one decision the rule leaves. Three answers to
 * "what does this name mean here" — a new group of the same name, a new group
 * under another name, or an existing group — and whichever it is, the
 * mapping is what the token's name resolves through. */
function MapSeenDialog({ seen, onClose }: { seen: SeenGroup | null; onClose: () => void }) {
  const groups = useGroups();
  const map = useMapSeenGroup();
  const toast = useOptionalToast();
  const [target, setTarget] = useState<"new_same_name" | "new_named" | "existing">(
    "new_same_name",
  );
  const [name, setName] = useState("");
  const [groupId, setGroupId] = useState("");
  const [reason, setReason] = useState("");

  const close = () => {
    map.reset();
    setTarget("new_same_name");
    setName("");
    setGroupId("");
    setReason("");
    onClose();
  };

  const options = groups.data?.items ?? [];
  const ready =
    target === "existing" ? groupId !== "" : target === "new_named" ? name.trim() !== "" : true;

  return (
    <Dialog
      open={seen !== null}
      title={m["admin_groups.map.title"]({ name: seen?.name ?? "" })}
      onClose={close}
      footer={
        <>
          <Button onClick={close}>{m["admin_groups.map.cancel"]()}</Button>
          <Button
            variant="primary"
            disabled={!ready}
            busy={map.isPending}
            onClick={() => {
              if (!seen) return;
              map.mutate(
                {
                  id: seen.id,
                  target,
                  ...(target === "new_named" ? { name: name.trim() } : {}),
                  ...(target === "existing" ? { group_id: groupId } : {}),
                  reason: reason.trim() || undefined,
                },
                {
                  onSuccess: (result) => {
                    toast?.add({
                      title: m["admin_groups.map.toast_done"]({
                        name: seen.name,
                        group: result.group.name,
                      }),
                      description: importedDescription(result),
                      type: "success",
                    });
                    close();
                  },
                  onError: (error) =>
                    toast?.add({
                      title: m["admin_groups.map.toast_failed"](),
                      // i18n: server text — the API's own error message.
                      description: error instanceof Error ? error.message : undefined,
                      type: "error",
                    }),
                },
              );
            }}
          >
            {m["admin_groups.map.confirm"]()}
          </Button>
        </>
      }
    >
      <div className={FORM}>
        {map.error ? (
          <Notice tone="danger" title={m["admin_groups.map.toast_failed"]()}>
            {/* i18n: server text — the API's own error message. */}
            {map.error instanceof Error ? map.error.message : m["admin_groups.unknown_error"]()}
          </Notice>
        ) : null}
        <fieldset>
          <legend className="p-0 text-sm">{m["admin_groups.map.legend"]()}</legend>
          <div className="flex flex-col gap-1.5">
            {(
              [
                ["new_same_name", m["admin_groups.map.option.new_same_name"]],
                ["new_named", m["admin_groups.map.option.new_named"]],
                ["existing", m["admin_groups.map.option.existing"]],
              ] as const
            ).map(([value, label]) => (
              <label key={value} className="flex cursor-pointer items-center gap-2 text-sm">
                <input
                  type="radio"
                  name="map-target"
                  checked={target === value}
                  onChange={() => setTarget(value)}
                  className="mt-[0.2em] accent-accent"
                />
                {label()}
              </label>
            ))}
          </div>
        </fieldset>
        {target === "new_named" ? (
          <Input
            label={m["admin_groups.map.new_name"]()}
            value={name}
            onChange={(event) => setName(event.target.value)}
            placeholder={m["admin_groups.map.new_name_placeholder"]()}
          />
        ) : null}
        {target === "existing" ? (
          <Select
            label={m["admin_groups.map.local_group"]()}
            value={groupId}
            onChange={(event) => setGroupId(event.target.value)}
            hint={m["admin_groups.map.local_group_hint"]()}
          >
            <option value="">{m["admin_groups.map.pick_group"]()}</option>
            {options.map((group) => (
              <option key={group.id} value={group.id}>
                {group.name}
              </option>
            ))}
          </Select>
        ) : null}
        <Input
          label={m["admin_groups.map.reason"]()}
          value={reason}
          onChange={(event) => setReason(event.target.value)}
          placeholder={m["admin_groups.map.reason_placeholder"]()}
        />
      </div>
    </Dialog>
  );
}

/** The one line that says what this list is under the deployment's mode. */
function ModeLine({ page }: { page: SeenGroupPage }) {
  const everyone = page.default_group ? (
    <>
      {" "}
      <Message
        message={m["admin_groups.mode.everyone"]}
        markup={{ strong: (children) => <strong>{children}</strong> }}
        inputs={{ group: page.default_group }}
      />
    </>
  ) : null;
  return page.group_import === "auto" ? (
    <>
      {m["admin_groups.mode.auto"]()}
      {everyone}
    </>
  ) : (
    <>
      {m["admin_groups.mode.manual"]()}
      {everyone}
    </>
  );
}

function importedDescription(result: SeenGroupImport): string {
  if (result.applied === "next_login") return m["admin_groups.import.next_login"]();
  return m["admin_groups.import.added_now"]({ count: result.members_added });
}

