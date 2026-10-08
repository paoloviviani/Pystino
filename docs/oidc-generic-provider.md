# Identity: the OIDC provider

!!! info "For operators"

    Registering an external OpenID Connect provider with the gateway; what the gateway then decides about administrators, linking and recovery is [Identity](identity.md).

This page is about the **provider**: what to register on it, which claims to
send, and what goes wrong at the seam between it and the gateway. The
variables that name it (`OIDC_ISSUER`, the client ids and secrets, the admin
rules) and everything the gateway does with the answer are on
[Identity](identity.md). To use the bundled Authelia instead, see
[Bundled accounts](bundled-accounts.md).

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


## Groups claim by provider

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
`OIDC_GROUPS_CLAIM` names it, and [how it is applied](identity.md#groups) is
`OIDC_GROUP_SYNC`.

## Providers that reject the `groups` scope

Some providers publish a `groups` *claim* but no `groups` *scope*, and answer a
sign-in that asks for it with `invalid_scope`. Infomaniak's discovery lists
`scopes_supported = openid, profile, email, phone`, for one. The gateway asks
for the scopes in `GATEWAY_OIDC__SCOPES`, so the fix is to drop `groups` from
that list and from the chat's `OIDC_SCOPES`, which are one setting spelled two
ways (a JSON list for the gateway, words for the chat).

With the deploy kit, `./configure --idp external` reads the issuer's discovery
document and asks only for the scopes it lists (`openid` always stays), writes
both spellings (`OIDC_SCOPES` and `OIDC_SCOPES_JSON`) and says so when `groups`
is dropped. `./configure --check` warns about a configured scope the issuer
does not list, and `./configure --oidc-scopes 'openid profile email'` (or
`--set OIDC_SCOPES=…`) sets them by hand. In `deploy/` here, edit the
`GATEWAY_OIDC__SCOPES` line of `compose.yaml`.

Without the scope, group-based features (group sync, quotas and billing by
group, an admin claim rule on `groups`) work only if the provider puts a groups
claim in its tokens regardless. `pystino idp check --device` prints the claim.
With no groups claim, people sign in with no group memberships, and an admin
rule by email still works.

The machine client follows the same rule: galopin asks for `offline_access`
only when the issuer lists it. A provider that grants refresh tokens without
the scope is still asked, and `galopin enroll` stops with a message instead of
enrolling a machine that would lose its sign-in within the hour, if no refresh
token comes back. Enable refresh tokens for `opencode-enrollment` at the
provider.

## Keycloak: the audience mapper

A Keycloak access token carries **no `aud` claim at all** unless an audience
mapper puts one there; it has only `azp`, which says who *asked* for the token,
not who it is *for*. The gateway checks the audience (`OIDC_AUDIENCE`,
`pystino-api` by default) rather than `azp`, because a token minted for any
other purpose by a permitted client would otherwise be accepted at the API. So
every Keycloak client that is to call `/v1` (the console's, the chat's and the
machine client) needs a protocol mapper of type *Audience* that adds `OIDC_AUDIENCE` to
the access token.

Without it nothing fails loudly at the provider. The symptom is that **every
chat sign-in fails**, and the only trace is a `WARNING` line in the gateway's log:

```
access token rejected: access token audience ['account'] does not include 'pystino-api'; the client needs an audience mapper naming this gateway
```

(the chat's announce door logs the same reason as `announce: token rejected: …`).
The list in the message is whatever `aud` the token did carry. Because it is
a `WARNING`, it shows without raising the log level.

## Signing out

Signing out of the console or the chat ends the session in both, and at the
provider, so the next sign-in asks for a password. When the provider
publishes an `end_session_endpoint`, it is used. When it does not, set
`OIDC_LOGOUT_URL` — `GATEWAY_OIDC__LOGOUT_URL` in the gateway — and the chat
reads the same value. For the bundled Authelia it is
`<origin>/authelia/logout?rd={redirect}`, which the deployments set for you.

## User sync

**Optional, and for an external provider only.** People appear at their first
sign-in whether or not you set it up. **User sync…** on the provider's card in
the console (it does not appear for the bundled Authelia, whose accounts the
[Users page](bundled-accounts.md) manages directly) keeps the user list in step
with the provider: people exist, with their groups and pre-assigned access,
before their first sign-in, and are deactivated when removed there. A
directory never deletes anyone, grants and revokes only what it granted, and
never deactivates half the deployment because it returned an empty list.

The dialog offers the adapters the provider kind can support:

| Adapter | How the directory tells the gateway about people |
|---|---|
| Off (default) | people appear at their first sign-in |
| Keycloak admin API | pulled on a schedule, with a service-account client |
| SCIM 2.0 | the provider pushes changes to `/scim/v2/<provider>` |
| Authelia users file | not needed: the Users page manages bundled accounts |

A provider with no listing API (OIDC itself has none) is just-in-time only,
and the dialog says so. A first pull is a **dry run**: nothing changes until
an administrator has looked at what the adapter would do and confirmed it.

## Worked example: GitLab

1. On GitLab, add an application (**Edit profile → Applications**, or a
   group's or the instance's Applications page). Mark it confidential, with
   the console's redirect URI and the scopes `openid profile email`. `openid`
   is the one that carries group memberships. Copy the secret: it is shown
   once. Add a second application for the chat if you deploy it, also
   confidential, with the chat's redirect URI and the same scopes.
2. In `.env`, leave `authelia` out of `COMPOSE_PROFILES` and set:

   ```bash
   OIDC_ISSUER='https://gitlab.com'          # self-managed: your instance's URL
   OIDC_INTERNAL_BASE_URL=''
   OIDC_KIND='generic'
   OIDC_CONSOLE_CLIENT_ID='<application id>'
   OIDC_CONSOLE_CLIENT_SECRET='<secret>'
   OIDC_GROUPS_CLAIM='groups_direct'
   OIDC_ADMIN_EMAIL='you@example.org'
   # with the chat:
   OIDC_CHAT_CLIENT_ID='<chat application id>'
   OIDC_CHAT_CLIENT_SECRET='<chat secret>'
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
- **Its access tokens are opaque**, not JWTs, so the gateway cannot read who
  issued one or check a signature. It asks GitLab instead, at the
  `introspection_endpoint` its discovery advertises (`/oauth/introspect`,
  RFC 7662), authenticating with `client_secret_basic`. GitLab answers
  `active: true` only to the application the token was issued to, so the
  chat's tokens (the chat signing in through `POST /v1/session/announce`, and
  every `/v1` call it then makes) are introspected with **the chat
  application's own id and secret**: `OIDC_CHAT_CLIENT_SECRET` must reach the
  gateway (`GATEWAY_OIDC__CHAT_CLIENT_SECRET`), not only the chat. Without it
  every chat sign-in is refused, and the gateway log says
  `inactive for client '<console id>'`. A token is accepted only when GitLab
  says it is active, its `client_id` is the application that asked, and its
  `exp` is in the future; it then counts as issued to that client (`azp`), so
  `ACCEPTED_CLIENTS` and the chat-client check apply as they do to a JWT.
  GitLab's answer carries no `sub`, so the gateway reads it from userinfo with
  the same token: the person is `(issuer, GitLab user id)`, exactly what a
  console sign-in produces, so console and chat land on the same account. The
  chat's application needs the `openid` scope for that (`openid profile
  email`, as above).
- **Revocation takes up to a minute.** Answers are cached in the gateway,
  keyed by a hash of the token (never the token), for 60 seconds or until the
  token's own `exp`, whichever is sooner; a refusal is cached for 10 seconds.
  A token revoked on GitLab therefore keeps working here for at most 60
  seconds, and `/v1` calls do not reach GitLab on every request.
- **Why a chat sign-in failed** is in `docker compose logs gateway`, one
  WARNING per refusal beginning `announce: token rejected:` (or `access token
  rejected:` for other `/v1` calls), naming the reason — not a JWT and no
  provider vouched for it, inactive for a client, a client other than the
  chat, an expired token, a client outside `ACCEPTED_CLIENTS` — and never the
  token itself.
- **Email** is present only for users with a public email on their profile.
  They can still sign in; the console shows the subject until an email
  appears.
