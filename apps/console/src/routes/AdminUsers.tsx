import {
  Badge,
  Button,
  Card,
  Dialog,
  Input,
  Notice,
  Pagination,
  Spinner,
  Table,
  } from "@llmp/ui";
import type { Column } from "@llmp/ui";
import { useState } from "react";
import {
  useAddGroupMember,
  useCreateGroup,
  useCreateUser,
  useDeleteGroup,
  useDeleteUser,
  useGroupMembers,
  useGroups,
  useRemoveGroupMember,
  useUpdateUser,
  useUsers,
} from "../lib/admin";
import { CHECK_ITEM, CHECK_LIST, CHIPS, CODE, FORM, MUTED, PAGE, ROW_ACTIONS } from "../lib/layout";
import { usePaginated } from "../lib/paging";
import type { AdminGroup, AdminUser } from "../lib/types";
import { useOptionalToast } from "../lib/toast";
import { PageHeader } from "../components/PageHeader";


export function AdminUsers() {
  // Searched and paged on the server. It used to filter in the browser over
  // whatever the endpoint had returned, which reads the same until the
  // organisation outgrows one response — at which point the box quietly
  // searches the first page and reports nothing found.
  const paged = usePaginated();
  const users = useUsers(paged.page);
  const groupPaging = usePaginated();
  const groups = useGroups(groupPaging.page);
  const update = useUpdateUser();
  const remove = useDeleteUser();
  const toast = useOptionalToast();

  const [creating, setCreating] = useState(false);
  const [deleting, setDeleting] = useState<AdminUser | null>(null);
  const [creatingGroup, setCreatingGroup] = useState(false);
  const [managing, setManaging] = useState<AdminGroup | null>(null);
  const [deletingGroup, setDeletingGroup] = useState<AdminGroup | null>(null);

  const page = users.data;
  const rows = page?.items ?? [];

  const columns: Column<AdminUser>[] = [
    {
      key: "who",
      header: "User",
      render: (user) => (
        <>
          <div>{user.display_name || user.email || user.subject}</div>
          <div className={MUTED}>{user.email ?? user.subject}</div>
          {/* Identity is (issuer, subject), not email. Two rows can therefore
              share a name and an address and still be different accounts —
              which is exactly what happens if the OIDC issuer URL ever changes.
              Showing the issuer makes that legible instead of looking like a
              duplicate nobody can explain. */}
          <div className={`${MUTED} ${CODE}`}>{user.issuer}</div>
        </>
      ),
    },
    {
      key: "groups",
      header: "Groups",
      render: (user) =>
        user.groups.length === 0 ? (
          <span className={MUTED}>none</span>
        ) : (
          <div className={CHIPS}>
            {user.groups.map((group) => (
              <Badge key={group}>{group}</Badge>
            ))}
          </div>
        ),
    },
    {
      key: "billing",
      header: "Billing group",
      render: (user) => user.default_billing_group ?? <span className={MUTED}>unset</span>,
    },
    {
      key: "keys",
      header: "Keys",
      numeric: true,
      render: (user) => user.active_key_count.toLocaleString(),
    },
    {
      key: "status",
      header: "Status",
      render: (user) => (
        <div className={CHIPS}>
          {user.is_admin && <Badge tone="accent">Admin</Badge>}
          {user.is_active ? <Badge tone="ok">Active</Badge> : <Badge tone="danger">Disabled</Badge>}
        </div>
      ),
    },
    {
      key: "seen",
      header: "Last login",
      render: (user) =>
        user.last_login_at ? formatDate(user.last_login_at) : <span className={MUTED}>never</span>,
    },
    {
      key: "actions",
      header: "",
      render: (user) => (
        <div className={ROW_ACTIONS}>
          <Button
            busy={update.isPending && update.variables?.id === user.id}
            onClick={() =>
              update.mutate(
                { id: user.id, is_active: !user.is_active },
                {
                  onSuccess: () => toast?.add({ title: "User updated", type: "success" }),
                  onError: () =>
                    toast?.add({ title: "Could not update the user", type: "error" }),
                },
              )
            }
          >
            {user.is_active ? "Disable" : "Enable"}
          </Button>
          <Button
            variant="danger"
            busy={remove.isPending && remove.variables === user.id}
            onClick={() => setDeleting(user)}
          >
            Delete
          </Button>
        </div>
      ),
    },
  ];

  return (
    <div className={PAGE}>
      <PageHeader
        title="Users"
        subtitle="Directory users arrive on first login (see Identity). New here means a
          local account with a password."
        actions={
          <Button variant="primary" onClick={() => setCreating(true)}>
            New user
          </Button>
        }
      />

      {update.error ? (
        <Notice tone="danger" title="Could not update the user">
          {update.error instanceof Error ? update.error.message : "Unknown error."}
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
              placeholder="email, name or identity provider subject"
              hint={
                page
                  ? `${page.total.toLocaleString()} matching`
                  : "Searches every account, not just this page."
              }
            />
          </div>
        </div>
      </Card>

      <Card flush>
        {users.isPending ? (
          <Spinner label="Loading users" />
        ) : users.error ? (
          <Notice tone="danger" title="Could not load users">
            {users.error instanceof Error ? users.error.message : "Unknown error."}
          </Notice>
        ) : (
          <>
            <Table
              columns={columns}
              rows={rows}
              rowKey={(user) => user.id}
              empty={paged.query ? "No user matches that." : "No users yet."}
              caption="Users, their groups and their keys."
            />
            <Pagination
              total={page?.total ?? 0}
              limit={paged.limit}
              offset={paged.offset}
              onOffsetChange={paged.setOffset}
              noun="users"
              busy={users.isFetching}
            />
          </>
        )}
      </Card>

      <GroupsCard
        groups={groups.data?.items ?? []}
        total={groups.data?.total ?? 0}
        loading={groups.isPending}
        paging={groupPaging}
        busy={groups.isFetching}
        onCreate={() => setCreatingGroup(true)}
        onManage={setManaging}
        onDelete={setDeletingGroup}
      />

      <CreateUserDialog
        open={creating}
        groups={groups.data?.items ?? []}
        onClose={() => setCreating(false)}
      />

      <DeleteUserDialog user={deleting} onClose={() => setDeleting(null)} />

      <CreateGroupDialog open={creatingGroup} onClose={() => setCreatingGroup(false)} />
      <GroupMembersDialog group={managing} onClose={() => setManaging(null)} />
      <DeleteGroupDialog group={deletingGroup} onClose={() => setDeletingGroup(null)} />
    </div>
  );
}

/**
 * Mint a local account (ADR 0048).
 *
 * Local only, and the form says so: a directory user created here would be
 * overwritten or orphaned at the next login, so the dialog names who it is
 * for — a contractor, a service account, someone the IdP will never know.
 * Groups are chosen from names, and a name that does not exist yet is
 * created — an operator typing a group name in a form means it.
 */
function CreateUserDialog({
  open,
  groups,
  onClose,
}: {
  open: boolean;
  groups: AdminGroup[];
  onClose: () => void;
}) {
  const create = useCreateUser();
  const toast = useOptionalToast();
  const [email, setEmail] = useState("");
  const [displayName, setDisplayName] = useState("");
  const [password, setPassword] = useState("");
  const [isAdmin, setIsAdmin] = useState(false);
  const [picked, setPicked] = useState<string[]>([]);

  const close = () => {
    setEmail("");
    setDisplayName("");
    setPassword("");
    setIsAdmin(false);
    setPicked([]);
    create.reset();
    onClose();
  };

  const toggleGroup = (name: string) =>
    setPicked((current) =>
      current.includes(name) ? current.filter((g) => g !== name) : [...current, name],
    );

  const submit = () =>
    create.mutate(
      {
        email: email.trim(),
        password,
        display_name: displayName.trim() || undefined,
        is_admin: isAdmin,
        groups: picked,
      },
      {
        onSuccess: (user) => {
          toast?.add({ title: `Account for ${user.subject} created`, type: "success" });
          close();
        },
        onError: () =>
          toast?.add({ title: "Could not create the account", type: "error" }),
      },
    );

  return (
    <Dialog
      open={open}
      title="New local user"
      onClose={close}
      footer={
        <>
          <Button onClick={close}>Cancel</Button>
          <Button
            variant="primary"
            busy={create.isPending}
            disabled={!email.trim() || password.length === 0}
            onClick={submit}
          >
            Create account
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
          label="Email"
          type="email"
          value={email}
          onChange={(event) => setEmail(event.target.value)}
          hint="The sign-in name, and the account's key."
        />
        <Input
          label="Password"
          type="password"
          value={password}
          onChange={(event) => setPassword(event.target.value)}
          hint="Handed to the person once, by you. Reset here later if it leaks."
        />
        <Input
          label="Display name"
          value={displayName}
          onChange={(event) => setDisplayName(event.target.value)}
        />
        <label className={CHECK_ITEM}>
          <input
            type="checkbox"
            checked={isAdmin}
            onChange={(event) => setIsAdmin(event.target.checked)}
          />
          Administrator
        </label>
        <div>
          <div className="text-xs font-medium tracking-[0.01em] text-ink-muted">Groups</div>
          <div className={CHECK_LIST}>
            {groups.map((group) => (
              <label key={group.id} className={CHECK_ITEM}>
                <input
                  type="checkbox"
                  checked={picked.includes(group.name)}
                  onChange={() => toggleGroup(group.name)}
                />
                {group.name}
              </label>
            ))}
          </div>
          {/* Only active groups are listed; naming one that does not exist is
              also fine — the account creation makes it. */}
          <p className="mt-1 text-xs text-ink-faint">
            A name that is not on the list is created when the account is.
          </p>
        </div>
      </div>
    </Dialog>
  );
}

/**
 * Deleting an account, with the two facts that make the decision honest:
 * the spend survives the person, and their rules go inert rather than away.
 */
function DeleteUserDialog({ user, onClose }: { user: AdminUser | null; onClose: () => void }) {
  const remove = useDeleteUser();
  const toast = useOptionalToast();

  return (
    <Dialog
      open={user !== null}
      title={`Delete ${user?.display_name || user?.email || user?.subject || "this account"}?`}
      onClose={onClose}
      footer={
        <>
          <Button onClick={onClose}>Cancel</Button>
          <Button
            variant="danger"
            busy={remove.isPending}
            onClick={() =>
              user &&
              remove.mutate(user.id, {
                onSuccess: () => {
                  toast?.add({ title: "Account deleted", type: "success" });
                  onClose();
                },
                onError: () =>
                  toast?.add({ title: "Could not delete the account", type: "error" }),
              })
            }
          >
            Delete permanently
          </Button>
        </>
      }
    >
      <p>
        The account's keys stop working immediately and cannot be restored. Their
        quota and redaction rules keep their scope but stop matching anyone until
        you delete or re-point them.
      </p>
      <p className="text-sm text-ink-muted">
        Past usage stays in the ledger, attributed to their groups as it was
        billed — only the name on the per-user breakdown goes.
      </p>
    </Dialog>
  );
}

function GroupsCard({
  groups,
  total,
  loading,
  paging,
  busy,
  onCreate,
  onManage,
  onDelete,
}: {
  groups: AdminGroup[];
  total: number;
  loading: boolean;
  paging: ReturnType<typeof usePaginated>;
  busy: boolean;
  onCreate: () => void;
  onManage: (group: AdminGroup) => void;
  onDelete: (group: AdminGroup) => void;
}) {
  const columns: Column<AdminGroup>[] = [
    { key: "name", header: "Group", render: (group) => group.name },
    {
      key: "source",
      header: "Source",
      // A group the IdP owns cannot be meaningfully edited here, so where it
      // came from is worth showing beside it.
      render: (group) => <Badge>{group.source}</Badge>,
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
          <Button variant="ghost" onClick={() => onManage(group)}>
            Members
          </Button>
          <Button variant="ghost" onClick={() => onDelete(group)}>
            Delete
          </Button>
        </div>
      ),
    },
  ];

  return (
    <Card
      title="Groups"
      flush
      description="Model access is granted per group, on the Models page. Membership is
        editable here for manual groups only — an identity-provider group is its
        directory's (ADR 0050)."
      actions={
        <Button variant="primary" onClick={onCreate}>
          New group
        </Button>
      }
    >
      {loading ? (
        <Spinner />
      ) : (
        <>
          <Table
            columns={columns}
            rows={groups}
            rowKey={(group) => group.id}
            empty="No groups."
          />
          <Pagination
            total={total}
            limit={paging.limit}
            offset={paging.offset}
            onOffsetChange={paging.setOffset}
            noun="groups"
            busy={busy}
          />
        </>
      )}
    </Card>
  );
}

function formatDate(iso: string): string {
  return new Date(iso).toLocaleDateString(undefined, {
    year: "numeric",
    month: "short",
    day: "numeric",
  });
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
      footer={
        <Button onClick={onClose}>Done</Button>
      }
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
                      add.mutate(
                        user.id,
                        {
                          onSuccess: () => {
                            toast?.add({ title: "Member added", type: "success" });
                            setAdding(false);
                            pickerPaging.setSearch("");
                          },
                          onError: () =>
                            toast?.add({ title: "Could not add the member", type: "error" }),
                        },
                      )
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
                  <div className="truncate text-sm">
                    {user.display_name || user.email || user.subject}
                  </div>
                  <div className="truncate text-xs text-ink-faint">
                    {user.email ?? user.subject}
                  </div>
                </div>
                <Button
                  variant="ghost"
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
