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
  useCreateBundledSignIn,
  useCreateBundledUser,
  useDeleteUser,
  useIdentityEvents,
  useIdentityProviders,
  useMergePreview,
  useMergeUser,
  useResetBundledPassword,
  useUpdateUser,
  useUsers,
} from "../lib/admin";
import {
  CHECK_ITEM,
  CHIPS,
  CODE,
  FORM,
  MUTED,
  PAGE,
  ROW_ACTIONS,
  SECRET,
  SECRET_DETAIL,
  SECRET_ROW,
} from "../lib/layout";
import { usePaginated } from "../lib/paging";
import type { AdminUser, IdentityProvider } from "../lib/types";
import { useOptionalToast } from "../lib/toast";
import { PageHeader } from "../components/PageHeader";

/**
 * The one enabled provider, when it is the bundled Authelia (ADR 0093 §2:
 * exactly one row is ever enabled). Every bundled-only action on this page —
 * Add user, Create sign-in, Reset password — reads this rather than a row id,
 * since there is never more than one to act on.
 */
function useBundledProvider(): IdentityProvider | undefined {
  const providers = useIdentityProviders();
  return providers.data?.find((provider) => provider.is_enabled && provider.kind === "authelia");
}

/** Whether this person can already sign in through the bundled row, so the
 * row offers Reset password instead of Create sign-in (§8.1). Bundled login
 * is not a fact `UserAdminResponse` states directly — the account's own
 * identity pair, or one of its linked ones, naming the bundled issuer is what
 * `bind_bundled_login` itself tests, so this mirrors that rather than
 * inventing a second answer to the same question. */
function hasBundledLogin(user: AdminUser, bundled: IdentityProvider | undefined): boolean {
  if (!bundled) return false;
  return user.issuer === bundled.issuer || user.linked_identities.includes(bundled.issuer);
}


