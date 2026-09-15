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

import { Badge, Button, Card, Input, Notice, Pagination, Spinner, Table } from "@llmp/ui";
import type { Column } from "@llmp/ui";
import { useState } from "react";
import { useCreateGroup, useDeleteGroup, useGroups } from "../lib/admin";
import { CHIPS, MUTED, PAGE, ROW_ACTIONS } from "../lib/layout";
import { usePaginated } from "../lib/paging";
import type { AdminGroup } from "../lib/types";
import { PageHeader } from "../components/PageHeader";
import { Dialog } from "@llmp/ui";
import { useAddGroupMember, useGroupMembers, useRemoveGroupMember, useUsers } from "../lib/admin";
import { FORM } from "../lib/layout";
import { useOptionalToast } from "../lib/toast";

export function AdminGroups() {
  const paged = usePaginated();
  const groups = useGroups(paged.page);

  const [creating, setCreating] = useState(false);
  const [managing, setManaging] = useState<AdminGroup | null>(null);
  const [deleting, setDeleting] = useState<AdminGroup | null>(null);

  const page = groups.data;
  const rows = page?.items ?? [];

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
      {group?.source === "oidc" && (
        <Notice tone="warn" title="This group comes from the identity provider">
          It will be recreated the next time one of its members signs in, unless
          the mappings or the directory have changed.
        </Notice>
      )}
    </Dialog>
  );
}
