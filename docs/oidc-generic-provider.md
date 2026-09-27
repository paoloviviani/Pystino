# Identity: the OIDC provider

People sign in to the console, and to the chat when it is deployed, through
OpenID Connect only. There is no password sign-in in the gateway itself. Use
the **bundled Authelia** that `deploy/` (and cerea-deploy) can run beside the
gateway, or bring **your own issuer**: any server that speaks OIDC discovery.

## One provider, set in the environment

One provider at a time, named by the `OIDC_*` variables of the deployment's
`.env`. The gateway takes its settings from there **at every start** and
rewrites its provider row to match, so the console shows the provider
read-only. What the console edits is the rest of identity: the provisioning
policy — who may come to exist, which claim names their groups, what an IdP
group means here (under **Settings**) — and the people themselves (the
**Users** page).

If you need several sources of users, federate them in your own IdP
(Keycloak, Authentik and the like) and point the stack at it.

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

Every other row is disabled, and the start logs one line per disabled row
with its user count. The console keeps editing what the environment does not
own: the provisioning policy and its group mappings, and directory sync on
the active row (an Authelia users-file or Keycloak-admin pull, and SCIM 2.0
pushes at `/scim/v2/<provider>`; a first sync is a dry run until an
administrator confirms it).

## Groups and administrators

Choose the groups claim for your provider, and check **where** it appears:
the console login merges userinfo into the ID token's claims, but `/v1` sees
only what is in the access token.

| Provider | Claim | Where |
|---|---|---|
| Bundled Authelia | `groups` | ID token, access token, userinfo |
| GitLab | `groups_direct` (direct memberships), or `groups` (with inherited ones) | `groups_direct` in the ID token; `groups` in userinfo only |
| Microsoft Entra ID | `groups` (object IDs unless transformed) | either |
| Keycloak | `realm_access.roles`, or a groups mapper | ID token |
| Namespaced (Auth0 and others) | e.g. `https://example\.org/groups` (dotted paths walk nested objects; escape a literal dot) | either |

A string claim is one group; it is never split on separators.

How the provider's answer about groups is applied is `OIDC_GROUP_SYNC`:
`every_login`, `first_login` (once, administered in the console afterwards)
or `never`. IdP group names mean whatever the console's group mappings say
they mean — the mappings live in the provisioning policy, not on the
provider. Bundled users carry only the `users` group in their users file;
everything else is console groups.

Administrators come from two environment rules, re-checked at every sign-in:

- **`OIDC_ADMIN_EMAIL`** grants admin when three things hold: the IdP
  asserted `email_verified` as the literal boolean `true` (the string
  `"true"` does not count), the normalised address is ASCII, and it is in
  the list.
- **`OIDC_ADMIN_CLAIM` plus `OIDC_ADMIN_CLAIM_VALUE`** grants admin when the
  claim carries one of the values — compared both as the IdP spells them and
  after the group mappings.

Either rule grants; a person is revoked only when neither matches. Removing
an address from the list takes its admin away at that person's next sign-in
and at the next gateway start, where a sweep drops every rule-granted admin
no longer on the list. The last active administrator is never revoked. Rules
never make an administrator of someone who has not signed in yet: the admin
flag moves through the rules, an administrator's decision in the console on
someone who has signed in, the once-per-deployment bootstrap, or break-glass.

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

## The bundled Authelia

With the `authelia` profile on, Authelia runs beside the gateway, from the
stock image, with the configuration in `deploy/authelia/configuration.yml`.

- **The issuer is a subpath** of the public origin: `<origin>/authelia`.
  Every endpoint in its discovery document carries the same prefix, and the
  proxy forwards `/authelia/*` without stripping it.
- **Server-to-server calls stay inside the network.** The gateway and the
  chat reach it at `http://authelia:9091/authelia`, with forwarded headers
  naming the public issuer, so no container needs to trust the proxy's
  certificate.
- **Three clients:** `pystino-console` and `cerea` (confidential, consent
  implied, since both are first-party) and `opencode-enrollment` (public,
  for agent machines: loopback and device flows, with a long-lived refresh
  token). Access tokens are RS256 JWTs carrying the `pystino-api` audience
  and the `groups` claim, and live one hour.
- **Accounts are managed from the console's Users page.** Add user creates
  the Authelia login and the gateway account together and shows a one-time
  password once; create sign-in gives an existing account a bundled login;
  reset password mints a fresh one-time password; disable, enable and delete
  are there, and so is merge into. With SMTP configured, people reset their
  own password from the sign-in page; without it, only an administrator can.
- **Password only**: the policy is one factor, and TOTP and WebAuthn are
  switched off in the rendered configuration. A second factor comes from an
  external IdP such as Keycloak, configured through the environment.
- It needs a **dotted host name**: browsers refuse its session cookie on an
  IP address or a dotless name.

## Deleting and merging accounts

**Delete** on the Users page erases the person everywhere, chat included:
the gateway's rows, the bundled login, and the chat's records (conversations,
files, projects, assistants, knowledge bases, memories, skills, connectors,
settings, sessions and machine links). The preview names what is removed and
what is shared with other people, whose confirmation tick acknowledges it.
Spend history stays in the ledger, anonymised. If the chat cannot be
reached, the erasure is recorded as pending and retried until it confirms.

**Merge into…** moves one account's identities, memberships, model access,
keys, spend history and rules onto another, in one transaction. An identity
both accounts hold at one issuer is dropped, named in the preview. The
confirmation asks for the source's address and a reason. The merge is
irreversible — the undo is the backup taken before it.

## The audit trail

