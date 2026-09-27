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
  useDeletePreview,
  useDeleteUser,
  useIdentityEvents,
  useIdentityProviders,
  useMergePreview,
  useMergeUser,
  usePendingErasures,
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
 * row offers Reset password instead of Create sign-in (§8.1). The gateway
 * states the bundled login outright (`bundled_login`) — the directory entry
 * bound to this user at the enabled bundled provider — because a person the
 * console created has a login from the moment of creation, while still
 * pending: no issuer of their own and no linked identity, so the pair those
 * two fields used to answer from wrongly read "no login", offered Create
 * sign-in, and a second acceptance gave one person two working passwords.
 * The issuer check stays as a fallback for an answer from a gateway predating
 * the field. */
function hasBundledLogin(user: AdminUser, bundled: IdentityProvider | undefined): boolean {
  if (!bundled) return false;
  if (user.bundled_login !== null) return true;
  return user.issuer === bundled.issuer || user.linked_identities.includes(bundled.issuer);
}

/**
 * The login a new sign-in starts from: the email's local part, made into a
 * name the sign-in page accepts. The derivation rules are `deploy/admin.py`'s
 * `_derive_login`'s, replicated here because the console cannot import them —
 * lowercase, then anything outside [a-z0-9._-] becomes "-", leading
 * separators stripped, a leading non-alphanumeric prefixed with "u", and a
 * 64-character cap. The half that checks the name is not already taken in the
 * users file stays server-side, where the file is.
 */
function deriveLogin(email: string): string {
  const local = email.split("@", 1)[0] || "user";
  const derived = local.trim().toLowerCase().replace(/[^a-z0-9._-]/g, "-");
  const stripped = derived.replace(/^[-._]+/, "") || "user";
  const prefixed = /^[a-z0-9]/.test(stripped) ? stripped : `u${stripped}`;
  return prefixed.slice(0, 64);
}

/** The sign-in page's own shape, as the field's standing help text. */
const LOGIN_HINT = "The name they'll type on the sign-in page. Lowercase letters, digits, '.', '_' or '-'.";

/** The gateway refuses a login another person already holds — from the users
 * file ("{login} already exists") or the directory mirror ("'{login}' already
 * exists."). Either way the sentence an administrator needs is the same. */
function takenName(error: unknown, login: string): string | null {
  const message = error instanceof Error ? error.message : "";
  if (!login.trim() || !message.includes("already exists")) return null;
  return `'${login.trim()}' is already another person's sign-in name — pick a different one.`;
}