export function AdminUsers() {
  // Searched and paged on the server. It used to filter in the browser over
  // whatever the endpoint had returned, which reads the same until the
  // organisation outgrows one response — at which point the box quietly
  // searches the first page and reports nothing found.
  const paged = usePaginated();
  const users = useUsers(paged.page);
  const update = useUpdateUser();
  const remove = useDeleteUser();
  const bundled = useBundledProvider();
  const providers = useIdentityProviders();
  // Only meaningful once the identity providers have loaded — before that,
  // "no bundled provider" and "haven't checked yet" would otherwise look
  // identical, hiding every bundled action for a moment on every page load.
  const knowsProviders = providers.data !== undefined;

  const [editing, setEditing] = useState<AdminUser | null>(null);
  const [deleting, setDeleting] = useState<AdminUser | null>(null);
  const [adding, setAdding] = useState(false);
  const [signingIn, setSigningIn] = useState<AdminUser | null>(null);
  const [resetting, setResetting] = useState<AdminUser | null>(null);
  const [togglingActive, setTogglingActive] = useState<AdminUser | null>(null);
  const [viewingActivity, setViewingActivity] = useState<AdminUser | null>(null);
  const [merging, setMerging] = useState<AdminUser | null>(null);

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
          <Button variant="ghost" onClick={() => setViewingActivity(user)}>
            Activity
          </Button>
          {/* Bundled-only (§8.1): a login belongs to the bundled Authelia, so
              neither action means anything against an external IdP, which
              owns its own accounts entirely. */}
          {knowsProviders && bundled ? (
            hasBundledLogin(user, bundled) ? (
              <Button variant="ghost" onClick={() => setResetting(user)}>
                Reset password
              </Button>
            ) : (
              <Button variant="ghost" onClick={() => setSigningIn(user)}>
                Create sign-in
              </Button>
            )
          ) : null}
          <Button variant="ghost" onClick={() => setTogglingActive(user)}>
            {user.is_active ? "Disable" : "Enable"}
          </Button>
          <Button variant="ghost" onClick={() => setMerging(user)}>
            Merge into…
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
        subtitle={
          knowsProviders && bundled
            ? "People arrive at their first sign-in, or you can add one directly below."
            : "Accounts live at the identity provider — this deployment cannot add a login or reset its password here. Disable and Enable still apply on the gateway side."
        }
        actions={
          knowsProviders && bundled ? <Button onClick={() => setAdding(true)}>Add user</Button> : null
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

      <AddUserDialog open={adding} onClose={() => setAdding(false)} />

      <CreateSignInDialog user={signingIn} onClose={() => setSigningIn(null)} />

      <ResetPasswordDialog user={resetting} onClose={() => setResetting(null)} />

      <DisableEnableDialog
        user={togglingActive}
        bundled={bundled}
        onClose={() => setTogglingActive(null)}
      />

      <ActivityDialog user={viewingActivity} onClose={() => setViewingActivity(null)} />

      <MergeUserDialog user={merging} onClose={() => setMerging(null)} />

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

/**
 * Merge one account into another (ADR 0093 §7.1). Irreversible, so the
 * shape follows the design's own: a target picker, then the preview (once
 * both ids are known), then a typed confirmation of the *source's* address
 * (or its id, if it has none) rather than a checkbox, plus a reason.
 */
function MergeUserDialog({ user, onClose }: { user: AdminUser | null; onClose: () => void }) {
  const [targetQuery, setTargetQuery] = useState("");
  const [targetId, setTargetId] = useState<string | undefined>(undefined);
  const [confirm, setConfirm] = useState("");
  const [reason, setReason] = useState("");
  const toast = useOptionalToast();

  const candidates = useUsers({ q: targetQuery, limit: 5 }, targetQuery.trim().length > 1);
  const preview = useMergePreview(user?.id, targetId);
  const merge = useMergeUser();

  const reset = () => {
    setTargetQuery("");
    setTargetId(undefined);
    setConfirm("");
    setReason("");
  };

  const expectedConfirm = user?.email || user?.id || "";
  const target = candidates.data?.items.find((candidate) => candidate.id === targetId);

  return (
    <Dialog
      open={user !== null}
      title={`Merge ${user?.display_name || user?.email || "this account"} into…`}
      onClose={() => {
        reset();
        onClose();
      }}
      footer={
        <>
          <Button
            onClick={() => {
              reset();
              onClose();
            }}
          >
            Cancel
          </Button>
          <Button
            variant="danger"
            disabled={!user || !targetId || confirm !== expectedConfirm || !reason.trim()}
            busy={merge.isPending}
            onClick={() =>
              user &&
              targetId &&
              merge.mutate(
                { sourceId: user.id, into: targetId, confirm, reason },
                {
                  onSuccess: () => {
                    toast?.add({ title: "Accounts merged", type: "success" });
                    reset();
                    onClose();
                  },
                  onError: (error) =>
                    toast?.add({
                      title: "Could not merge",
                      description: error instanceof Error ? error.message : "Unknown error.",
                      type: "error",
                    }),
                }
              )
            }
          >
            Merge, irreversibly
          </Button>
        </>
      }
    >
      <p>
        Moves every membership, key, ledger row and identity of{" "}
        <strong>{user?.email || user?.display_name || "this account"}</strong> onto the target,
        then deletes it. There is no undo but restoring a backup taken beforehand (the
        README's volume tar) — take one now if you have not today.
      </p>

      {!targetId ? (
        <div className={FORM}>
          <Input
            label="Merge into…"
            value={targetQuery}
            onChange={(event) => setTargetQuery(event.target.value)}
            placeholder="Search by email, name or username"
            autoFocus
          />
          {candidates.data && targetQuery.trim().length > 1 ? (
            <div className="flex flex-col gap-1">
              {candidates.data.items
                .filter((candidate) => candidate.id !== user?.id)
                .map((candidate) => (
                  <Button
                    key={candidate.id}
                    variant="secondary"
                    onClick={() => setTargetId(candidate.id)}
                  >
                    {candidate.display_name || candidate.email || candidate.subject}
                    {candidate.email ? ` — ${candidate.email}` : ""}
                  </Button>
                ))}
              {candidates.data.items.length === 0 && (
                <p className="text-sm text-ink-muted">No match.</p>
              )}
            </div>
          ) : null}
        </div>
      ) : preview.isPending ? (
        <Spinner label="Loading preview" />
      ) : preview.error ? (
        <Notice tone="danger" title="Could not preview this merge">
          {preview.error instanceof Error ? preview.error.message : "Unknown error."}
        </Notice>
      ) : preview.data ? (
        <div className={FORM}>
          <p className="text-sm text-ink-muted">
            Into {target?.display_name || target?.email || preview.data.target_id}
          </p>
          <ul className={CHECK_ITEM}>
            {Object.entries(preview.data.counts)
              .filter(([, count]) => count > 0)
              .map(([table, count]) => (
                <li key={table}>
                  {count} {table.replace(/_/g, " ")}
                </li>
              ))}
          </ul>
          {preview.data.identities_dropped.length > 0 && (
            <Notice tone="warn" title="These identities are dropped, not moved">
              The target already has an identity at the same issuer:{" "}
              {preview.data.identities_dropped.map((i) => i.issuer).join(", ")}.
            </Notice>
          )}
          {preview.data.bundled_logins_disabled.length > 0 && (
            <p className="text-sm text-ink-muted">
              Bundled login{preview.data.bundled_logins_disabled.length > 1 ? "s" : ""} disabled:{" "}
              {preview.data.bundled_logins_disabled.join(", ")}
            </p>
          )}
          <p className="text-sm text-ink-muted">
            Resulting administrator flag: {preview.data.resulting_is_admin ? "yes" : "no"}. {preview.data.chat_note}.
          </p>
          <Input
            label={`Type "${expectedConfirm}" to confirm`}
            value={confirm}
            onChange={(event) => setConfirm(event.target.value)}
          />
          <Input
            label="Reason"
            value={reason}
            onChange={(event) => setReason(event.target.value)}
            placeholder="Why these are the same person"
          />
        </div>
      ) : null}
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
 * The one-time password, shown once, with the note §8.1 requires: Authelia
 * cannot force a change at the person's first sign-in, so the operator has to
 * choose how it reaches them and what happens after.
 */
function MintedPasswordNotice({ password }: { password: string }) {
  const [copied, setCopied] = useState<boolean | null>(null);

  const copy = async () => {
    try {
      await navigator.clipboard.writeText(password);
      setCopied(true);
    } catch {
      setCopied(false);
    }
  };

  return (
    <>
      <div className={SECRET_ROW}>
        <code className={SECRET}>{password}</code>
        <Button onClick={copy}>{copied ? "Copied" : "Copy"}</Button>
      </div>
      {copied === false && (
        <p className="text-sm text-warn">Could not reach the clipboard; copy it by hand.</p>
      )}
      <p className={SECRET_DETAIL}>
        Share it over a one-time channel (a password manager share, or in person). Authelia
        can't force a change at first sign-in; ask them to change it from the sign-in page's
        reset link (needs SMTP) or keep it.
      </p>
    </>
  );
}

/**
 * Add a person to the bundled directory (§8.1/§8.2). Groups here are console
 * groups — manual memberships — never the Authelia file's own, which is
 * always `["users"]` (§8.3): editing it stopped being on offer at all.
 */
function AddUserDialog({ open, onClose }: { open: boolean; onClose: () => void }) {
  const create = useCreateBundledUser();
  const [login, setLogin] = useState("");
  const [displayName, setDisplayName] = useState("");
  const [email, setEmail] = useState("");
  const [groups, setGroups] = useState("");

  const close = () => {
    create.reset();
    setLogin("");
    setDisplayName("");
    setEmail("");
    setGroups("");
    onClose();
  };

  const created = create.data;

  return (
    <Dialog
      open={open}
      title="Add user"
      onClose={close}
      footer={
        created ? (
          <Button variant="primary" onClick={close}>
            Done
          </Button>
        ) : (
          <>
            <Button onClick={close}>Cancel</Button>
            <Button
              variant="primary"
              disabled={!login.trim() || !email.trim()}
              busy={create.isPending}
              onClick={() =>
                create.mutate({
                  login: login.trim(),
                  display_name: displayName.trim() || undefined,
                  email: email.trim(),
                  groups: groups
                    .split(",")
                    .map((g) => g.trim())
                    .filter(Boolean),
                })
              }
            >
              Add user
            </Button>
          </>
        )
      }
    >
      {create.error ? (
        <Notice tone="danger" title="Could not add the user">
          {create.error instanceof Error ? create.error.message : "Unknown error."}
        </Notice>
      ) : null}

      {created ? (
        <MintedPasswordNotice password={created.password} />
      ) : (
        <div className={FORM}>
          <Input
            label="Login"
            value={login}
            onChange={(event) => setLogin(event.target.value)}
            hint="The name this person signs in with."
          />
          <Input
            label="Display name"
            value={displayName}
            onChange={(event) => setDisplayName(event.target.value)}
          />
          <Input
            label="Email"
            type="email"
            value={email}
            onChange={(event) => setEmail(event.target.value)}
          />
          <Input
            label="Groups"
            value={groups}
            onChange={(event) => setGroups(event.target.value)}
            placeholder="comma separated"
            hint="Console groups — the same manual membership an administrator grants anywhere else."
          />
        </div>
      )}
    </Dialog>
  );
}

/**
 * A bundled login for an existing gateway user who has none — the
 * after-switch and after-break-glass case (§8.1).
 */
function CreateSignInDialog({ user, onClose }: { user: AdminUser | null; onClose: () => void }) {
  const create = useCreateBundledSignIn();
  const [login, setLogin] = useState("");

  const close = () => {
    create.reset();
    setLogin("");
    onClose();
  };

  const created = create.data;

  return (
    <Dialog
      open={user !== null}
      title={`Create sign-in — ${user?.display_name || user?.email || user?.subject}`}
      onClose={close}
      footer={
        created ? (
          <Button variant="primary" onClick={close}>
            Done
          </Button>
        ) : (
          <>
            <Button onClick={close}>Cancel</Button>
            <Button
              variant="primary"
              disabled={!login.trim()}
              busy={create.isPending}
              onClick={() => user && create.mutate({ userId: user.id, login: login.trim() })}
            >
              Create sign-in
            </Button>
          </>
        )
      }
    >
      {create.error ? (
        <Notice tone="danger" title="Could not create the sign-in">
          {create.error instanceof Error ? create.error.message : "Unknown error."}
        </Notice>
      ) : null}

      {created ? (
        <MintedPasswordNotice password={created.password} />
      ) : (
        <Input
          label="Login"
          value={login}
          onChange={(event) => setLogin(event.target.value)}
          hint="The name this person will sign in with. Their existing account, groups and history are unchanged."
        />
      )}
    </Dialog>
  );
}

/** Mints a fresh one-time password for the user's bundled login (§8.1). */
function ResetPasswordDialog({ user, onClose }: { user: AdminUser | null; onClose: () => void }) {
  const reset = useResetBundledPassword();

  const close = () => {
    reset.reset();
    onClose();
  };

  const result = reset.data;

  return (
    <Dialog
      open={user !== null}
      title={`Reset password — ${user?.display_name || user?.email || user?.subject}`}
      onClose={close}
      footer={
        result ? (
          <Button variant="primary" onClick={close}>
            Done
          </Button>
        ) : (
          <>
            <Button onClick={close}>Cancel</Button>
            <Button
              variant="primary"
              busy={reset.isPending}
              onClick={() => user && reset.mutate(user.id)}
            >
              Reset password
            </Button>
          </>
        )
      }
    >
      {reset.error ? (
        <Notice tone="danger" title="Could not reset the password">
          {reset.error instanceof Error ? reset.error.message : "Unknown error."}
        </Notice>
      ) : null}

      {result ? (
        <MintedPasswordNotice password={result.password} />
      ) : (
        <p>
          This mints a new one-time password for their bundled login. The old one stops
          working immediately.
        </p>
      )}
    </Dialog>
  );
}

/**
 * Disable and Enable, as their own dialog rather than a checkbox in Edit —
 * the consequences are the whole point of asking first (ADR 0093 §9.1).
 */
function DisableEnableDialog({
  user,
  bundled,
  onClose,
}: {
  user: AdminUser | null;
  bundled: IdentityProvider | undefined;
  onClose: () => void;
}) {
  const update = useUpdateUser();
  const toast = useOptionalToast();

  const close = () => {
    update.reset();
    onClose();
  };

  // Same shape null-guard as every other dialog here: `open` follows `user`,
  // and everything inside reads it through `?.` rather than returning early,
  // so the hooks above run on every render regardless.
  const disabling = user?.is_active ?? true;
  const verb = disabling ? "Disable" : "Enable";

  const save = () =>
    user &&
    update.mutate(
      { id: user.id, is_active: !user.is_active },
      {
        onSuccess: (updated) => {
          if (updated.authelia_sync === "failed") {
            toast?.add({
              title: updated.authelia_sync_message ?? "The bundled login could not be updated",
              type: "error",
            });
          } else {
            toast?.add({ title: `Account ${disabling ? "disabled" : "enabled"}`, type: "success" });
          }
          close();
        },
        onError: () => toast?.add({ title: `Could not ${verb.toLowerCase()} the account`, type: "error" }),
      },
    );

  return (
    <Dialog
      open={user !== null}
      title={`${verb} ${user?.display_name || user?.email || user?.subject || "this account"}?`}
      onClose={close}
      footer={
        <>
          <Button onClick={close}>Cancel</Button>
          <Button variant={disabling ? "danger" : "primary"} busy={update.isPending} onClick={save}>
            {verb}
          </Button>
        </>
      }
    >
      {update.error ? (
        <Notice tone="danger" title={`Could not ${verb.toLowerCase()} the account`}>
          {update.error instanceof Error ? update.error.message : "Unknown error."}
        </Notice>
      ) : null}

      {disabling ? (
        <div className={FORM}>
          <p>
            Their console session, chat sessions and machine links are refused within about
            a minute. Refresh credentials and minted API keys are revoked at once.
          </p>
          <p>
            Personal API keys are kept but stop admitting requests while the account is
            disabled — they return automatically if you re-enable it.
          </p>
          {user && hasBundledLogin(user, bundled) ? (
            <p>Their bundled Authelia login is disabled too, under the same action.</p>
          ) : (
            <p>
              This does not disable the person at the identity provider — do that there too,
              or they can still authenticate even though the gateway refuses them.
            </p>
          )}
        </div>
      ) : (
        <div className={FORM}>
          <p>
            They can sign in again immediately, and personal API keys admit requests again.
            Sessions, minted keys and devices stay revoked — they sign in again and re-enroll
            rather than resuming automatically.
          </p>
          {user && hasBundledLogin(user, bundled) ? (
            <p>Their bundled Authelia login is re-enabled too, under the same action.</p>
          ) : null}
        </div>
      )}
    </Dialog>
  );
}

/** What happened to this person, and what they did (§8.1): `user_id` matches
 * either side of an `identity_events` row. */
function ActivityDialog({ user, onClose }: { user: AdminUser | null; onClose: () => void }) {
  const events = useIdentityEvents(user?.id ?? null);

  return (
    <Dialog
      open={user !== null}
      title={`Activity — ${user?.display_name || user?.email || user?.subject}`}
      onClose={onClose}
      footer={<Button onClick={onClose}>Close</Button>}
    >
      {events.isPending ? (
        <Spinner label="Loading activity" />
      ) : events.error ? (
        <Notice tone="danger" title="Could not load activity">
          {events.error instanceof Error ? events.error.message : "Unknown error."}
        </Notice>
      ) : events.data && events.data.items.length > 0 ? (
        <ul className="flex flex-col gap-3">
          {events.data.items.map((event) => (
            <li key={event.id} className="border-b border-line pb-2 last:border-none">
              <div className="flex items-center justify-between gap-2">
                <span className={CODE}>{event.action}</span>
                <span className={`${MUTED} text-sm`}>{formatDate(event.at)}</span>
              </div>
              <div className={`${MUTED} text-sm`}>
                {event.actor_label || event.actor_type}
                {event.reason ? ` — ${event.reason}` : ""}
              </div>
            </li>
          ))}
        </ul>
      ) : (
        <p className={MUTED}>No activity recorded yet.</p>
      )}
    </Dialog>
  );
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
  const dirty = user !== null && (isAdmin !== user.is_admin || profileDirty);

  const save = () =>
    user &&
    update.mutate(
      { id: user.id, is_admin: isAdmin, ...profileChanges },
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
