# 0043 — Local authentication: email + password beside OIDC

- Date: 2026-08-29
- Status: **accepted, built** (unit-tested; the live flow script is still to run)
- Related: [0011](0011-oidc-integration.md) (OIDC login, the session this reuses),
  [0035](0035-public-tls-exposure.md) (why the seeded-credentials problem matters).

## Context

Until now the management surface had exactly one way in: the OIDC
authorization-code flow against the deployment's identity provider. That is the
right default — the directory is authoritative for groups, admin, and
revocation, and a second credential for the same people is a second thing to
revoke.

But there are deployments without an IdP at all: a personal instance reached
over an SSH tunnel, a demo stood up to show someone the console, a homelab
where Keycloak is the largest component for what it does. Today those deploy a
Keycloak whose only job is to authenticate one person, or they do not get a
console.

`gateway seed` has always created a local admin row (`issuer="local"`) — but
with no credential on it, which is why the gap never showed: the seeded user
authenticates with its API key and never signs in.

So: **local email + password sign-in, optional, beside OIDC — never instead of
it.**

## Decisions

### Off by default; `GATEWAY_LOCAL_AUTH__ENABLED=true` turns it on

A deployment with an identity provider should not grow a second way in just
because the feature shipped. The switch is a settings group (`local_auth`),
and the throttle's presence on `app.state` *is* the feature switch: with local
auth off, `POST /auth/login` answers 503 and `/auth/methods` reports no local
method. There is no code path where the form exists and the endpoint refuses
it for another reason.

### One session, two doors

A local login mints exactly the cookie the OIDC callback mints — a signed
`gw-session` JWT. Nothing downstream of authentication (quotas, admin,
reports, the console) learns which door the person used, and there is no
"local user" type: a local account is a `User` row with `issuer="local"`, the
convention `gateway seed` already used. Two doors, one room.

### A separate `local_credentials` table, not a column

Most users will never carry a password; putting a nullable hash on `users`
makes "who can sign in without the IdP" a scan for non-null values instead of
a query. The table also makes revocation a row deletion rather than an UPDATE
that leaves the empty string behind, and it keeps the audit question
enumerable.

**A local account is never linked to a directory identity.** Attaching a
password to an `issuer=<IdP>` row would give a leaked local password access to
whatever the directory granted that person — access nothing in the directory
reviewed. The admin routes refuse a password for any user whose issuer is not
`local`. Someone with two accounts has two accounts; that is honest.

### `pwdlib` + Argon2id

`passlib` is unmaintained and breaks on modern Python; `pwdlib` (MIT, 0.3.1 at
adoption) wraps `argon2-cffi`, whose Argon2id is the current standard answer
for a low-entropy human secret. The contrast with `security.py`'s bare SHA-256
for API keys is deliberate and recorded there: a key has 2^256 of entropy and
is presented on every request, where key stretching would tax the hot path to
defend nothing; a password is whatever the person chose and is presented only
at login, where tens of milliseconds is exactly the point.

`verify_and_update` rehashes on login when parameters have moved on — the one
moment the plaintext is in hand and an upgrade is free. Password policy is
length only (default 10), because character-class rules make passwords harder
to remember and not harder to guess (NIST SP 800-63B's measured position);
length is the axis that buys entropy.

### Every failure is the same answer, and costs the same work

Unknown address, wrong password, and a disabled account all return the same
401 with the same body. An unknown address also runs a **real Argon2
verification against a dummy hash**, so response timing does not reveal which
addresses exist before a single password is guessed — the first check on this
endpoint is not free on purpose.

### The throttle is per process, and that is a known cost

Failed attempts are counted per email in worker memory, not in Valkey and not
in PostgreSQL:

- **Not Valkey** — the counter store is a rebuildable cache; a throttle that
  vanishes when the cache is flushed is no throttle.
- **Not PostgreSQL with a fail-open fallback** — a login defence must fail
  *closed*; the quota engine's graceful degradation pattern is exactly the
  wrong shape here.

In-process fails closed for free (the counter cannot be lost without the
process dying) and costs proportionality: with N workers the effective limit
is N × `max_failed_attempts` per window. Argon2id keeps each attempt
expensive; a shared throttle would need sticky sessions or a loss-tolerant
store, and this deployment has neither. The arithmetic is a known quantity
rather than a silent gap.

429 deliberately carries no `Retry-After`: naming the window tells an attacker
exactly how long to wait between guesses.

### Bootstrap and administration

- **`gateway passwd <email>`** — creates or resets a local account, prompted
  for the password, no running gateway required. This is the bootstrap for the
  first administrator (the admin API needs an admin; on a fresh database nobody
  is one). Idempotent: re-running resets the password, which is the recovery
  story. Shell access to the host is the trust boundary — anyone with it can
  read the database anyway.
- **`gateway seed --password`** — the demo user gets a login when asked.
  Opt-in: a seeded credential nobody chose is a guessable account.
- **Admin API** — `PUT/DELETE /api/admin/users/{id}/password`, and
  `has_password` on the user listing. The console's Users screen can manage
  local accounts without SQL.

### The console adapts

`GET /auth/methods` (unauthenticated — it reveals only what visiting
`/auth/login` already reveals) tells the login page what to show:

- OIDC only → the same auto-redirect there always was;
- local only → the form;
- both → the form plus a "Sign in with SSO" link, and the auto-redirect stops,
  because it would make the password path unreachable.

`?next` survives both journeys.

### What was rejected

- **Self-service registration** — accounts exist because an operator created
  them. A gateway that bills money does not accept walk-ups.
- **Email-based password reset** — puts recovery in the hands of whatever mail
  server the deployment has; a bigger surface than the feature is worth.
  Recovery is `gateway passwd`.
- **`EmailStr` on the login request** — drags in `email-validator` to police
  the syntax of a string whose real test is whether it names an account; an
  unknown address already costs one dummy verification and one "incorrect".
- **Authlib / a full OIDC framework for local auth** — there is no flow here
  to buy: one form, one verify, one cookie.

## Consequences

- A deployment that never sets `GATEWAY_LOCAL_AUTH__ENABLED` behaves exactly as
  before. Existing tests pass unchanged.
- Local accounts bypass group provisioning: `gateway passwd --group` adds a
  membership additively, and nothing ever *removes* one — there is no directory
  to be authoritative. Revoking a local person means disabling the user
  (existing admin route), which the login and every key path re-check.
- `GATEWAY_OIDC__ADMIN_GROUPS` remains the only automatic admin path; local
  accounts are made admin by `gateway passwd --admin` (the default) or the
  existing `PATCH /api/admin/users/{id}`.
- **Not yet verified against the running stack**: `scripts/` has no local-auth
  live script yet; the unit suite covers the routes with the real password
  stack but not a real browser through the proxy. That is the next check before
  calling this done — this project's history says the live run is where the
  bug lives.