export function AdminUsers() {
  // Searched and paged on the server. It used to filter in the browser over
  // whatever the endpoint had returned, which reads the same until the
  // organisation outgrows one response — at which point the box quietly
  // searches the first page and reports nothing found.
  const paged = usePaginated();
  const users = useUsers(paged.page);
  const update = useUpdateUser();
  const pendingErasures = usePendingErasures();
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
          {/* The login this person answers through at the bundled provider
              (§8): the name an administrator needs when sending a one-time
              password, named as what it is rather than left to be recognised
              inside the username line, which the person's directory entry may
              or may not share. */}
          {user.bundled_login ? (
            <div className={`${MUTED} ${CODE}`}>sign-in: {user.bundled_login}</div>
          ) : null}
          {/* The directory's own name for this person, shown when it is not
              already the line above. An account created in Keycloak as
              `chat@local` with the address `chat@example.org` was listed only
              by the address, so searching for the name it was made under found
              nothing — which reads as a missing account rather than a missing
              label. Suppressed when it is the bundled login: the sign-in line
              above already carries that exact value. */}
          {user.username &&
          user.username !== user.email &&
          user.username !== user.display_name &&
          user.username !== user.bundled_login ? (
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

      {pendingErasures.data && pendingErasures.data.pending > 0 && (
        <Notice tone="info" title="Erasures waiting for the chat">
          {pendingErasures.data.pending} erasure
          {pendingErasures.data.pending === 1 ? "" : "s"} waiting for the chat to confirm. They
          retry automatically; nothing to do here.
        </Notice>
      )}

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
  const [confirmSharedLoss, setConfirmSharedLoss] = useState(false);
  const preview = useDeletePreview(user?.id);
  const remove = useDeleteUser();
  const toast = useOptionalToast();

  const close = () => {
    setConfirmSharedLoss(false);
    onClose();
  };

  return (
    <Dialog
      open={user !== null}
      title={`Delete ${user?.display_name || user?.email || user?.subject || "this account"}?`}
      onClose={close}
      footer={
        <>
          <Button onClick={close}>Cancel</Button>
          <Button
            variant="danger"
            disabled={Boolean(preview.data?.shared_with_others) && !confirmSharedLoss}
            busy={remove.isPending}
            onClick={() =>
              user &&
              remove.mutate(
                { id: user.id, confirmSharedLoss },
                {
                  onSuccess: (result) => {
                    toast?.add({
                      title: "Account deleted",
                      description: result.chat_erasure_done
                        ? "The chat confirmed erasure immediately."
                        : "The chat could not be reached; erasure is queued and will retry.",
                      type: "success",
                    });
                    close();
                  },
                  onError: () =>
                    toast?.add({ title: "Could not delete the account", type: "error" }),
                }
              )
            }
          >
            Delete permanently
          </Button>
        </>
      }
    >
      {preview.isPending ? (
        <Spinner label="Loading preview" />
      ) : preview.error ? (
        <Notice tone="danger" title="Could not load the preview">
          {preview.error instanceof Error ? preview.error.message : "Unknown error."}
        </Notice>
      ) : preview.data ? (
        <div className={FORM}>
          <ul className={CHECK_ITEM}>
            {Object.entries(preview.data.gateway_counts)
              .filter(([, count]) => count > 0)
              .map(([table, count]) => (
                <li key={table}>
                  {count} {table.replace(/_/g, " ")}
                </li>
              ))}
          </ul>
          {preview.data.bundled_login && (
            <p className="text-sm text-ink-muted">
              Bundled login removed: {preview.data.bundled_login}
            </p>
          )}
          {!preview.data.chat_reachable ? (
            <Notice tone="warn" title="Chat counts unavailable">
              The erasure will be queued and retried.
            </Notice>
          ) : (
            <ul className={CHECK_ITEM}>
              {Object.entries(preview.data.chat_counts ?? {})
                .filter(([, count]) => count > 0)
                .map(([collection, count]) => (
                  <li key={collection}>
                    {count} {collection.replace(/([A-Z])/g, " $1").toLowerCase()}
                  </li>
                ))}
            </ul>
          )}
          <p>
            The account's keys stop working immediately and cannot be restored. Their
            quota and redaction rules keep their scope but stop matching anyone until
            you delete or re-point them.
          </p>
          <p className="text-sm text-ink-muted">
            Past usage stays in the ledger, attributed to their groups as it was
            billed — only the name on the per-user breakdown goes.
          </p>
          {preview.data.shared_with_others && (
            <>
              {preview.data.shared.length > 0 && (
                <ul className={CHECK_ITEM}>
                  {preview.data.shared.map((resource) => (
                    <li key={`${resource.kind}:${resource.id}`}>
                      {resource.title} — {resource.audience}
                    </li>
                  ))}
                </ul>
              )}
              <label className={CHECK_ITEM}>
                <input
                  type="checkbox"
                  checked={confirmSharedLoss}
                  onChange={(event) => setConfirmSharedLoss(event.target.checked)}
                />
                <span>
                  This account has content shared with others (
                  {preview.data.chat_unattributed_legacy_shares > 0
                    ? "including some that can no longer be attributed to anyone"
                    : "shared with other people, named above"}
                  ). I understand it will disappear for them too.
                </span>
              </label>
            </>
          )}
        </div>
      ) : null}
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
          {preview.data.duplicate_rules_dropped > 0 && (
            <p className="text-sm text-ink-muted">
              {preview.data.duplicate_rules_dropped} duplicate rule
              {preview.data.duplicate_rules_dropped > 1 ? "s" : ""} dropped: the target already has
              an equivalent one, so the source's is kept off rather than moved.
            </p>
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
 * choose how it reaches them and what happens after. Both halves of what to
 * send are named — the login and the password — because a password without
 * the name it unlocks is half a message.
 */
function MintedPasswordNotice({ login, password }: { login: string; password: string }) {
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
      <p>
        Sign in as <strong>{login}</strong> with this password:
      </p>
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
  // The login prefills from the email until the administrator edits it by
  // hand; after that the typed value wins and later keystrokes in the email
  // stop overwriting it.
  const [loginByHand, setLoginByHand] = useState(false);
  const [displayName, setDisplayName] = useState("");
  const [email, setEmail] = useState("");
  const [groups, setGroups] = useState("");

  const close = () => {
    create.reset();
    setLogin("");
    setLoginByHand(false);
    setDisplayName("");
    setEmail("");
    setGroups("");
    onClose();
  };

  const created = create.data;
  const taken = takenName(create.error, login);

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
          {taken ?? (create.error instanceof Error ? create.error.message : "Unknown error.")}
        </Notice>
      ) : null}

      {created ? (
        <MintedPasswordNotice login={created.bundled_login ?? login.trim()} password={created.password} />
      ) : (
        <div className={FORM}>
          {/* Email sits above Login because Login derives from it: the
              prefill has to be visible happening, in the direction forms are
              read, not appear retroactively in a field above the one typed. */}
          <Input
            label="Email"
            type="email"
            value={email}
            onChange={(event) => {
              setEmail(event.target.value);
              if (!loginByHand) setLogin(deriveLogin(event.target.value));
            }}
          />
          <Input
            label="Login"
            value={login}
            onChange={(event) => {
              setLogin(event.target.value);
              setLoginByHand(true);
            }}
            hint={LOGIN_HINT}
          />
          <Input
            label="Display name"
            value={displayName}
            onChange={(event) => setDisplayName(event.target.value)}
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

  // Prefilled from the person's email's local part, then left editable — the
  // same derivation Add user uses, so both doors suggest the same name.
  const userKey = user?.id ?? "none";
  const [seededFor, setSeededFor] = useState(userKey);
  if (seededFor !== userKey) {
    setSeededFor(userKey);
    setLogin(user ? deriveLogin(user.email ?? "") : "");
  }

  const close = () => {
    create.reset();
    setLogin("");
    onClose();
  };

  const created = create.data;
  const taken = takenName(create.error, login);

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
          {taken ?? (create.error instanceof Error ? create.error.message : "Unknown error.")}
        </Notice>
      ) : null}

      {created ? (
        <MintedPasswordNotice login={created.bundled_login ?? login.trim()} password={created.password} />
      ) : (
        <Input
          label="Login"
          value={login}
          onChange={(event) => setLogin(event.target.value)}
          hint={LOGIN_HINT}
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
        <MintedPasswordNotice login={user?.bundled_login ?? user?.username ?? ""} password={result.password} />
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
