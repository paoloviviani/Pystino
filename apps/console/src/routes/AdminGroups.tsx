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
} from "@llmp/ui";
import type { Column } from "@llmp/ui";
import { useState } from "react";
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

export function AdminGroups() {
  const paged = usePaginated();
  const groups = useGroups(paged.page);
  const updateGroup = useUpdateGroup();
  const toast = useOptionalToast();

  const [creating, setCreating] = useState(false);
  const [managing, setManaging] = useState<AdminGroup | null>(null);
  const [deleting, setDeleting] = useState<AdminGroup | null>(null);

  const page = groups.data;
  const rows = page?.items ?? [];

  const flipCapability = (
    group: AdminGroup,
    knob: "code_agents_allowed" | "api_keys_allowed",
    value: boolean,
  ) =>
    updateGroup.mutate(
      { id: group.id, body: { [knob]: value } },
      {
        onError: (caught: unknown) =>
          toast?.add({
            title: caught instanceof Error ? caught.message : "Could not update the group",
            type: "error",
          }),
      },
    );

  const columns: Column<AdminGroup>[] = [
    {
      key: "who",
      header: "Group",
      render: (group) => (
        <>
          <div>{group.name}</div>
          {group.description && <div className={MUTED}>{group.description}</div>}
        </>
      ),
    },
    {
      key: "source",
      header: "Source",
      // The source is the editability: a manual group's membership is this
      // screen's to change, an OIDC group's belongs to the directory. Showing
      // it as a badge on every row is cheaper than a refusal nobody expected.
      render: (group) => <Badge tone={group.source === "manual" ? "accent" : "neutral"}>{group.source}</Badge>,
    },
    {
      key: "members",
      header: "Members",
      numeric: true,
      render: (group) => group.member_count.toLocaleString(),
    },
    {
      key: "capabilities",
      header: "Capabilities",
      // ADR 0096: the two toggles. A flag grants a capability to the group's
      // active members and never withholds one, so the toggle is the whole
      // editor — no dialog, no confirm. The mutation invalidates the users
      // too, so a badge there changes with the flag that decides it.
      render: (group) => (
        <div className="flex flex-col gap-1.5">
          <label className="inline-flex items-center gap-1.5 text-sm">
            <input
              type="checkbox"
              aria-label={`${group.name}: coding agents`}
              checked={group.code_agents_allowed}
              onChange={(event) =>
                flipCapability(group, "code_agents_allowed", event.target.checked)
              }
              disabled={updateGroup.isPending}
              className="accent-accent"
            />
            Agents
          </label>
          <label className="inline-flex items-center gap-1.5 text-sm">
            <input
              type="checkbox"
              aria-label={`${group.name}: API keys`}
              checked={group.api_keys_allowed}
              onChange={(event) =>
                flipCapability(group, "api_keys_allowed", event.target.checked)
              }
              disabled={updateGroup.isPending}
              className="accent-accent"
            />
            API keys
          </label>
        </div>
      ),
    },
    {
      key: "models",
      header: "Models",
      render: (group) =>
        group.models.length === 0 ? (
          <span className={MUTED}>none granted</span>
        ) : (
          <div className={CHIPS}>
            {group.models.map((model) => (
              <Badge key={model}>{model}</Badge>
            ))}
          </div>
        ),
    },
    {
      key: "actions",
      header: "",
      render: (group) => (
        <div className={ROW_ACTIONS}>
          <Button variant="ghost" onClick={() => setManaging(group)}>
            Members
          </Button>
          <Button variant="ghost" className="text-danger" onClick={() => setDeleting(group)}>
            Delete
          </Button>
        </div>
      ),
    },
  ];

  return (
    <div className={PAGE}>
      <PageHeader
        title="Groups"
        subtitle="Who belongs together, and what the group may use. Model access is
          granted per group, on the Models screen."
        actions={
          <Button variant="primary" onClick={() => setCreating(true)}>
            New group
          </Button>
        }
      />

      {groups.error ? (
        <Notice tone="danger" title="Could not load groups">
          {groups.error instanceof Error ? groups.error.message : "Unknown error."}
        </Notice>
      ) : null}

      <Card>
        <div className="flex flex-wrap items-end gap-3">
          <div className="min-w-56 flex-1">
            <Input
              label="Search"
              value={paged.search}
              onChange={(event) => paged.setSearch(event.target.value)}
              placeholder="group name or description"
              hint={
                page
                  ? `${page.total.toLocaleString()} matching`
                  : "Searches every group, not just this page."
              }
            />
          </div>
        </div>
      </Card>

      <Card flush>
        {groups.isPending ? (
          <Spinner label="Loading groups" />
        ) : (
          <>
            <Table
              columns={columns}
              rows={rows}
              rowKey={(group) => group.id}
              empty={paged.query ? "No group matches that." : "No groups yet."}
              caption="Groups, their members and their model access."
            />
            <Pagination
              total={page?.total ?? 0}
              limit={paged.limit}
              offset={paged.offset}
              onOffsetChange={paged.setOffset}
              noun="groups"
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
      title="New group"
      onClose={close}
      footer={
        <>
          <Button onClick={close}>Cancel</Button>
          <Button
            variant="primary"
            busy={create.isPending}
            disabled={!name.trim()}
            onClick={() =>
              create.mutate(
                { name: name.trim(), description: description.trim() || undefined },
                {
                  onSuccess: (group) => {
                    toast?.add({ title: `Group ${group.name} created`, type: "success" });
                    close();
                  },
                  onError: () =>
                    toast?.add({ title: "Could not create the group", type: "error" }),
                },
              )
            }
          >
            Create group
          </Button>
        </>
      }
    >
      {create.error ? (
        <Notice tone="danger">
          {create.error instanceof Error ? create.error.message : "Unknown error."}
        </Notice>
      ) : null}
      <div className={FORM}>
        <Input
          label="Name"
          value={name}
          onChange={(event) => setName(event.target.value)}
          hint="Shown wherever a group is named: pickers, listings, reports."
        />
        <Input
          label="Description"
          value={description}
          onChange={(event) => setDescription(event.target.value)}
          hint="Optional. A sentence saying what the group is for."
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
      title={group ? `Members — ${group.name}` : "Members"}
      onClose={onClose}
      footer={<Button onClick={onClose}>Done</Button>}
    >
      <div className={FORM}>
        {adding ? (
          <div className="flex flex-wrap items-end gap-3">
            <div className="min-w-56 flex-1">
              <Input
                label="Add a member"
                value={pickerPaging.search}
                onChange={(event) => pickerPaging.setSearch(event.target.value)}
                placeholder="email, name or identity provider subject"
                autoFocus
              />
            </div>
            <Button variant="ghost" onClick={() => setAdding(false)}>
              Cancel
            </Button>
          </div>
        ) : (
          <Button variant="primary" onClick={() => setAdding(true)}>
            Add member
          </Button>
        )}

        {adding && pickerPaging.query !== "" ? (
          picker.isPending ? (
            <Spinner label="Searching" />
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
                          toast?.add({ title: "Member added", type: "success" });
                          setAdding(false);
                          pickerPaging.setSearch("");
                        },
                        onError: () =>
                          toast?.add({ title: "Could not add the member", type: "error" }),
                      })
                    }
                  >
                    Add
                  </Button>
                </div>
              ))}
              {(picker.data?.items ?? []).length === 0 && (
                <p className="m-0 text-sm text-ink-muted">No account matches that.</p>
              )}
            </div>
          )
        ) : null}

        {members.isPending ? (
          <Spinner label="Loading members" />
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
                      <Badge tone="neutral">from the directory</Badge>
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
                      onSuccess: () => toast?.add({ title: "Member removed", type: "success" }),
                      onError: () =>
                        toast?.add({ title: "Could not remove the member", type: "error" }),
                    })
                  }
                >
                  Remove
                </Button>
              </div>
            ))}
            {(members.data?.items ?? []).length === 0 && (
              <p className="m-0 text-sm text-ink-muted">No members yet.</p>
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
      title={`Delete ${group?.name ?? "this group"}?`}
      onClose={onClose}
      footer={
        <>
          <Button onClick={onClose}>Cancel</Button>
          <Button
            variant="danger"
            busy={remove.isPending}
            onClick={() =>
              group &&
              remove.mutate(group.id, {
                onSuccess: () => {
                  toast?.add({ title: "Group deleted", type: "success" });
                  onClose();
                },
                onError: () =>
                  toast?.add({ title: "Could not delete the group", type: "error" }),
              })
            }
          >
            Delete permanently
          </Button>
        </>
      }
    >
      <p>
        Model access granted to this group is removed, and anyone billing to it
        as their default goes back to choosing at request time. Their quota and
        redaction rules scoped to the group keep their scope but stop matching
        anyone.
      </p>
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
    <Notice tone="warn" title="This group comes from the identity provider">
      It will be recreated the next time one of its members signs in, unless
      the mappings or the directory have changed.
    </Notice>
  ) : (
    <Notice tone="info" title="This group comes from the identity provider">
      Its members lose it now. When one of them next signs in, its name is
      listed under “Groups from your identity provider” again, and nothing is
      recreated unless you map it.
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
            title: action === "dismiss" ? `${row.name} ignored` : `${row.name} listed again`,
            type: "success",
          });
        },
        onError: (error) =>
          toast?.add({
            title: `Could not ${action} ${row.name}`,
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
        Map…
      </Button>
      {row.dismissed ? (
        <Button variant="ghost" busy={busy(row, "restore")} onClick={() => run(row, "restore")}>
          Restore
        </Button>
      ) : (
        <Button variant="ghost" busy={busy(row, "dismiss")} onClick={() => run(row, "dismiss")}>
          Ignore
        </Button>
      )}
    </>
  );

  const columns: Column<SeenGroup>[] = [
    {
      key: "name",
      header: "Name",
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
      header: "People",
      numeric: true,
      render: (row) => row.people.toLocaleString(),
    },
    {
      key: "seen",
      header: "Last seen",
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
      title="Groups from your identity provider"
      description={page ? <ModeLine page={page} /> : null}
    >
      {seen.error ? (
        <Notice tone="danger" title="Could not load the groups your identity provider reported">
          {seen.error instanceof Error ? seen.error.message : "Unknown error."}
        </Notice>
      ) : null}
      {/* ADR 0095 §4: the mappings this panel decides, with their Unmap.
          Memberships granted through a mapping are the directory's to
          reconcile, so they go at the next sign-in — the confirm says so. */}
      {policy.data && policy.data.group_mappings.length > 0 ? (
        <div className="mb-4">
          <p className="mb-2 text-sm font-medium">Current mappings</p>
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
                  aria-label={`Unmap ${rule.idp}`}
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
                            title: `${rule.idp} unmapped`,
                            description:
                              "Memberships granted through the mapping go at each person's next sign-in.",
                            type: "success",
                          }),
                        onError: (error) =>
                          toast?.add({
                            title: "Could not unmap",
                            description: error instanceof Error ? error.message : undefined,
                            type: "error",
                          }),
                      },
                    );
                  }}
                >
                  Unmap
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
              label="Search"
              value={paged.search}
              onChange={(event) => paged.setSearch(event.target.value)}
              placeholder="group name"
              hint={page ? `${page.total.toLocaleString()} ${showDismissed ? "dismissed" : "waiting"}` : undefined}
            />
          </div>
          {/* Aligned with the input, not its label. */}
          <Button className="mt-6" onClick={() => setShowDismissed((value) => !value)}>
            {showDismissed ? "Show waiting" : "Show dismissed"}
          </Button>
        </div>
        {seen.isPending ? (
          <Spinner label="Loading the groups your identity provider reported" />
        ) : rows.length === 0 && !paged.query ? (
          <EmptyState
            title={showDismissed ? "Nothing dismissed." : "Nothing waiting to be mapped."}
            detail={
              showDismissed
                ? "Names you dismiss are kept here, to map or list again later."
                : "Group names your identity provider reports appear here after someone signs in with them."
            }
          />
        ) : (
          <>
            <Table
              columns={columns}
              rows={rows}
              rowKey={(row) => row.id}
              empty="No group name matches that."
              caption="Group names your identity provider reported, and how many people carry each."
            />
            <Pagination
              total={page?.total ?? 0}
              limit={paged.limit}
              offset={paged.offset}
              onOffsetChange={paged.setOffset}
              noun="names"
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
      title={`Map ${seen?.name ?? ""}`}
      onClose={close}
      footer={
        <>
          <Button onClick={close}>Cancel</Button>
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
                      title: `${seen.name} mapped to ${result.group.name}`,
                      description: importedDescription(result),
                      type: "success",
                    });
                    close();
                  },
                  onError: (error) =>
                    toast?.add({
                      title: "Could not map",
                      description: error instanceof Error ? error.message : undefined,
                      type: "error",
                    }),
                },
              );
            }}
          >
            Map
          </Button>
        </>
      }
    >
      <div className={FORM}>
        {map.error ? (
          <Notice tone="danger" title="Could not map">
            {map.error instanceof Error ? map.error.message : "Unknown error."}
          </Notice>
        ) : null}
        <fieldset>
          <legend className="p-0 text-sm">What this name grants</legend>
          <div className="flex flex-col gap-1.5">
            {(
              [
                ["new_same_name", "New group with the same name"],
                ["new_named", "New group named…"],
                ["existing", "Existing group…"],
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
                {label}
              </label>
            ))}
          </div>
        </fieldset>
        {target === "new_named" ? (
          <Input
            label="Name the new group"
            value={name}
            onChange={(event) => setName(event.target.value)}
            placeholder="engineering"
          />
        ) : null}
        {target === "existing" ? (
          <Select
            label="Local group"
            value={groupId}
            onChange={(event) => setGroupId(event.target.value)}
            hint="The IdP name now grants this group, wherever it is claimed."
          >
            <option value="">Pick a group…</option>
            {options.map((group) => (
              <option key={group.id} value={group.id}>
                {group.name}
              </option>
            ))}
          </Select>
        ) : null}
        <Input
          label="Reason (optional)"
          value={reason}
          onChange={(event) => setReason(event.target.value)}
          placeholder="recorded on the policy history"
        />
      </div>
    </Dialog>
  );
}

/** The one line that says what this list is under the deployment's mode. */
function ModeLine({ page }: { page: SeenGroupPage }) {
  const everyone = page.default_group ? (
    <>
      {" "}Everyone joins <strong>{page.default_group}</strong> at their first sign-in, so
      new people can use public models before anything is mapped.
    </>
  ) : null;
  return page.group_import === "auto" ? (
    <>
      Every name is mapped automatically at sign-in. Names listed here were seen
      before that was turned on.{everyone}
    </>
  ) : (
    <>
      Nothing is created until you map it. Mapping creates the group (or names
      one that exists) and gives it to the people who carry it now — or at
      their next sign-in where the provider's groups only apply then.{everyone}
    </>
  );
}

function importedDescription(result: SeenGroupImport): string {
  if (result.applied === "next_login")
    return "Its members get it at their next sign-in. Grant it models on the Models screen.";
  const people = result.members_added === 1 ? "1 person" : `${result.members_added} people`;
  return `${people} added now. Grant it models on the Models screen.`;
}

function formatDate(iso: string): string {
  return new Date(iso).toLocaleDateString(undefined, {
    year: "numeric",
    month: "short",
    day: "numeric",
  });
}
