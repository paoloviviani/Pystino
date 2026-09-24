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
  useDeleteUser,
  useGroups,
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
          <div>{user.display_name || user.username || user.email || user.subject}</div>
          <div className={MUTED}>{user.email ?? user.subject}</div>
          {/* The directory's own name for this person, shown when it is not
              already the line above. An account created in Keycloak as
              `chat@local` with the address `chat@example.org` was listed only
              by the address, so searching for the name it was made under found
              nothing — which reads as a missing account rather than a missing
              label. */}
          {user.username && user.username !== user.email && user.username !== user.display_name ? (
            <div className={`${MUTED} ${CODE}`}>{user.username}</div>
          ) : null}
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
          <Button variant="secondary" onClick={() => setEditing(user)}>
            Edit
          </Button>
          {/* Thin red text, never filled: the same row-level delete as every
              other screen — filled red belongs to the confirm dialog, not to
              a control that sits beside Edit all day. */}
          <Button
            variant="ghost"
            className="text-danger"
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
        subtitle="People arrive at their first sign-in, or before it through directory sync
          (Settings → Identity providers → Directory). The bundled Authelia's people are
          added under People there."
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
              placeholder="email, name, username or identity provider subject"
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


      <EditUserDialog user={editing} onClose={() => setEditing(null)} />

      <DeleteUserDialog user={deleting} onClose={() => setDeleting(null)} />

    </div>
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
 * Four facts shape it:
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
 * - **The profile fields the account carries are editable here, and an edit
 *   pins them.** Sign-in used to refresh email, display name and username
 *   from the directory's claims, silently reverting whatever was corrected.
 *   Now a field edited here is recorded server-side and sign-in stops
 *   touching it — which is what the hint on a directory account's fields
 *   says, because a note that promises less than the behaviour delivers is
 *   how an operator stops reading the notes.
 */
function EditUserDialog({ user, onClose }: { user: AdminUser | null; onClose: () => void }) {
  const update = useUpdateUser();
  const toast = useOptionalToast();

  const [isActive, setIsActive] = useState(user?.is_active ?? true);
  const [isAdmin, setIsAdmin] = useState(user?.is_admin ?? false);
  const [email, setEmail] = useState(user?.email ?? "");
  const [displayName, setDisplayName] = useState(user?.display_name ?? "");
  const [username, setUsername] = useState(user?.username ?? "");

  // Re-seed the fields when a different user opens: the dialog is keyed by
  // remount at the call site in spirit, but state here must follow the row.
  const userKey = user?.id ?? "none";
  const [seededFor, setSeededFor] = useState(userKey);
  if (seededFor !== userKey) {
    setSeededFor(userKey);
    setIsActive(user?.is_active ?? true);
    setIsAdmin(user?.is_admin ?? false);
    setEmail(user?.email ?? "");
    setDisplayName(user?.display_name ?? "");
    setUsername(user?.username ?? "");
  }

  const close = () => {
    update.reset();
    onClose();
  };

  // Only what changed travels. The gateway records a sent profile field as
  // administrator-edited and stops refreshing it from the directory — sending
  // an untouched field under a "no change" save would silently detach it.
  const profileChanges: {
    email?: string | null;
    display_name?: string | null;
    username?: string | null;
  } = {};
  if (user !== null) {
    if (email !== (user.email ?? "")) profileChanges.email = email.trim() || null;
    if (displayName !== (user.display_name ?? ""))
      profileChanges.display_name = displayName.trim() || null;
    if (username !== (user.username ?? "")) profileChanges.username = username.trim() || null;
  }
  const profileDirty = Object.keys(profileChanges).length > 0;
  const dirty =
    user !== null &&
    (isActive !== user.is_active || isAdmin !== user.is_admin || profileDirty);

  const save = () =>
    user &&
    update.mutate(
      { id: user.id, is_active: isActive, is_admin: isAdmin, ...profileChanges },
      {
        onSuccess: () => toast?.add({ title: "User updated", type: "success" }),
        onError: (caught: unknown) =>
          toast?.add({
            title: caught instanceof Error ? caught.message : "Could not update the user",
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
          <Button variant="primary" disabled={!dirty} busy={update.isPending} onClick={save}>
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
          </div>
          {/* Read-only, and named as what it is: (issuer, subject) is the
              login identity — the pair every key, session and ledger row keys
              on. It looks editable-adjacent sitting in a form, so the line
              says it is not, rather than letting somebody find out from a
              silent no-op. */}
          <p className="mt-1 text-xs text-ink-faint">
            {user && (
              <span className={CODE}>
                {user.issuer} / {user.subject}
              </span>
            )}{" "}
            — the login identity. Not editable here.
          </p>
        </div>

        <div>
          <div className="text-xs font-medium tracking-[0.01em] text-ink-muted">Profile</div>
          <div className="mt-1 space-y-3">
            <Input
              label="Email"
              type="email"
              value={email}
              disabled={!user}
              onChange={(event) => setEmail(event.target.value)}
              hint={
                isLocal
                  ? "Where mail about the account goes. A local account signs in with its identity, not this address."
                  : "The identity provider refreshes this at sign-in. Editing it records your value, and sign-in stops changing it."
              }
            />
            <Input
              label="Display name"
              value={displayName}
              disabled={!user}
              onChange={(event) => setDisplayName(event.target.value)}
              hint={
                isLocal
                  ? undefined
                  : "Refreshed at sign-in from the identity provider, unless edited here."
              }
            />
            <Input
              label="Username"
              value={username}
              disabled={!user}
              onChange={(event) => setUsername(event.target.value)}
              hint={
                isLocal
                  ? undefined
                  : "The directory's own name for the person (`preferred_username`). Follows the directory unless edited here."
              }
            />
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
              /* Says what actually happens now, which is the opposite of what
                 this used to say. Granting adds a *manual* membership of the
                 admin group, and a directory sync never removes one — so the
                 decision survives the next login rather than being undone by
                 it. Revoking is the narrower half: a membership the directory
                 granted has to be withdrawn there, and the API says so. */
              <span className="mt-0.5 block text-xs text-ink-faint">
                Granting this adds them to the admin group here, and it survives
                their next sign-in. Removing it works only if this console
                granted it — admin the identity provider granted has to be
                withdrawn there.
              </span>
            )}
          </span>
        </label>

        <p className="m-0 text-sm text-ink-muted">
          Passwords belong to the identity provider; for the bundled Authelia they are managed
          under Settings → Identity providers → People.
        </p>
      </div>
    </Dialog>
  );
}
