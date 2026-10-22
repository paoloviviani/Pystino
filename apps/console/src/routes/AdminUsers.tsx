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
  RowActions,
} from "@llmp/ui";
import type { Column } from "@llmp/ui";
import { useState } from "react";
import {
  useCreateBundledSignIn,
  useCreateBundledUser,
  useDeletePreview,
  useDeleteUser,
  useEmailSettings,
  useIdentityEvents,
  useIdentityProviders,
  useInviteText,
  useInviteUser,
  useMergePreview,
  useMergeUser,
  usePendingErasures,
  useResetBundledPassword,
  useUpdateUser,
  useUserStatusCounts,
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
import type { AdminUser, IdentityProvider, UserStatus } from "../lib/types";
import { useOptionalToast } from "../lib/toast";
import { formatNumber, formatDate } from "../lib/format";
import { Message } from "../components/Message";
import { m } from "../paraglide/messages.js";
import { PageHeader } from "../components/PageHeader";

/** How long an invitation stays claimable (ADR 0095) — the marker's clock.
 * Kept in step with the gateway's `INVITE_EXPIRY_DAYS` by hand: the gateway
 * decides, the console only labels. */
const INVITE_EXPIRY_DAYS = 14;

function inviteExpired(user: AdminUser): boolean {
  if (!user.invited_at) return false;
  return Date.now() - new Date(user.invited_at).getTime() > INVITE_EXPIRY_DAYS * 86_400_000;
}

const isInvited = (user: AdminUser): boolean => user.invited_at !== null;

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
const LOGIN_HINT = () => m["admin_users.login_hint"]();

/** The gateway refuses a login another person already holds — from the users
 * file ("{login} already exists") or the directory mirror ("'{login}' already
 * exists."). Either way the sentence an administrator needs is the same. */
function takenName(error: unknown, login: string): string | null {
  const message = error instanceof Error ? error.message : "";
  if (!login.trim() || !message.includes("already exists")) return null;
  return m["admin_users.taken_login"]({ login: login.trim() });
}


export function AdminUsers() {
  // Searched and paged on the server. It used to filter in the browser over
  // whatever the endpoint had returned, which reads the same until the
  // organisation outgrows one response — at which point the box quietly
  // searches the first page and reports nothing found.
  const paged = usePaginated();
  const [status, setStatus] = useState<UserStatus | null>(null);
  const users = useUsers(paged.page, true, status ?? undefined);
  const statusCounts = useUserStatusCounts();
  const update = useUpdateUser();
  const toast = useOptionalToast();
  const pendingErasures = usePendingErasures();
  const bundled = useBundledProvider();
  const providers = useIdentityProviders();
  // Only meaningful once the identity providers have loaded — before that,
  // "no bundled provider" and "haven't checked yet" would otherwise look
  // identical, hiding every bundled action for a moment on every page load.
  const knowsProviders = providers.data !== undefined;
  // ADR 0095: the account backend comes from the enabled provider's
  // capabilities, not its kind — the bundled Authelia creates logins; every
  // other kind invites. The gateway answers the same question from the same
  // field, so the two can never disagree.
  const enabledProvider = providers.data?.find((provider) => provider.is_enabled);
  const createsLogins = enabledProvider?.capabilities.creates_logins ?? false;

  const [editing, setEditing] = useState<AdminUser | null>(null);
  const [deleting, setDeleting] = useState<AdminUser | null>(null);
  const [adding, setAdding] = useState(false);
  const [signingIn, setSigningIn] = useState<AdminUser | null>(null);
  const [resetting, setResetting] = useState<AdminUser | null>(null);
  const [togglingActive, setTogglingActive] = useState<AdminUser | null>(null);
  const [viewingActivity, setViewingActivity] = useState<AdminUser | null>(null);
  const [merging, setMerging] = useState<AdminUser | null>(null);

  const pickStatus = (next: UserStatus | null) => {
    setStatus(next);
    paged.setOffset(0);
  };

  // Re-issue an expired invitation: regenerate the text (which re-stamps
  // `invited_at`, ADR 0095) and put it on the clipboard.
  const reinvite = useInviteText();
  const reissue = (user: AdminUser) =>
    reinvite.mutate(user.id, {
      onSuccess: async (result) => {
        try {
          await navigator.clipboard.writeText(result.invite_text);
          toast?.add({ title: m["admin_users.invite.toast_copied"](), type: "success" });
        } catch {
          toast?.add({ title: result.invite_text, type: "info" });
        }
      },
      onError: (caught: unknown) =>
        toast?.add({
          // i18n: server text — the API's own error message.
          title: caught instanceof Error ? caught.message : m["admin_users.toast.reissue_failed"](),
          type: "error",
        }),
    });

  const approve = (user: AdminUser) =>
    update.mutate(
      { id: user.id, is_active: true },
      {
        onSuccess: () =>
          toast?.add({ title: m["admin_users.toast.approved"](), type: "success" }),
        onError: (caught: unknown) =>
          toast?.add({
            // i18n: server text — the API's own error message.
            title: caught instanceof Error
              ? caught.message
              : m["admin_users.toast.approve_failed"](),
            type: "error",
          }),
      },
    );

  const page = users.data;
  const rows = page?.items ?? [];

  const columns: Column<AdminUser>[] = [
    {
      key: "who",
      header: m["admin_users.column.user"](),
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
            <div className={`${MUTED} ${CODE}`}>
              {m["admin_users.bundled_login_line"]({ login: user.bundled_login })}
            </div>
          ) : null}
          {/* The directory's own name for this person, shown when it is not
              already the line above. An account created in the identity provider as
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
      header: m["admin_users.column.groups"](),
      render: (user) =>
        user.groups.length === 0 ? (
          <span className={MUTED}>{m["admin_users.groups_none"]()}</span>
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
      header: m["admin_users.column.billing_group"](),
      render: (user) =>
        user.default_billing_group ?? <span className={MUTED}>{m["admin_users.billing_unset"]()}</span>,
    },
    {
      key: "keys",
      header: m["admin_users.column.keys"](),
      numeric: true,
      render: (user) => formatNumber(user.active_key_count),
    },
    {
      key: "status",
      header: m["admin_users.column.status"](),
      render: (user) => (
        <div className={CHIPS}>
          {user.is_admin && <Badge tone="accent">{m["admin_users.badge.admin"]()}</Badge>}
          {isInvited(user) && <Badge tone="warn">{m["admin_users.badge.invited"]()}</Badge>}
          {!user.is_active && user.deactivated_by === "approval" && (
            <Badge tone="warn">{m["admin_users.badge.awaiting"]()}</Badge>
          )}
          {user.is_active && !isInvited(user) ? (
            <Badge tone="ok">{m["admin_users.badge.active"]()}</Badge>
          ) : null}
          {!user.is_active && user.deactivated_by !== "approval" && (
            <Badge tone="danger">{m["admin_users.badge.disabled"]()}</Badge>
          )}
          {isInvited(user) && inviteExpired(user) && (
            <Badge tone="danger">{m["admin_users.badge.expired"]()}</Badge>
          )}
          {/* ADR 0096: neither capability — a chat-only person. Null means
              the listing did not answer, which is never the badge. */}
          {user.code_agents_allowed === false && user.api_keys_allowed === false && (
            <Badge tone="neutral">{m["admin_users.badge.chat_only"]()}</Badge>
          )}
        </div>
      ),
    },
    {
      key: "seen",
      header: m["admin_users.column.last_login"](),
      render: (user) =>
        user.last_login_at ? (
          formatDate(user.last_login_at)
        ) : (
          <span className={MUTED}>{m["admin_users.never"]()}</span>
        ),
    },
    {
      key: "actions",
      header: "",
      render: (user) => (
        <div className={ROW_ACTIONS}>
          {/* The entry point: Reset password, Activity and Merge into… live
              inside the panel this opens, so the row carries only the
              decisions, not every tool. */}
          <Button variant="secondary" onClick={() => setEditing(user)}>
            {m["admin_users.action.edit"]()}
          </Button>
          {/* The approval queue's one click (ADR 0095): enable is the whole
              approval; Reject is the Delete in the menu. */}
          {!user.is_active && user.deactivated_by === "approval" && (
            <Button variant="secondary" onClick={() => approve(user)}>
              {m["admin_users.action.approve"]()}
            </Button>
          )}
          <RowActions
            label={m["table.row_actions"]({
              name: user.display_name || user.username || user.email || user.subject,
            })}
            actions={[
              ...(isInvited(user) && inviteExpired(user)
                ? [
                    {
                      key: "reinvite",
                      label: m["admin_users.action.reinvite"](),
                      onSelect: () => reissue(user),
                      disabled: reinvite.isPending,
                    },
                  ]
                : []),
              ...(!user.is_active && user.deactivated_by === "approval"
                ? []
                : [
                    {
                      key: "toggle",
                      label: user.is_active
                        ? m["admin_users.action.disable"]()
                        : m["admin_users.action.enable"](),
                      onSelect: () => setTogglingActive(user),
                    },
                  ]),
              {
                key: "delete",
                label: m["admin_users.action.delete"](),
                onSelect: () => setDeleting(user),
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
        title={m["admin_users.title"]()}
        subtitle={
          !knowsProviders
            ? m["admin_users.subtitle.unknown"]()
            : createsLogins
              ? m["admin_users.subtitle.logins"]()
              : m["admin_users.subtitle.invites"]()
        }
        actions={
          knowsProviders && enabledProvider ? (
            <Button onClick={() => setAdding(true)}>
              {createsLogins ? m["admin_users.add_user"]() : m["admin_users.invite"]()}
            </Button>
          ) : null
        }
      />

      {pendingErasures.data && pendingErasures.data.pending > 0 && (
        <Notice tone="info" title={m["admin_users.erasures.title"]()}>
          {m["admin_users.erasures.body"]({ count: pendingErasures.data.pending })}
        </Notice>
      )}

      {update.error ? (
        <Notice tone="danger" title={m["admin_users.toast.update_failed"]()}>
          {/* i18n: server text — the API's own error message. */}
          {update.error instanceof Error ? update.error.message : m["admin_users.unknown_error"]()}
        </Notice>
      ) : null}

      <Card>
        {/* The search row and its growing field are a one-off shape (a lone
            input that must shrink before the row wraps), so they are inline
            here rather than constants in lib/layout. */}
        <div className="flex flex-wrap items-end gap-3">
          <div className="min-w-56 flex-1">
            <Input
              label={m["admin_users.search"]()}
              value={paged.search}
              onChange={(event) => paged.setSearch(event.target.value)}
              placeholder={m["admin_users.search_placeholder"]()}
              hint={
                page
                  ? m["admin_users.search_hint_matching"]({ count: page.total })
                  : m["admin_users.search_hint_global"]()
              }
            />
          </div>
        </div>
        {/* The filter chips (ADR 0095): the queues an administrator works,
            with their counts from one round trip. "All" is no filter at all. */}
        <div className="mt-3 flex flex-wrap gap-2">
          <Chip
            active={status === null}
            onClick={() => pickStatus(null)}
            label={m["admin_users.chip.all"]()}
          />
          <Chip
            active={status === "awaiting_approval"}
            onClick={() => pickStatus("awaiting_approval")}
            label={m["admin_users.chip.awaiting"]()}
            count={statusCounts.data?.awaiting_approval}
          />
          <Chip
            active={status === "invited"}
            onClick={() => pickStatus("invited")}
            label={m["admin_users.chip.invited"]()}
            count={statusCounts.data?.invited}
          />
          <Chip
            active={status === "active"}
            onClick={() => pickStatus("active")}
            label={m["admin_users.chip.active"]()}
            count={statusCounts.data?.active}
          />
          <Chip
            active={status === "disabled"}
            onClick={() => pickStatus("disabled")}
            label={m["admin_users.chip.disabled"]()}
            count={statusCounts.data?.disabled}
          />
        </div>
      </Card>

      <Card flush>
        {users.isPending ? (
          <Spinner label={m["admin_users.loading"]()} />
        ) : users.error ? (
          <Notice tone="danger" title={m["admin_users.load_error.title"]()}>
            {/* i18n: server text — the API's own error message. */}
            {users.error instanceof Error ? users.error.message : m["admin_users.unknown_error"]()}
          </Notice>
        ) : (
          <>
            <Table
              columns={columns}
              rows={rows}
              rowKey={(user) => user.id}
              empty={paged.query ? m["admin_users.empty_query"]() : m["admin_users.empty"]()}
              caption={m["admin_users.caption"]()}
            />
            <Pagination
              total={page?.total ?? 0}
              limit={paged.limit}
              offset={paged.offset}
              onOffsetChange={paged.setOffset}
              noun={m["admin_users.noun"]()}
              busy={users.isFetching}
            />
          </>
        )}
      </Card>


      <EditUserDialog
        user={editing}
        bundled={bundled}
        knowsProviders={knowsProviders}
        onResetPassword={setResetting}
        onCreateSignIn={setSigningIn}
        onViewActivity={setViewingActivity}
        onMerge={setMerging}
        onClose={() => setEditing(null)}
      />

      <DeleteUserDialog user={deleting} onClose={() => setDeleting(null)} />

      <AddUserDialog
        open={adding}
        createsLogins={createsLogins}
        knowsProviders={knowsProviders}
        onClose={() => setAdding(false)}
      />

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
      title={m["admin_users.delete.title"]({
        name: user?.display_name || user?.email || user?.subject || m["admin_users.delete.title_fallback"](),
      })}
      onClose={close}
      footer={
        <>
          <Button onClick={close}>{m["admin_users.delete.cancel"]()}</Button>
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
                      title: m["admin_users.delete.toast_done"](),
                      // i18n: server text — the erasure result is the API's.
                      description: result.chat_erasure_done
                        ? m["admin_users.delete.toast_chat_done"]()
                        : m["admin_users.delete.toast_chat_queued"](),
                      type: "success",
                    });
                    close();
                  },
                  onError: () =>
                    toast?.add({
                      title: m["admin_users.delete.toast_failed"](),
                      type: "error",
                    }),
                }
              )
            }
          >
            {m["admin_users.delete.confirm"]()}
          </Button>
        </>
      }
    >
      {preview.isPending ? (
        <Spinner label={m["admin_users.delete.loading"]()} />
      ) : preview.error ? (
        <Notice tone="danger" title={m["admin_users.delete.load_error.title"]()}>
          {/* i18n: server text — the API's own error message. */}
          {preview.error instanceof Error ? preview.error.message : m["admin_users.unknown_error"]()}
        </Notice>
      ) : preview.data ? (
        <div className={FORM}>
          <ul className={CHECK_ITEM}>
            {Object.entries(preview.data.gateway_counts)
              .filter(([, count]) => count > 0)
              .map(([table, count]) => (
                // i18n: server text — the table's own name, which is what the
                // API calls it and what an operator greps the schema for.
                <li key={table}>
                  {formatNumber(count)} {table.replace(/_/g, " ")}
                </li>
              ))}
          </ul>
          {preview.data.bundled_login && (
            <p className="text-sm text-ink-muted">
              {m["admin_users.delete.bundled_login_line"]({ login: preview.data.bundled_login })}
            </p>
          )}
          {!preview.data.chat_reachable ? (
            <Notice tone="warn" title={m["admin_users.delete.chat_unavailable_title"]()}>
              {m["admin_users.delete.chat_unavailable_body"]()}
            </Notice>
          ) : (
            <ul className={CHECK_ITEM}>
              {Object.entries(preview.data.chat_counts ?? {})
                .filter(([, count]) => count > 0)
                .map(([collection, count]) => (
                  // i18n: server text — the chat's own collection names.
                  <li key={collection}>
                    {formatNumber(count)} {collection.replace(/([A-Z])/g, " $1").toLowerCase()}
                  </li>
                ))}
            </ul>
          )}
          <p>{m["admin_users.delete.body"]()}</p>
          <p className="text-sm text-ink-muted">{m["admin_users.delete.ledger_note"]()}</p>
          {preview.data.shared_with_others && (
            <>
              {preview.data.shared.length > 0 && (
                <ul className={CHECK_ITEM}>
                  {preview.data.shared.map((resource) => (
                    // i18n: server text — the shared resource's own title and audience.
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
                  {preview.data.chat_unattributed_legacy_shares > 0
                    ? m["admin_users.delete.shared_confirm_attributed"]()
                    : m["admin_users.delete.shared_confirm_named"]()}
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
 * (or its id, if it has none) rather than a checkbox, plus an optional reason.
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
      title={m["admin_users.merge.title"]({
        name: user?.display_name || user?.email || m["admin_users.merge.title_fallback"](),
      })}
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
            {m["admin_users.merge.cancel"]()}
          </Button>
          <Button
            variant="danger"
            disabled={!user || !targetId || confirm !== expectedConfirm}
            busy={merge.isPending}
            onClick={() =>
              user &&
              targetId &&
              merge.mutate(
                { sourceId: user.id, into: targetId, confirm, reason },
                {
                  onSuccess: () => {
                    toast?.add({ title: m["admin_users.merge.toast_done"](), type: "success" });
                    reset();
                    onClose();
                  },
                  onError: (error) =>
                    toast?.add({
                      title: m["admin_users.merge.toast_failed"](),
                      // i18n: server text — the API's own error message.
                      description: error instanceof Error
                        ? error.message
                        : m["admin_users.unknown_error"](),
                      type: "error",
                    }),
                }
              )
            }
          >
            {m["admin_users.merge.confirm"]()}
          </Button>
        </>
      }
    >
      <p>
        <Message
          message={m["admin_users.merge.body"]}
          markup={{ strong: (children) => <strong>{children}</strong> }}
          inputs={{
            name: user?.email || user?.display_name || m["admin_users.merge.title_fallback"](),
          }}
        />
      </p>

      {!targetId ? (
        <div className={FORM}>
          <Input
            label={m["admin_users.merge.target_label"]()}
            value={targetQuery}
            onChange={(event) => setTargetQuery(event.target.value)}
            placeholder={m["admin_users.merge.target_placeholder"]()}
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
                <p className="text-sm text-ink-muted">{m["admin_users.merge.no_match"]()}</p>
              )}
            </div>
          ) : null}
        </div>
      ) : preview.isPending ? (
        <Spinner label={m["admin_users.merge.loading"]()} />
      ) : preview.error ? (
        <Notice tone="danger" title={m["admin_users.merge.load_error.title"]()}>
          {/* i18n: server text — the API's own error message. */}
          {preview.error instanceof Error ? preview.error.message : m["admin_users.unknown_error"]()}
        </Notice>
      ) : preview.data ? (
        <div className={FORM}>
          <p className="text-sm text-ink-muted">
            {/* i18n: server text — the target account's own names. */}
            {m["admin_users.merge.into"]({
              name: target?.display_name || target?.email || preview.data.target_id,
            })}
          </p>
          <ul className={CHECK_ITEM}>
            {Object.entries(preview.data.counts)
              .filter(([, count]) => count > 0)
              .map(([table, count]) => (
                // i18n: server text — the table's own name.
                <li key={table}>
                  {formatNumber(count)} {table.replace(/_/g, " ")}
                </li>
              ))}
          </ul>
          {preview.data.identities_dropped.length > 0 && (
            <Notice tone="warn" title={m["admin_users.merge.identities_title"]()}>
              {m["admin_users.merge.identities_body"]({
                issuers: preview.data.identities_dropped.map((i) => i.issuer).join(", "),
              })}
            </Notice>
          )}
          {preview.data.duplicate_rules_dropped > 0 && (
            <p className="text-sm text-ink-muted">
              {m["admin_users.merge.duplicate_rules"]({
                count: preview.data.duplicate_rules_dropped,
              })}
            </p>
          )}
          {preview.data.bundled_logins_disabled.length > 0 && (
            <p className="text-sm text-ink-muted">
              {m["admin_users.merge.bundled_disabled"]({
                count: preview.data.bundled_logins_disabled.length,
                logins: preview.data.bundled_logins_disabled.join(", "),
              })}
            </p>
          )}
          <p className="text-sm text-ink-muted">
            {preview.data.resulting_is_admin
              ? m["admin_users.merge.admin_yes"]()
              : m["admin_users.merge.admin_no"]()}{" "}
            {/* i18n: server text — the chat's own note. */}
            {preview.data.chat_note}.
          </p>
          <Input
            label={m["admin_users.merge.type_confirm"]({ text: expectedConfirm })}
            value={confirm}
            onChange={(event) => setConfirm(event.target.value)}
          />
          <Input
            label={m["admin_users.merge.reason"]()}
            value={reason}
            onChange={(event) => setReason(event.target.value)}
            placeholder={m["admin_users.merge.reason_placeholder"]()}
          />
        </div>
      ) : null}
    </Dialog>
  );
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
        <Message
          message={m["admin_users.password.sign_in_as"]}
          markup={{ strong: (children) => <strong>{children}</strong> }}
          inputs={{ login }}
        />
      </p>
      <div className={SECRET_ROW}>
        <code className={SECRET}>{password}</code>
        <Button onClick={copy}>
          {copied ? m["admin_users.password.copied"]() : m["admin_users.password.copy"]()}
        </Button>
      </div>
      {copied === false && (
        <p className="text-sm text-warn">{m["admin_users.password.clipboard"]()}</p>
      )}
      <p className={SECRET_DETAIL}>{m["admin_users.password.detail"]()}</p>
    </>
  );
}

/**
 * Add a person to the bundled directory (§8.1/§8.2). Groups here are console
 * groups — manual memberships — never the Authelia file's own, which is
 * always `["users"]` (§8.3): editing it stopped being on offer at all.
 */
function AddUserDialog({
  open,
  createsLogins,
  knowsProviders,
  onClose,
}: {
  open: boolean;
  createsLogins: boolean;
  knowsProviders: boolean;
  onClose: () => void;
}) {
  // ADR 0095: one entry point, two backends — the enabled provider's
  // capabilities decide, the same field the gateway routes on.
  if (open && knowsProviders && !createsLogins) {
    return <InviteUserDialog open={open} onClose={onClose} />;
  }
  return <BundledAddUserDialog open={open} onClose={onClose} />;
}

/** One filter chip of the status row. A button styled as a chip: "All" is
 * no filter at all, and the count sits in the label. */
function Chip({
  active,
  onClick,
  label,
  count,
}: {
  active: boolean;
  onClick: () => void;
  label: string;
  count?: number;
}) {
  return (
    <button
      type="button"
      onClick={onClick}
      aria-pressed={active}
      className={
        active
          ? "rounded-full border border-accent bg-accent-soft px-3 py-1 text-xs font-medium text-accent"
          : "rounded-full border border-line px-3 py-1 text-xs text-ink-muted hover:border-accent"
      }
    >
      {label}
      {count === undefined ? "" : ` (${formatNumber(count)})`}
    </button>
  );
}

/** Invite a person for a provider that creates no logins (ADR 0095 §2):
 * email, name, groups — then the ready-to-paste paragraph. */
function InviteUserDialog({ open, onClose }: { open: boolean; onClose: () => void }) {
  const invite = useInviteUser();
  const emailSettings = useEmailSettings();
  const toast = useOptionalToast();
  const [displayName, setDisplayName] = useState("");
  const [email, setEmail] = useState("");
  const [groups, setGroups] = useState("");
  const [sendEmail, setSendEmail] = useState(false);
  const [copied, setCopied] = useState(false);

  const smtpUsable =
    (emailSettings.data?.enabled ?? false) &&
    (emailSettings.data?.host ?? "") !== "" &&
    (emailSettings.data?.from_address ?? "") !== "";

  const close = () => {
    invite.reset();
    setDisplayName("");
    setEmail("");
    setGroups("");
    setSendEmail(false);
    setCopied(false);
    onClose();
  };

  const created = invite.data;

  return (
    <Dialog
      open={open}
      title={m["admin_users.invite.title"]()}
      onClose={close}
      footer={
        created ? (
          <Button variant="primary" onClick={close}>
            {m["admin_users.invite.done"]()}
          </Button>
        ) : (
          <>
            <Button onClick={close}>{m["admin_users.invite.cancel"]()}</Button>
            <Button
              variant="primary"
              disabled={!email.trim()}
              busy={invite.isPending}
              onClick={() =>
                invite.mutate({
                  email: email.trim(),
                  display_name: displayName.trim() || undefined,
                  groups: groups
                    .split(",")
                    .map((g) => g.trim())
                    .filter(Boolean),
                  send_email: sendEmail && smtpUsable,
                })
              }
            >
              {sendEmail && smtpUsable
                ? m["admin_users.invite.submit_and_send"]()
                : m["admin_users.invite.submit"]()}
            </Button>
          </>
        )
      }
    >
      {invite.error ? (
        <Notice tone="danger" title={m["admin_users.invite.load_error.title"]()}>
          {/* i18n: server text — the API's own error message. */}
          {invite.error instanceof Error ? invite.error.message : m["admin_users.unknown_error"]()}
        </Notice>
      ) : null}

      {created ? (
        <div className={FORM}>
          {created.email_sent ? (
            <Notice tone="info" title={m["admin_users.invite.sent_title"]()}>
              {/* i18n: server text — the invited account's own address. */}
              {m["admin_users.invite.sent_body"]({ email: String(created.user.email) })}
            </Notice>
          ) : created.email_error ? (
            <Notice tone="warn" title={m["admin_users.invite.email_failed_title"]()}>
              {/* i18n: server text — the mailer's own error. */}
              {m["admin_users.invite.email_failed_body"]({ error: created.email_error })}
            </Notice>
          ) : sendEmail ? null : null}
          <div>
            <p className="mb-1 text-sm font-medium">{m["admin_users.invite.text_heading"]()}</p>
            <p className="rounded border border-line bg-surface-muted p-2 text-sm">
              {created.invite_text}
            </p>
            <Button
              variant="secondary"
              className="mt-2"
              busy={copied}
              onClick={async () => {
                try {
                  await navigator.clipboard.writeText(created.invite_text);
                  setCopied(true);
                  toast?.add({
                    title: m["admin_users.invite.toast_copied"](),
                    type: "success",
                  });
                } catch {
                  toast?.add({
                    title: m["admin_users.invite.toast_copy_failed"](),
                    type: "error",
                  });
                }
              }}
            >
              {copied
                ? m["admin_users.invite.copied"]()
                : m["admin_users.invite.copy"]()}
            </Button>
          </div>
        </div>
      ) : (
        <div className={FORM}>
          <Input
            label={m["admin_users.invite.email"]()}
            type="email"
            value={email}
            onChange={(event) => setEmail(event.target.value)}
          />
          <Input
            label={m["admin_users.invite.display_name"]()}
            value={displayName}
            onChange={(event) => setDisplayName(event.target.value)}
          />
          <Input
            label={m["admin_users.invite.groups"]()}
            value={groups}
            onChange={(event) => setGroups(event.target.value)}
            placeholder={m["admin_users.invite.groups_placeholder"]()}
            hint={m["admin_users.invite.groups_hint"]()}
          />
          {smtpUsable ? (
            <label className={CHECK_ITEM}>
              <input
                type="checkbox"
                checked={sendEmail}
                onChange={(event) => setSendEmail(event.target.checked)}
              />
              <span>{m["admin_users.invite.email_them"]()}</span>
            </label>
          ) : (
            <p className="text-xs text-ink-faint">
              {m["admin_users.invite.email_unconfigured"]()}
            </p>
          )}
        </div>
      )}
    </Dialog>
  );
}

function BundledAddUserDialog({ open, onClose }: { open: boolean; onClose: () => void }) {
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
      title={m["admin_users.add.title"]()}
      onClose={close}
      footer={
        created ? (
          <Button variant="primary" onClick={close}>
            {m["admin_users.add.done"]()}
          </Button>
        ) : (
          <>
            <Button onClick={close}>{m["admin_users.add.cancel"]()}</Button>
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
              {m["admin_users.add.submit"]()}
            </Button>
          </>
        )
      }
    >
      {create.error ? (
        <Notice tone="danger" title={m["admin_users.add.load_error.title"]()}>
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
            label={m["admin_users.add.email"]()}
            type="email"
            value={email}
            onChange={(event) => {
              setEmail(event.target.value);
              if (!loginByHand) setLogin(deriveLogin(event.target.value));
            }}
          />
          <Input
            label={m["admin_users.add.login"]()}
            value={login}
            onChange={(event) => {
              setLogin(event.target.value);
              setLoginByHand(true);
            }}
            hint={LOGIN_HINT()}
          />
          <Input
            label={m["admin_users.add.display_name"]()}
            value={displayName}
            onChange={(event) => setDisplayName(event.target.value)}
          />
          <Input
            label={m["admin_users.add.groups"]()}
            value={groups}
            onChange={(event) => setGroups(event.target.value)}
            placeholder={m["admin_users.add.groups_placeholder"]()}
            hint={m["admin_users.add.groups_hint"]()}
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
      title={m["admin_users.signin.title"]({
        name: user?.display_name || user?.email || user?.subject || "",
      })}
      onClose={close}
      footer={
        created ? (
          <Button variant="primary" onClick={close}>
            {m["admin_users.signin.done"]()}
          </Button>
        ) : (
          <>
            <Button onClick={close}>{m["admin_users.signin.cancel"]()}</Button>
            <Button
              variant="primary"
              disabled={!login.trim()}
              busy={create.isPending}
              onClick={() => user && create.mutate({ userId: user.id, login: login.trim() })}
            >
              {m["admin_users.signin.submit"]()}
            </Button>
          </>
        )
      }
    >
      {create.error ? (
        <Notice tone="danger" title={m["admin_users.signin.load_error.title"]()}>
          {taken ?? (create.error instanceof Error ? create.error.message : "Unknown error.")}
        </Notice>
      ) : null}

      {created ? (
        <MintedPasswordNotice login={created.bundled_login ?? login.trim()} password={created.password} />
      ) : (
        <Input
          label={m["admin_users.signin.login"]()}
          value={login}
          onChange={(event) => setLogin(event.target.value)}
          hint={LOGIN_HINT()}
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
      title={m["admin_users.reset.title"]({
        name: user?.display_name || user?.email || user?.subject || "",
      })}
      onClose={close}
      footer={
        result ? (
          <Button variant="primary" onClick={close}>
            {m["admin_users.reset.done"]()}
          </Button>
        ) : (
          <>
            <Button onClick={close}>{m["admin_users.reset.cancel"]()}</Button>
            <Button
              variant="primary"
              busy={reset.isPending}
              onClick={() => user && reset.mutate(user.id)}
            >
              {m["admin_users.reset.submit"]()}
            </Button>
          </>
        )
      }
    >
      {reset.error ? (
        <Notice tone="danger" title={m["admin_users.reset.load_error.title"]()}>
          {reset.error instanceof Error ? reset.error.message : "Unknown error."}
        </Notice>
      ) : null}

      {result ? (
        <MintedPasswordNotice login={user?.bundled_login ?? user?.username ?? ""} password={result.password} />
      ) : (
        <p>{m["admin_users.reset.body"]()}</p>
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
  // The verb is a word on a button and half a title; the two directions are
  // separate messages rather than one word swapped into two sentences, because
  // the sentences around it differ too.
  const verb = disabling ? m["admin_users.toggle.disable"]() : m["admin_users.toggle.enable"]();

  const save = () =>
    user &&
    update.mutate(
      { id: user.id, is_active: !user.is_active },
      {
        onSuccess: (updated) => {
          if (updated.authelia_sync === "failed") {
            toast?.add({
              // i18n: server text — the sync failure is the API's own sentence.
              title: updated.authelia_sync_message ?? m["admin_users.toggle.authelia_failed"](),
              type: "error",
            });
          } else {
            toast?.add({
              title: disabling
                ? m["admin_users.toggle.toast_disabled"]()
                : m["admin_users.toggle.toast_enabled"](),
              type: "success",
            });
          }
          close();
        },
        onError: () =>
          toast?.add({
            title: disabling
              ? m["admin_users.toggle.toast_disable_failed"]()
              : m["admin_users.toggle.toast_enable_failed"](),
            type: "error",
          }),
      },
    );

  return (
    <Dialog
      open={user !== null}
      title={
        disabling
          ? m["admin_users.toggle.disable_title"]({
              name: user?.display_name || user?.email || user?.subject || m["admin_users.toggle.title_fallback"](),
            })
          : m["admin_users.toggle.enable_title"]({
              name: user?.display_name || user?.email || user?.subject || m["admin_users.toggle.title_fallback"](),
            })
      }
      onClose={close}
      footer={
        <>
          <Button onClick={close}>{m["admin_users.toggle.cancel"]()}</Button>
          <Button variant={disabling ? "danger" : "primary"} busy={update.isPending} onClick={save}>
            {verb}
          </Button>
        </>
      }
    >
      {update.error ? (
        <Notice
          tone="danger"
          title={
            disabling
              ? m["admin_users.toggle.toast_disable_failed"]()
              : m["admin_users.toggle.toast_enable_failed"]()
          }
        >
          {/* i18n: server text — the API's own error message. */}
          {update.error instanceof Error ? update.error.message : m["admin_users.unknown_error"]()}
        </Notice>
      ) : null}

      {disabling ? (
        <div className={FORM}>
          <p>{m["admin_users.toggle.disable_sessions"]()}</p>
          <p>{m["admin_users.toggle.disable_keys"]()}</p>
          {user && hasBundledLogin(user, bundled) ? (
            <p>{m["admin_users.toggle.disable_bundled"]()}</p>
          ) : (
            <p>{m["admin_users.toggle.disable_idp"]()}</p>
          )}
        </div>
      ) : (
        <div className={FORM}>
          <p>{m["admin_users.toggle.enable_body"]()}</p>
          {user && hasBundledLogin(user, bundled) ? (
            <p>{m["admin_users.toggle.enable_bundled"]()}</p>
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
      title={m["admin_users.activity.title"]({
        name: user?.display_name || user?.email || user?.subject || "",
      })}
      onClose={onClose}
      footer={<Button onClick={onClose}>{m["admin_users.activity.close"]()}</Button>}
    >
      {events.isPending ? (
        <Spinner label={m["admin_users.activity.loading"]()} />
      ) : events.error ? (
        <Notice tone="danger" title={m["admin_users.activity.load_error.title"]()}>
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
              {/* i18n: server text — the action, the actor and the reason are
                  the audit log's own values. */}
              <div className={`${MUTED} text-sm`}>
                {event.actor_label || event.actor_type}
                {event.reason ? ` — ${event.reason}` : ""}
              </div>
            </li>
          ))}
        </ul>
      ) : (
        <p className={MUTED}>{m["admin_users.activity.none"]()}</p>
      )}
    </Dialog>
  );
}

/**
 * Everything an administrator may change about one account, in one place
 * instead of a scattered Enable button and a password endpoint with no door.
 *
 * Five facts shape it:
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
 * - **The person's other actions live here.** Reset password (or Create
 *   sign-in, per whether they already hold a bundled login), Activity and
 *   Merge into… moved in from the row, which kept only the decisions
 *   (Disable/Enable, Delete). Each opens its own dialog on top of this one —
 *   the page's existing stack, where every dialog is a sibling controlled by
 *   its own state and the later sibling layers above — so nothing about
 *   those flows changed but their address.
 */
function EditUserDialog({
  user,
  bundled,
  knowsProviders,
  onResetPassword,
  onCreateSignIn,
  onViewActivity,
  onMerge,
  onClose,
}: {
  user: AdminUser | null;
  bundled: IdentityProvider | undefined;
  knowsProviders: boolean;
  onResetPassword: (user: AdminUser) => void;
  onCreateSignIn: (user: AdminUser) => void;
  onViewActivity: (user: AdminUser) => void;
  onMerge: (user: AdminUser) => void;
  onClose: () => void;
}) {
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
        onSuccess: () =>
          toast?.add({ title: m["admin_users.edit.toast_done"](), type: "success" }),
        onError: (caught: unknown) =>
          toast?.add({
            // i18n: server text — the API's own error message.
            title: caught instanceof Error
              ? caught.message
              : m["admin_users.edit.toast_failed"](),
            type: "error",
          }),
      },
    );

  const isLocal = user?.issuer === "local";

  return (
    <Dialog
      open={user !== null}
      title={
        user
          ? m["admin_users.edit.title"]({
              name: user.display_name || user.email || user.subject,
            })
          : m["admin_users.edit.title_plain"]()
      }
      onClose={close}
      footer={
        <>
          <Button onClick={close}>{m["admin_users.edit.close"]()}</Button>
          <Button variant="primary" disabled={!dirty} busy={update.isPending} onClick={save}>
            {m["admin_users.edit.save"]()}
          </Button>
        </>
      }
    >
      {update.error ? (
        <Notice tone="danger" title={m["admin_users.toast.update_failed"]()}>
          {/* i18n: server text — the API's own error message. */}
          {update.error instanceof Error ? update.error.message : m["admin_users.unknown_error"]()}
        </Notice>
      ) : null}

      <div className={FORM}>
        <div>
          <div className="text-xs font-medium tracking-[0.01em] text-ink-muted">
            {m["admin_users.edit.identity"]()}
          </div>
          <div className="mt-1 flex flex-wrap items-center gap-2">
            {user?.is_admin && (
              <Badge tone="accent">{m["admin_users.edit.badge_administrator"]()}</Badge>
            )}
            {user?.is_active ? (
              <Badge tone="ok">{m["admin_users.edit.badge_active"]()}</Badge>
            ) : (
              <Badge tone="danger">{m["admin_users.edit.badge_disabled"]()}</Badge>
            )}
            <Badge tone={isLocal ? "neutral" : "warn"}>
              {/* i18n: server text — "local" is the API's own issuer value. */}
              {isLocal ? m["admin_users.edit.kind_local"]() : m["admin_users.edit.kind_idp"]()}
            </Badge>
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
            {m["admin_users.edit.identity_note"]()}
          </p>
        </div>

        <div>
          <div className="text-xs font-medium tracking-[0.01em] text-ink-muted">
            {m["admin_users.edit.profile"]()}
          </div>
          <div className="mt-1 space-y-3">
            <Input
              label={m["admin_users.edit.email"]()}
              type="email"
              value={email}
              disabled={!user}
              onChange={(event) => setEmail(event.target.value)}
              hint={
                isLocal
                  ? m["admin_users.edit.email_hint_local"]()
                  : m["admin_users.edit.email_hint_idp"]()
              }
            />
            <Input
              label={m["admin_users.edit.display_name"]()}
              value={displayName}
              disabled={!user}
              onChange={(event) => setDisplayName(event.target.value)}
              hint={isLocal ? undefined : m["admin_users.edit.display_name_hint"]()}
            />
            <Input
              label={m["admin_users.edit.username"]()}
              value={username}
              disabled={!user}
              onChange={(event) => setUsername(event.target.value)}
              hint={isLocal ? undefined : m["admin_users.edit.username_hint"]()}
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
            {m["admin_users.edit.admin_label"]()}
            {!isLocal && (
              /* Says what actually happens now, which is the opposite of what
                 this used to say. Granting adds a *manual* membership of the
                 admin group, and a directory sync never removes one — so the
                 decision survives the next login rather than being undone by
                 it. Revoking is the narrower half: a membership the directory
                 granted has to be withdrawn there, and the API says so. */
              <span className="mt-0.5 block text-xs text-ink-faint">
                {m["admin_users.edit.admin_note"]()}
              </span>
            )}
          </span>
        </label>

        {/* The person's other actions, moved in from the row (which keeps
            only Edit, Disable/Enable and Delete). Each opens its own dialog
            on top of this one — the page's existing stack — so every flow
            keeps its dialog, its copy and its confirmations; only its
            address changed. */}
        <div>
          <div className="text-xs font-medium tracking-[0.01em] text-ink-muted">
            {m["admin_users.edit.actions"]()}
          </div>
          <div className="mt-2 flex flex-wrap gap-2">
            {/* Bundled-only (§8.1): a login belongs to the bundled Authelia,
                so neither action means anything against an external IdP,
                which owns its own accounts entirely. Everything is gated on
                `user` too — a dialog's children are evaluated whether it is
                open or not, and the closed state carries null. */}
            {user && knowsProviders && bundled ? (
              hasBundledLogin(user, bundled) ? (
                <Button variant="ghost" onClick={() => onResetPassword(user)}>
                  {m["admin_users.edit.reset_password"]()}
                </Button>
              ) : (
                <Button variant="ghost" onClick={() => onCreateSignIn(user)}>
                  {m["admin_users.edit.create_signin"]()}
                </Button>
              )
            ) : null}
            <Button
              variant="ghost"
              disabled={!user}
              onClick={() => user && onViewActivity(user)}
            >
              {m["admin_users.edit.activity"]()}
            </Button>
            <Button variant="ghost" disabled={!user} onClick={() => user && onMerge(user)}>
              {m["admin_users.edit.merge_into"]()}
            </Button>
          </div>
        </div>

        <p className="m-0 text-sm text-ink-muted">
          {knowsProviders && bundled
            ? m["admin_users.edit.passwords_note_bundled"]()
            : m["admin_users.edit.passwords_note"]()}
        </p>
      </div>
    </Dialog>
  );
}
