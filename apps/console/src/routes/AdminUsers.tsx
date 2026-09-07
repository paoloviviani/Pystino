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
  useClearUserPassword,
  useCreateUser,
  useDeleteUser,
  useGroups,
  useSetUserPassword,
  useUpdateUser,
  useUsers,
} from "../lib/admin";
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

  const [creating, setCreating] = useState(false);
  const [editing, setEditing] = useState<AdminUser | null>(null);
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
          {/* A linked account answers to a directory as well as to its own
              key (ADR 0056). Shown here rather than in a column of its own
              because it is the same question this cell already answers: which
              issuer, or issuers, name this person. */}
          {user.linked_identities.map((issuer) => (
            <div key={issuer} className={`${MUTED} ${CODE}`}>
              + {issuer}
            </div>
          ))}
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
          <Button variant="primary" onClick={() => setEditing(user)}>
            Edit
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

      <EditUserDialog user={editing} onClose={() => setEditing(null)} />

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

/**
 * Everything an administrator may change about one account, in one place
 * instead of a scattered Enable button and a password endpoint with no door.
 *
 * Three facts shape it:
 *
 * - **The issuer decides the offers.** A local account can have its password
 *   set, cleared, and re-set here; a directory account's credentials belong
 *   to its IdP (ADR 0049), so the password section is simply absent — not
 *   disabled, absent. The issuer is shown either way, because "why is there
 *   no password box" is a question the answer should preempt.
 * - **Admin can follow the IdP.** When the deployment maps admin groups, the
 *   flag on a directory account is rewritten at the next login (the API
 *   refuses the change); the note says so beside the checkbox rather than
 *   letting a 400 be the explanation.
 * - **Password changes take effect immediately** and separately from the
 *   status toggle: an administrator resetting a locked-out account should
 *   not have to also review flags to do it.
 */
function EditUserDialog({ user, onClose }: { user: AdminUser | null; onClose: () => void }) {
  const update = useUpdateUser();
  const setPassword = useSetUserPassword();
  const clearPassword = useClearUserPassword();
  const toast = useOptionalToast();

  const [isActive, setIsActive] = useState(user?.is_active ?? true);
  const [isAdmin, setIsAdmin] = useState(user?.is_admin ?? false);
  const [newPassword, setNewPassword] = useState("");

  // Re-seed the toggles when a different user opens: the dialog is keyed by
  // remount at the call site in spirit, but state here must follow the row.
  const userKey = user?.id ?? "none";
  const [seededFor, setSeededFor] = useState(userKey);
  if (seededFor !== userKey) {
    setSeededFor(userKey);
    setIsActive(user?.is_active ?? true);
    setIsAdmin(user?.is_admin ?? false);
    setNewPassword("");
  }

  const close = () => {
    setNewPassword("");
    update.reset();
    setPassword.reset();
    clearPassword.reset();
    onClose();
  };

  const dirty = user !== null && (isActive !== user.is_active || isAdmin !== user.is_admin);

  const saveFlags = () =>
    user &&
    update.mutate(
      { id: user.id, is_active: isActive, is_admin: isAdmin },
      {
        onSuccess: () => toast?.add({ title: "User updated", type: "success" }),
        onError: (caught: unknown) =>
          toast?.add({
            title: caught instanceof Error ? caught.message : "Could not update the user",
            type: "error",
          }),
      },
    );

  const setNewPasswordForUser = () =>
    user &&
    setPassword.mutate(
      { id: user.id, password: newPassword },
      {
        onSuccess: () => {
          toast?.add({
            title: "Password set — hand it to the person over a channel you trust",
            type: "success",
          });
          setNewPassword("");
        },
        onError: (caught: unknown) =>
          toast?.add({
            title: caught instanceof Error ? caught.message : "Could not set the password",
            type: "error",
          }),
      },
    );

  const isLocal = user?.issuer === "local";

  return (
    <Dialog
      open={user !== null}
      title={user ? `Edit — ${user.display_name || user.email || user.subject}` : "Edit"}
      onClose={close}
      footer={
        <>
          <Button onClick={close}>Close</Button>
          <Button variant="primary" disabled={!dirty} busy={update.isPending} onClick={saveFlags}>
            Save changes
          </Button>
        </>
      }
    >
      {update.error ? (
        <Notice tone="danger" title="Could not update the user">
          {update.error instanceof Error ? update.error.message : "Unknown error."}
        </Notice>
      ) : null}

      <div className={FORM}>
        <div>
          <div className="text-xs font-medium tracking-[0.01em] text-ink-muted">Identity</div>
          <div className="mt-1 flex flex-wrap items-center gap-2">
            {user?.is_admin && <Badge tone="accent">Administrator</Badge>}
            {user?.is_active ? (
              <Badge tone="ok">Active</Badge>
            ) : (
              <Badge tone="danger">Disabled</Badge>
            )}
            <Badge tone={isLocal ? "neutral" : "warn"}>{isLocal ? "local" : "IdP"}</Badge>
            {!isLocal && user && <span className={CODE}>{user.issuer}</span>}
          </div>
        </div>

        <label className={CHECK_ITEM}>
          <input
            type="checkbox"
            checked={isActive}
            disabled={!user}
            onChange={(event) => setIsActive(event.target.checked)}
          />
          <span>
            Account active
            <span className="mt-0.5 block text-xs text-ink-faint">
              A disabled account cannot sign in, and its keys stop admitting
              requests.
            </span>
          </span>
        </label>

        <label className={CHECK_ITEM}>
          <input
            type="checkbox"
            checked={isAdmin}
            disabled={!user}
            onChange={(event) => setIsAdmin(event.target.checked)}
          />
          <span>
            Administrator
            {!isLocal && (
              <span className="mt-0.5 block text-xs text-ink-faint">
                For an identity-provider account this follows group membership at
                the next login, if admin groups are mapped.
              </span>
            )}
          </span>
        </label>

        {isLocal ? (
          <div>
            <div className="text-xs font-medium tracking-[0.01em] text-ink-muted">Password</div>
            <div className="mt-1 flex flex-wrap items-end gap-2">
              <div className="min-w-56 flex-1">
                <Input
                  label="Set a new password"
                  type="password"
                  value={newPassword}
                  onChange={(event) => setNewPassword(event.target.value)}
                  placeholder="typed once, by you, for them"
                />
              </div>
              <Button
                busy={setPassword.isPending}
                disabled={newPassword.length === 0}
                onClick={setNewPasswordForUser}
              >
                Set password
              </Button>
              {user?.has_password && (
                <Button
                  variant="ghost"
                  busy={clearPassword.isPending}
                  onClick={() =>
                    user &&
                    clearPassword.mutate(user.id, {
                      onSuccess: () => toast?.add({ title: "Password removed", type: "success" }),
                      onError: (caught: unknown) =>
                        toast?.add({
                          title:
                            caught instanceof Error
                              ? caught.message
                              : "Could not remove the password",
                          type: "error",
                        }),
                    })
                  }
                >
                  Remove password
                </Button>
              )}
            </div>
            <p className="mt-1 text-xs text-ink-faint">
              The person can change it themselves from their account menu.
            </p>
          </div>
        ) : (
          <p className="m-0 text-sm text-ink-muted">
            Password, if any, is managed by the identity provider above.
          </p>
        )}
      </div>
    </Dialog>
  );
}