Every identity action lands in `identity_events`, an append-only table the
database itself protects: triggers refuse `UPDATE` and `DELETE`. It records
user, admin, password, link, merge, break-glass, provider re-seed and
bootstrap actions, with the actor, the target, and the reason when one was
given. It is kept forever; archive it with SQL when it grows.

Read it at `GET /api/admin/identity-events?user_id=…&action=…&before=…`,
newest first; `user_id` matches either side of an action. The Users page
shows the same trail per person.

## The command line

Run inside the gateway container — `docker compose exec gateway pystino …`,
or `docker compose run --rm --no-deps gateway pystino …` when the gateway is
not up:

| Command | What it does |
|---|---|
| `pystino break-glass --email <address> --reason "<why>" [--login <name>]` | recover admin access when nobody can administer: find or create the account with that email, create or re-enable their bundled login, grant admin, and print a one-time password — once, to stdout, never to logs. Users, memberships, data and chat records are untouched. When the email names several accounts, it lists them and takes `--user-id`. |
| `pystino admin grant\|revoke <email> [--issuer <url>]` | the ordinary grant or revoke for someone who can already sign in (the account must exist, and an email held at several issuers needs `--issuer`); a revoke that would leave no active administrator is refused |
| `pystino idp check [--discovery-only\|--device\|--token <jwt>] [--issuer <url>]` | probe the configured issuer: discovery and JWKS, and — with `--device`, which prints a URL to sign in at, or `--token`, which validates a pasted access token — the `sub`, the email and `email_verified` with its JSON type, the groups claim, the audience and client, and whether each admin rule would grant; exits non-zero when discovery fails, the audience is wrong, or no rule would grant |
| `pystino erasure list` | the pending chat erasures: id, user, attempts, next attempt, last error |
| `pystino erasure retry <id>` | force one pending erasure's next attempt now, ignoring its backoff |
| `pystino email export-env` | the mail configuration in force, as `SMTP_*` `KEY=VALUE` lines, password included — for moving console-era mail settings into the environment |

## Registering the clients on your provider

Register a **confidential web client** per application, and the public
machine client:

| Client | Redirect URI | Scopes |
|---|---|---|
| console (`pystino-console`) | `<origin>/auth/callback/default` | `openid profile email groups` |
| chat (`cerea`) | `<origin>/chat/login/callback` | `openid profile email groups` |
| machine (`opencode-enrollment`) | device flow, plus loopback `http://localhost/callback` | `openid profile email groups offline_access` |

The machine client needs the device-flow and refresh-token grants, and no
secret.

- Redirect URIs are compared as exact strings. Never use a wildcard: an open
  redirect on an OIDC client hands the authorization code to whoever asks.
- Take the issuer from the provider's own discovery document
  (`https://<provider>/.well-known/openid-configuration`). The gateway
  compares `iss` byte for byte.
- **For `/v1`**, the chat and coding agents send the person's **access
  token**. It must be a signed JWT (RS256), carry `OIDC_AUDIENCE` in `aud`,
  name an accepted client in `azp`, and carry the groups claim itself:
  `/v1` validates the token locally and never calls userinfo.

The flow needs no configuration: authorization code with PKCE (S256), `state`
and `nonce`, ID-token signature checked against the provider's JWKS (refetched
once on an unknown key id, so key rotation needs no restart), and `iss`,
`aud`, `exp` and `sub` checked. Discovery is read at startup: restart the
gateway after changing the issuer.

## Signing out

Signing out of the console or the chat ends the session in both, and at the
provider, so the next sign-in asks for a password. When the provider
publishes an `end_session_endpoint`, it is used. When it does not, set
`OIDC_LOGOUT_URL` — `GATEWAY_OIDC__LOGOUT_URL` in the gateway — and the chat
reads the same value. For the bundled Authelia it is
`<origin>/authelia/logout?rd={redirect}`, which the deployments set for you.

## Worked example: GitLab

1. On GitLab, add an application (**Edit profile → Applications**, or a
   group's or the instance's Applications page). Mark it confidential, with
   the console's redirect URI and the scopes `openid profile email`. `openid`
   is the one that carries group memberships. Copy the secret: it is shown
   once. Add a second application for the chat if you deploy it.
2. In `.env`, leave `authelia` out of `COMPOSE_PROFILES` and set:

   ```bash
   OIDC_ISSUER='https://gitlab.com'          # self-managed: your instance's URL
   OIDC_INTERNAL_BASE_URL=''
   OIDC_KIND='generic'
   OIDC_CONSOLE_CLIENT_ID='<application id>'
   OIDC_CONSOLE_CLIENT_SECRET='<secret>'
   OIDC_GROUPS_CLAIM='groups_direct'
   OIDC_ADMIN_EMAIL='you@example.org'
   ```

   Group names arrive as full paths (`my-group/sub-group`); name group-based
   model access and admin claims accordingly. Run `pystino idp check
   --device` after the first sign-in to see what GitLab actually asserted —
   the email, its `email_verified` type, and whether the admin rule would
   grant.
3. `docker compose up -d --wait`, and sign in at `<origin>/`.

What to know about GitLab:

- **No `nonce` in its ID tokens** (gitlab.com's discovery lists none). The
  gateway sends one and accepts a token without it, but refuses a token whose
  nonce does not match the browser's flow.
- **No `end_session_endpoint`.** Signing out ends the gateway's session only,
  and GitLab's own session persists: signing in again is one click.
- **RS256 only**, which is what the gateway expects.
- **Email** is present only for users with a public email on their profile.
  They can still sign in; the console shows the subject until an email
  appears.
