# Identity: OIDC providers

People sign in to the console, and to the chat when it is deployed, through
OpenID Connect only. There is no password sign-in in the gateway itself. Use
the **bundled Authelia** that `deploy/` (and cerea-deploy) can run beside the
gateway, or bring **your own issuer**: any server that speaks OIDC discovery.

## Configuration

The deployment's `.env` seeds the first provider; after the first start, the
console (**Settings → Identity providers**) owns it and any others you add.

| Variable | Meaning |
|---|---|
| `OIDC_ISSUER` | the issuer URL, exactly as the discovery document's `issuer` states it |
| `OIDC_INTERNAL_BASE_URL` | where the gateway reaches the issuer from inside the network (bundled Authelia: `http://authelia:9091/authelia`); empty means the issuer URL itself |
| `OIDC_KIND` | `authelia` for the bundled one (users-file sync and user management in the console), `generic` otherwise |
| `OIDC_CONSOLE_CLIENT_ID`, `OIDC_CONSOLE_CLIENT_SECRET` | the console's client |
| `OIDC_CHAT_CLIENT_ID`, `OIDC_CHAT_CLIENT_SECRET` | the chat's client, when the chat is deployed |
| `OIDC_GROUPS_CLAIM` | which claim carries group membership (below) |
| `OIDC_AUDIENCE` | the audience `/v1` requires on OIDC access tokens; default `pystino-api` |
| `OIDC_LOGOUT_URL` | for an issuer without `end_session_endpoint`: where the chat sends the browser to end the IdP session; `{redirect}` is where to land |
| `PYSTINO_BOOTSTRAP_ADMIN_EMAIL` | the first sign-in with this verified email becomes an administrator, while none exists |

Changing the issuer later re-provisions everyone. Users are keyed on
`(issuer, subject)`, never on email, so a new issuer means new user rows with
no groups and no admin flag.

## Registering the clients on your provider

Register a **confidential web client** per application:

| Client | Redirect URI | Scopes |
|---|---|---|
| console | `<origin>/auth/callback/default` | `openid profile email groups` |
| chat | `<origin>/chat/login/callback` | `openid profile email groups` |

- Redirect URIs are compared as exact strings. Never use a wildcard: an open
  redirect on an OIDC client hands the authorization code to whoever asks.
- Take the issuer from the provider's own discovery document
  (`https://<provider>/.well-known/openid-configuration`). The gateway
  compares `iss` byte for byte.
- **For `/v1`**, the chat and coding agents send the person's **access
  token**. It must be a signed JWT (RS256), carry `OIDC_AUDIENCE` in `aud`,
  and carry the groups claim itself: `/v1` validates the token locally and
  never calls userinfo.

The flow needs no configuration: authorization code with PKCE (S256), `state`
and `nonce`, ID-token signature checked against the provider's JWKS (refetched
once on an unknown key id, so key rotation needs no restart), and `iss`,
`aud`, `exp` and `sub` checked. Discovery is read at startup: restart the
gateway after changing the issuer.

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

Per provider, the console decides:

- **where groups come from**: the token's claim, a directory sync, or the
  console only;
- **how often** the provider's answer applies: every login, first login only
  (the default), or never;
- **where admin comes from**: the console (the default), or a claim and the
  values that grant it.

Directory sync can pull from the bundled Authelia's users file or from
Keycloak's admin API, and accepts SCIM 2.0 pushes at `/scim/v2/<provider>`
(Entra ID, Okta, Authentik). A first sync is a dry run until an administrator
confirms it.

Break-glass, when nobody can administer:
`docker compose exec gateway pystino admin grant you@example.org`.

## Signing out

Signing out of the console or the chat ends the session in both, and at the
provider, so the next sign-in asks for a password. When the provider
publishes an `end_session_endpoint`, it is used. When it does not, set the
provider's **Logout URL** in the console, and `OIDC_LOGOUT_URL` for the chat.
For the bundled Authelia both are `<origin>/authelia/logout?rd={redirect}`,
which the deployments set for you.

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
   ```

   Group names arrive as full paths (`my-group/sub-group`); name group-based
   model access and admin claims accordingly.
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

## The bundled Authelia

With the `authelia` profile on, Authelia runs beside the gateway, from the
stock image, with the configuration in `authelia/configuration.yml`.

- **The issuer is a subpath** of the public origin: `<origin>/authelia`.
  Every endpoint in its discovery document carries the same prefix, and the
  proxy forwards `/authelia/*` without stripping it.
- **Server-to-server calls stay inside the network.** The gateway and the
  chat reach it at `http://authelia:9091/authelia`, with forwarded headers
  naming the public issuer, so no container needs to trust the proxy's
  certificate.
- **Three clients:** `pystino-console` and `cerea` (confidential, consent
  implied, since both are first-party) and `opencode-enrollment` (public,
  for coding agents: loopback and device flows, with a long-lived refresh
  token). Access tokens are RS256 JWTs carrying the `pystino-api` audience
  and the `groups` claim, and live one hour.
- **Users** are a file on a volume, which Authelia reloads when it changes.
  The first account comes from `AUTHELIA_ADMIN_*` on the first `up`. After
  that, administrators manage people in the console (**Settings → Identity
  providers → People**).
- **Password only**: the policy is one factor. TOTP or WebAuthn would need
  the policy raised and a real notifier; the bundled one writes to a file.
- It needs a **dotted host name**: browsers refuse its session cookie on an
  IP address or a dotless name.

To develop against Keycloak instead, `deploy/dev/keycloak/` has one.
