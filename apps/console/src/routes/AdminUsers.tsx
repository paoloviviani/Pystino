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
import { useCreateUser, useDeleteUser, useGroups, useUpdateUser, useUsers } from "../lib/admin";
import {
  CHECK_ITEM,
  CHECK_LIST,
  CHIPS,
  CODE,
  FORM,
  MUTED,
  PAGE,
  ROW_ACTIONS,
} from "../lib/layout";
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
  // The create-account dialog picks from existing groups (and may name new
  // ones); managing them is the Groups screen's job (ADR 0050).
  const groups = useGroups({ limit: 200 });
  const update = useUpdateUser();
  const remove = useDeleteUser();
  const toast = useOptionalToast();

  const [creating, setCreating] = useState(false);
  const [deleting, setDeleting] = useState<AdminUser | null>(null);

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

      <CreateUserDialog
        open={creating}
        groups={groups.data?.items ?? []}
        onClose={() => setCreating(false)}
      />

      <DeleteUserDialog user={deleting} onClose={() => setDeleting(null)} />

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

function formatDate(iso: string): string {
  return new Date(iso).toLocaleDateString(undefined, {
    year: "numeric",
    month: "short",
    day: "numeric",
  });
}
