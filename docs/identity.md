# Identity: who people are and who administers

!!! info "For operators"

    How a deployment decides who may sign in, who is an administrator, and how to get back in when nobody can; the provider's own registration is [Identity: the OIDC provider](oidc-generic-provider.md).

People sign in to the console, and to the chat when it is deployed, through
OpenID Connect only. There is no password sign-in in the gateway itself. Use
the [bundled Authelia](bundled-accounts.md) that `deploy/` (and cerea-deploy)
can run beside the gateway, or bring [your own issuer](oidc-generic-provider.md).
This page is everything the gateway decides *after* the provider has answered.

## One provider, set in the environment

One provider at a time, named by the `OIDC_*` variables of the deployment's
`.env`. The gateway takes its settings from there **at every start** and
rewrites its provider row to match, so the console shows the provider
read-only. What the console edits is the rest of identity: the provisioning
policy (who may come to exist, which claim names their groups, what an IdP
group means here, under **Settings**) and the people themselves (the
**Users** page).

If you need several sources of users, federate them in your own IdP
(Keycloak, Authentik and the like) and point the stack at it. **If you need
multi-factor authentication, that is also the IdP's job**: the bundled Authelia is password-only, so switch the stack to a Keycloak (or any IdP that does MFA) instead.

| Variable | Gateway setting | Meaning |
|---|---|---|
| `OIDC_ISSUER` | `GATEWAY_OIDC__ISSUER` | the issuer URL, exactly as the discovery document's `issuer` states it |
| `OIDC_INTERNAL_BASE_URL` | `GATEWAY_OIDC__INTERNAL_BASE_URL` | where the gateway reaches the issuer from inside the network (bundled Authelia: `http://authelia:9091/authelia`); empty means the issuer URL itself |
| `OIDC_KIND` | `GATEWAY_OIDC__KIND` | `authelia` for the bundled one, `generic` otherwise (`keycloak`, `entra`, `okta`, `authentik`, `google` are recognised kinds) |
| `OIDC_CONSOLE_CLIENT_ID`, `_SECRET` | `GATEWAY_OIDC__CLIENT_ID`, `_CLIENT_SECRET` | the console's client |
| `OIDC_CHAT_CLIENT_ID`, `_SECRET` | `GATEWAY_OIDC__CHAT_CLIENT_ID` (id only) | the chat's client, when the chat is deployed; the id also tells the gateway which accepted client may announce chat sign-ins |
| `OIDC_MACHINE_CLIENT_ID` | — | galopin's public client, `opencode-enrollment` by default: the device flow for agent machines |
| `OIDC_AUDIENCE` | `GATEWAY_OIDC__ACCESS_TOKEN_AUDIENCE` | the audience `/v1` requires on OIDC access tokens; default `pystino-api` |
| `OIDC_GROUPS_CLAIM` | `GATEWAY_OIDC__GROUPS_CLAIM` | which claim carries group membership (below) |
| `OIDC_GROUP_SYNC` | `GATEWAY_OIDC__GROUP_SYNC` | how often the groups claim applies: `every_login`, `first_login` or `never` |
| `OIDC_LOGOUT_URL` | `GATEWAY_OIDC__LOGOUT_URL` | for an issuer without `end_session_endpoint`: where the browser is sent to end the IdP session; `{redirect}` is where to land |
| `OIDC_SCOPES_JSON` | `GATEWAY_OIDC__SCOPES` | the scopes the console asks for, as a JSON list; `["openid","profile","email","groups"]` unless the issuer does not offer one ([the `groups` scope](oidc-generic-provider.md#providers-that-reject-the-groups-scope)). cerea-deploy writes it from the issuer's discovery; `deploy/compose.yaml` here sets the default literally |
| `OIDC_ADMIN_EMAIL` | `GATEWAY_OIDC__ADMIN_EMAILS` | comma-separated addresses that confer admin (below) |
| `OIDC_ADMIN_CLAIM`, `OIDC_ADMIN_CLAIM_VALUE` | `GATEWAY_OIDC__ADMIN_CLAIM`, `GATEWAY_OIDC__ADMIN_CLAIM_VALUES` | a claim path and the comma-separated values that confer admin (below) |
| `OIDC_LINK_BY_EMAIL` | `GATEWAY_OIDC__LINK_BY_EMAIL` | a first sign-in attaches to the existing account with the same verified email; off by default (below) |
| `OIDC_ACCEPTED_CLIENTS` | `GATEWAY_OIDC__ACCEPTED_CLIENTS` | the clients a bearer token's `azp` may name; empty derives the list from the console, chat and machine client ids |
| `PYSTINO_BOOTSTRAP_ADMIN_EMAIL` | `GATEWAY_BOOTSTRAP_ADMIN_EMAIL` | the bundled Authelia's first administrator; honoured once per deployment |

On top of the audience check, a bearer token whose `azp` (or `client_id`) is
present must name one of the accepted clients — the console, the chat and
the machine client. A token minted for any other purpose by any other client
is refused.

## What the gateway checks at startup

The compose deployments run the gateway in production mode, where these
refuse to start:

- an external IdP with neither an admin email list nor a claim pair:

  ```
  an external identity provider needs an admin rule: set OIDC_ADMIN_EMAIL,
  or OIDC_ADMIN_CLAIM with OIDC_ADMIN_CLAIM_VALUE, in .env
  ```

- half a claim pair (`OIDC_ADMIN_CLAIM` without `OIDC_ADMIN_CLAIM_VALUE`, or the reverse);
- an audience set with the accepted-clients list empty;
- a removed variable still set — `GATEWAY_OIDC__LINK_LOCAL_BY_EMAIL=true`,
  `GATEWAY_LOCAL_AUTH__ENABLED=true`, `GATEWAY_IDP__ENABLED=true`,
  `GATEWAY_OIDC__ADMIN_GROUPS`, or any `GATEWAY_LOCAL_AUTH__PASSWORD_RESET__*`
  — with an error naming what replaced it.

Warnings, logged at every start, never fatal:

- `OIDC_LINK_BY_EMAIL` on: a first sign-in with a verified email will attach
  to the existing non-admin account with that address; turn it off when the
  transition is done.
- `PYSTINO_BOOTSTRAP_ADMIN_EMAIL` set while `OIDC_KIND` is not `authelia`:
  the bootstrap is the bundled IdP's only, so the variable is ignored there.
- an audience set without `GATEWAY_OIDC__CHAT_CLIENT_ID`: every chat sign-in
  is refused until the chat's client id is set.

## The provider row follows the environment

At every start the gateway rewrites the provider row the environment names:
issuer, client, scopes, groups claim, group sync, admin rule and logout URL
are overwritten from `.env`, in one transaction under a database advisory
lock. When the issuer changed, the previous row is renamed
`previous-<date>-<id>` and disabled — never deleted — and a fresh row takes
the name `default`, so the callback path `/auth/callback/default` never
changes. Identities are stored by issuer and subject, so switching back
re-enables the earlier row with everyone's identities intact.

### Removing a previous provider nobody used

A disabled previous row stays on the Settings screen so people who signed in
there can be linked back. When nobody ever did (a provider you tried and
abandoned), **Remove** on the row clears it; the row's directory entries and
sync history go with it, and the removal is audited (`idp.remove`). The console
offers the button only where the gateway says the row is removable, and the
API (`DELETE /api/admin/identity-providers/<id>`) answers `409` for anything
else:

- an enabled row, which is the environment's and can never be removed here;
- a row with people: any account keyed on its issuer, or any linked identity at
  it, even a disabled one, so they can still be linked back;
- a row whose directory logins are bound to accounts.

Removing a row loses nothing you cannot get back from `.env`: pointing the
deployment at that issuer again creates the row afresh.

Every other row is disabled, and the start logs one line per disabled row
with its user count. The console keeps editing what the environment does not
own: the provisioning policy and its group mappings, and directory sync on
the active row (an Authelia users-file or Keycloak-admin pull, and SCIM 2.0
pushes at `/scim/v2/<provider>`; a first sync is a dry run until an
administrator confirms it).

## Groups

How the provider's answer about groups is applied is `OIDC_GROUP_SYNC`:
`every_login`, `first_login` (once, administered in the console afterwards)
or `never`. IdP group names mean whatever the console's group mappings say
they mean: the mappings live in the provisioning policy, not on the provider.
Bundled users carry only the `users` group in their users file; everything
else is console groups. Which claim carries the groups, and in which token it
appears, depends on the provider: see
[the per-provider table](oidc-generic-provider.md#groups-claim-by-provider).
A string claim is one group; it is never split on separators.

## Administrators

Administrators come from two environment rules, re-checked at every sign-in:

- **`OIDC_ADMIN_EMAIL`** grants admin when three things hold: the IdP
  asserted `email_verified` as the literal boolean `true` (the string
  `"true"` does not count), the normalised address is ASCII, and it is in
  the list.
- **`OIDC_ADMIN_CLAIM` plus `OIDC_ADMIN_CLAIM_VALUE`** grants admin when the
  claim carries one of the values, compared both as the IdP spells them and
  after the group mappings.

Either rule grants; a person is revoked only when neither matches.

**Rules only revoke what rules granted.** Every administrator carries a
provenance: `env` when a rule granted it, `manual` when an administrator's
decision in the console, `pystino admin grant`, the bootstrap or break-glass
did. A rule match never converts a manual administrator into a rule one, and a
rule mismatch never revokes a manual one. Removing an address from
`OIDC_ADMIN_EMAIL` takes admin away from the people **that rule granted** at
their next sign-in, and at the next gateway start a sweep drops the
email-rule grants whose address is no longer on the list. The sweep never
touches the claim rule's grants: with no fresh claims at startup there is
nothing to re-check them against, so they lapse at that person's next
sign-in.

Rules never make an administrator of someone who has not signed in yet: the
admin flag moves through the rules, an administrator's decision in the
console on someone who has signed in, the once-per-deployment bootstrap
(`PYSTINO_BOOTSTRAP_ADMIN_EMAIL`, the bundled Authelia's first
administrator), or break-glass.

### The last-admin guard

**The last active administrator is never revoked**, by any path: the console's
edit and delete, `pystino admin revoke`, a rule at sign-in, the startup sweep,
or a directory sync. "Active" means signed in, an administrator and enabled;
the placeholder account an invitation creates before someone's first sign-in
does not count, so a deployment cannot lock itself out by counting a person
who has never arrived.

The check is **serialized**. Under PostgreSQL every guard takes a
transaction-scoped advisory lock (`pg_advisory_xact_lock`) before it asks
whether another active administrator exists, so two guards running at once
(two administrators demoting each other, or a console demotion racing a
rule-driven revocation at a sign-in) queue up instead of both seeing
"another admin exists" and both committing. The rule never depends on
READ COMMITTED interleaving. A refused revocation from a sign-in is audited as
`admin.refused_last`.

## Link by email

With `OIDC_LINK_BY_EMAIL=true` (off by default; a warning prints at every
start while it is on), a first sign-in from an unknown identity attaches to
the one existing account whose stored address matches, when **all** of these
hold:

- the sign-in's `email_verified` is the literal boolean `true`;
- the address normalises to ASCII;
- exactly one account holds that address, and its address was itself
  verified or set by an administrator;
- the account is active, is **not an administrator**, is not still a
  placeholder awaiting its first sign-in, and has no identity at this issuer
  yet.

Links happen only at the two sign-in doors — the console callback and the
chat's announce — never on a plain `/v1` call. Every link is audited; every
refusal is logged with a numbered reason. An administrator who switches
providers moves through a merge or break-glass instead, never a link.

## Merging and deleting accounts

**Merge into…** (a person's edit panel on the Users page) moves one account's
identities, memberships, model access, keys, spend history and rules onto
another, in one transaction. An identity both accounts hold at one issuer is
dropped, and named in the preview. The confirmation asks you to type the
source's address, and optionally a reason (recorded in the audit log when given). The merge is irreversible: the undo is the
backup taken before it.

**Delete** on the Users page erases the person everywhere, chat included: the
gateway's rows, the bundled login, and the chat's records (conversations,
files, projects, assistants, knowledge bases, memories, skills, connectors,
settings, sessions and machine links). The preview names what is removed and
what is shared with other people, whose confirmation tick acknowledges it.
Spend history stays in the ledger, anonymised. If the chat cannot be
reached, the erasure is recorded as pending and retried with a backoff
(from 30 seconds up to an hour) until it confirms; `pystino erasure list` and
`pystino erasure retry <id>` show and force it (below).

## The audit trail

Every identity action lands in `identity_events`, an append-only table the
database itself protects: triggers refuse `UPDATE` and `DELETE`. It records
user, admin, password, link, merge, break-glass, provider re-seed and
removal, and bootstrap actions, with the actor, the target, and the reason when one was
given. It is kept forever; archive it with SQL when it grows.

Read it at `GET /api/admin/identity-events?user_id=…&action=…&before=…`,
newest first; `user_id` matches either side of an action. The Users page
shows the same trail per person (**Activity**).

## Break-glass

When nobody can administer, or the identity provider itself is gone. Two
things have to be true before it can work, and they explain the two steps:

- the gateway must be pointed at the **bundled Authelia**, because break-glass
  creates or re-enables a bundled login and prints its password. If the
  deployment is on an external provider that is now unreachable, that means
  switching `.env` to the bundled one first; and
- you need shell access to the host, which already implies the database.

With cerea-deploy, one command does both:

```sh
./configure --break-glass --admin-email ops@example.org --reason "IdP outage"
```

It rewrites `.env` to the bundled Authelia (your external settings survive in
the `.env.bak-<timestamp>` it makes, and nowhere else), starts PostgreSQL and
runs the bootstrap, runs `pystino break-glass` in a one-off gateway container,
brings the stack up, and prints the sign-in URL, the login and a one-time
password, once. Each person with an agent machine has to re-enroll it afterwards, because its tokens name the old issuer; tell them before you switch. In a Pystino-only deployment, edit `deploy/.env` to the
bundled Authelia yourself, then run the command that step wraps:

```bash
docker compose run --rm --no-deps gateway pystino break-glass \
  --email you@example.org --reason "…"
```

`--reason` is optional; it is recorded in the audit log when given. The command finds or
creates the account with that email, creates or re-enables their bundled
login, grants admin (as `manual`, so no rule can take it back) and prints a
one-time password once, to stdout, never to logs. Users, memberships, data and
chat records are untouched. When the email names several accounts it lists
them and takes `--user-id`.

To go back to your external provider afterwards, switch `.env` back with the
values from the backup file; the accounts and identities that provider held
resolve again.

**If you suspect the deployment itself was compromised, rotate
`GATEWAY_SESSION_SECRET`** (`openssl rand -hex 32`, then restart): it signs
the console's sessions, so rotating it signs everyone out, including anyone
holding a stolen cookie. It is deliberately separate from
`GATEWAY_SECRET_KEY`, which encrypts provider credentials and must **not** be
rotated casually.

For someone who **can** already sign in, `docker compose exec gateway pystino
admin grant you@example.org` grants admin without touching anything else.

## The command line

Run inside the gateway container — `docker compose exec gateway pystino …`,
or `docker compose run --rm --no-deps gateway pystino …` when the gateway is
not up:

| Command | What it does |
|---|---|
| `pystino break-glass --email <address> [--reason "<why>"] [--login <name>]` | recover admin access when nobody can administer: find or create the account with that email, create or re-enable their bundled login, grant admin, and print a one-time password — once, to stdout, never to logs. Users, memberships, data and chat records are untouched. When the email names several accounts, it lists them and takes `--user-id`. |
| `pystino admin grant\|revoke <email> [--issuer <url>]` | the ordinary grant or revoke for someone who can already sign in (the account must exist, and an email held at several issuers needs `--issuer`); a revoke that would leave no active administrator is refused |
| `pystino idp check [--discovery-only\|--device\|--token <jwt>] [--issuer <url>]` | probe the configured issuer: discovery and JWKS, and — with `--device`, which prints a URL to sign in at, or `--token`, which validates a pasted access token — the `sub`, the email and `email_verified` with its JSON type, the groups claim, the audience and client, and whether each admin rule would grant; exits non-zero when discovery fails, the audience is wrong, or no rule would grant |
| `pystino erasure list` | the pending chat erasures: id, user, attempts, next attempt, last error |
| `pystino erasure retry <id>` | force one pending erasure's next attempt now, ignoring its backoff |
| `pystino email export-env` | the mail configuration in force, as `SMTP_*` `KEY=VALUE` lines, password included — for moving console-era mail settings into the environment |
