# 0048 — Users are created and deleted in the console; OIDC policy is a decision, not an env var

- Date: 2026-09-02
- Status: **accepted, built**
- Amends [0011](0011-oidc-integration.md) (provisioning was unconditional and
  environmental), [0022](0022-administration-surface.md) (admin followed an
  env-named group), and [0043](0043-local-authentication.md) (accounts were
  created by CLI). The connection settings of 0011 are **not** amended.
- Requested as: "I need facilities to create users, delete users, and
  configuration for the OIDC (like enabling automatic provisioning of users,
  group mapping)."

## Context

Everything about *who may exist* lived in `GATEWAY_OIDC__*` and was read once
at startup. Provisioning was unconditional: any stranger with a valid token
became a user at first login. Groups arrived by raw claim value — what the IdP
calls `platform-admins` is what the platform must call it — and admin
followed an env-named group. Local accounts existed only through `gateway
passwd`, and deleting an account was not possible at all (deactivation was).

The redaction work already solved "operator-editable configuration that must
not require a restart and must leave a record": an append-only table, newest
row wins, polled by every worker (ADR 0033). The identity policy is the same
kind of thing — a governance decision with a history someone will ask about —
so it takes the same shape.

## Decision

### The boundary: connection is environment, policy is configuration

The issuer, client secret, redirect URI and `access_token_audience` stay in
`GATEWAY_OIDC__*`, read once at startup. They are *how we talk to the IdP*;
making them hot would put the identity provider's reachability on the request
path and make a half-updated worker a security question. The *policy* — may a
stranger become a user, which claim names groups, what an IdP group means
here, which local group confers admin — is what an operator changes, so it
lives in the new append-only `oidc_config` table and is polled every 10s
(`oidc_policy.OIDCPolicyResolver`, the redaction resolver's shape with the
per-request machinery removed).

**Every policy column is nullable, and null means "this row does not
decide."** Effective policy is a per-field fold of the newest row over the
environment's values. A row can turn one knob without knowing the
deployment's other answers, and a deployment that never touches the console
behaves exactly as before — which is what kept the upgrade silent.

### Provisioning and the unknown-user rule

`auto_provision` (default true, unchanged behaviour when on). When off, a
first-time sign-in follows `unknown_user_policy`:

- **refuse** — the stranger is told to ask an administrator. Nothing created.
- **create_inactive** — the account is created *disabled* (memberships and
  all), waiting for an administrator to enable it. The sign-in itself is
  refused either way; the row is the approval queue.

The gate lives in `provision_user`, because both doors in — the browser
callback and a bearer token on `/v1` — must give the same answer, and that is
the one place a *first* login is visible as such. The callback additionally
refuses a session to any inactive account (previously a disabled IdP user
could complete a login into a console that would 401 on every request).

### Group mapping

A mapping rule says: IdP group *X* is called *Y* here. Many IdP groups may
map to one local group; unmapped groups keep their own name; the allowlist
still filters what the IdP *reports* (environmental, unchanged). Mapping
happens before reconciliation, and `admin_groups` compares **mapped, local
names** — what a group is called here, not what the IdP calls it. The
existing `groups_claim` setting (dotted paths supported) moves into the same
decision.

### Accounts

- **Create** (`POST /api/admin/users`) mints a **local** account — email,
  initial password, optional groups and admin. Local only, deliberately: a
  console-created directory user would be overwritten or orphaned at the next
  login. Group *names* resolve-or-create (source `manual` — a group an
  operator typed must not be pruned by IdP reconciliation). The account is
  born with its credential; the end-to-end test found the first draft shipped
  without one.
- **Delete** (`DELETE /api/admin/users/{id}`) removes the row. Keys,
  memberships, refresh credentials and the local password die with it
  (`CASCADE`); **the ledger keeps everything** (`usage_records.user_id` was
  already `ON DELETE SET NULL`) — historical spend survives, losing only the
  name in the per-user breakdown, the same trade deleting an API key makes.
  Quota and redaction rules scoped to the user keep their scope id and go
  **inert**, rendering as pointing at nobody; removing them is the
  administrator's next decision, not a side effect. Self-deletion is refused,
  and that rule *is* the last-admin rule: the caller is always an active
  administrator other than the target, so a "last admin" guard counting
  others could never fire.

## Consequences

- **Changing `GATEWAY_OIDC__*` still needs a restart** — for the connection
  fields. The console's Identity screen reports which fields are console
  decisions and which are environmental, because "the console says X and the
  environment says Y" is otherwise invisible.
- The append-only row records who changed what, when, and (optionally) why.
  Erasing a user nulls their `created_by` on old decisions; the decision
  outlives them, as on `redaction_config`.
- `unknown_user_policy` alongside `auto_provision: true` is refused by the
  API rather than stored-never-applied: a knob that does nothing because of
  another knob reads as a bug.
- The unused `GATEWAY_OIDC__GROUPS_CLAIM` / `ADMIN_GROUPS` env values remain
  the baseline a row folds onto — an operator can still deploy fully
  declaratively and never open the screen.
