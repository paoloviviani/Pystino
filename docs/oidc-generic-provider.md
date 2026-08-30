# Connecting a generic OIDC identity provider

The gateway (management API and console) and the chat both authenticate humans
through OpenID Connect. There is **no bundled identity provider** — any server
speaking OIDC discovery works, and this page is the checklist plus a worked
example for GitLab. If you would rather not run any IdP, enable local
email + password sign-in instead (ADR 0043, `GATEWAY_LOCAL_AUTH__ENABLED`).

The settings live in `deploy/.env` and reach the containers through
`docker-compose.proxy.yml`, which passes `GATEWAY_OIDC__*` (and `CHAT_OIDC__*`)
through unchanged. All double-underscore form, e.g. `GATEWAY_OIDC__ISSUER`.

## What the gateway needs from you

| Setting | Meaning |
|---|---|
| `GATEWAY_OIDC__ENABLED` | `true` to turn the flow on. Everything below is inert until this is set. |
| `GATEWAY_OIDC__ISSUER` | The provider's issuer URL, exactly as its discovery document's `iss` says. Trailing slash stripped automatically. |
| `GATEWAY_OIDC__CLIENT_ID` / `CLIENT_SECRET` | The client you register on the provider (below). |
| `GATEWAY_OIDC__REDIRECT_URI` | Defaults to `<PUBLIC_ORIGIN>/auth/callback` behind the proxy overlay. Register **exactly** this value on the provider — the gateway sends one URI and the provider compares it as a string. |
| `GATEWAY_OIDC__GROUPS_CLAIM` | Which claim carries group membership. The provider-dependent one; see the table below. Dotted paths walk nested objects; a literal dot is escaped `\.`. |
| `GATEWAY_OIDC__ADMIN_GROUPS` | JSON list. Membership of any named group grants `is_admin`; empty means admin is a manual database decision. |
| `GATEWAY_OIDC__ACCESS_TOKEN_AUDIENCE` | Empty by default and fine to leave: API keys are the `/v1` credential. Set it only if you want OIDC **access tokens** accepted on `/v1` too (ADR 0040) — the provider must then put that audience on its tokens. |

The flow itself is fixed and needs no configuration: authorization-code with
PKCE S256, `state` and `nonce`, ID-token signature validated against the
provider's JWKS (refetched once on an unknown key id, so rotation needs no
restart), `iss`/`aud`/`exp`/`sub` checked, groups reconciled on every login.

Restart the gateway after changing these — OIDC discovery is read once at
startup.

## The provider's side

1. Register a **confidential web client** with:
   - redirect URI: `<PUBLIC_ORIGIN>/auth/callback` — exact, never a wildcard;
     an open redirect on an OIDC client hands the authorization code to whoever
     asks for it;
   - scopes: `openid`, `profile`, `email`;
   - for the chat (if deployed): a **second** client with
     `<PUBLIC_ORIGIN>/chat/auth/callback`. Two services that authenticate
     people are two clients, each revocable on its own.
2. Take the issuer URL from the provider's own discovery document
   (`https://<provider>/.well-known/openid-configuration`) — never from a
   README. The gateway compares `iss` byte-for-byte.
3. Decide the groups claim with the table below, remembering **where each
   claim lives**: the console login merges `userinfo` into the ID token's
   claims, but a `/v1` access token is validated locally — whatever is not in
   the token is not seen.

| Provider | Claim | Where |
|---|---|---|
| GitLab | `groups_direct` (direct members; in the ID token) — or `groups` (direct + inherited; **userinfo only**) | ID token / userinfo |
| Microsoft Entra ID | `groups` (object IDs, unless claims-transformed) | either |
| Keycloak (if you run one) | `realm_access.roles` | ID token |
| Namespaced (Auth0 et al.) | `https://example\.org/groups` — the full path is tried as a flat key first | either |

A bare string claim is treated as **one** group, not split on separators;
group names containing spaces are common and inventing two groups out of one
would grant the wrong access.

4. Choose `GATEWAY_OIDC__ADMIN_GROUPS` to match a claim that is present **in
   the ID token** when you use `/v1` access tokens. Admin follows group
   membership in both directions on every login, so an admin group named after
   a userinfo-only claim is silently lost (and can be *revoked*) during a
   token-only reconciliation. A group the admin is a direct member of, read
   via `groups_direct`, works on both paths.

Users are provisioned on first login, keyed on `(issuer, subject)` — never on
email, which is mutable and reassignable. Consequence worth knowing before you
change anything: **changing `ISSUER` re-provisions every user as a new row
with no groups, no default billing group and no admin flag.** Free on a fresh
deployment; a migration nobody has written on one with history.

## Worked example: GitLab

Verified against gitlab.com's live discovery document (2026-08-29) and GitLab's
"GitLab as OpenID Connect identity provider" documentation; see the caveats at
the end for the one part that still needs a live test.

1. **Register the application** on your GitLab instance:
   - gitlab.com or group-owned: avatar → *Edit profile* → *Access →
     Applications* → *Add new application* (or group → *Settings →
     Applications*; instance-wide on self-managed via the *Admin* area).
   - Name: anything ("LLM gateway"). Redirect URI:
     `https://<PUBLIC_HOST>:<HTTPS_PORT>/auth/callback`.
   - Scopes: `openid`, `profile`, `email` — `openid` is the one that carries
     group memberships. Mark the application confidential (a secret is shown
     once; copy it immediately, "Renew secret" rotates it later).
2. **Set the environment** in `deploy/.env`:

   ```bash
   GATEWAY_OIDC__ENABLED=true
   GATEWAY_OIDC__ISSUER=https://gitlab.com          # self-managed: https://gitlab.example.com
   GATEWAY_OIDC__CLIENT_ID=<Application ID>
   GATEWAY_OIDC__CLIENT_SECRET=<Secret>
   GATEWAY_OIDC__GROUPS_CLAIM=groups_direct
   GATEWAY_OIDC__ADMIN_GROUPS=["<group-your-admin-is-a-direct-member-of>"]
   # GATEWAY_OIDC__ACCESS_TOKEN_AUDIENCE=   # leave empty; /v1 stays on API keys
   ```

   The redirect URI needs no line: the proxy overlay defaults it to
   `<PUBLIC_ORIGIN>/auth/callback`, which is what you registered in step 1.
   `groups_direct` carries GitLab's group paths (`my-group/sub-group`) in the
   **ID token**, so it works for both console login and `/v1` tokens; the
   richer `groups` claim (direct + inherited) is userinfo-only and therefore
   invisible to `/v1`. Group names arrive as full paths — name
   `ADMIN_GROUPS` and any group-based model access accordingly.

3. **Bring the stack up** and sign in:

   ```bash
   docker compose --env-file deploy/.env \
     -f deploy/compose/docker-compose.yml -f deploy/compose/docker-compose.smoke.yml \
     -f deploy/compose/docker-compose.redaction.yml \
     -f deploy/compose/docker-compose.proxy.yml up -d --build
   ```

   `GET /auth/methods` should now report `{"local":true,"oidc":true}`; the
   console shows the password form **and** a "Sign in with SSO" link. If you
   want SSO only, set `GATEWAY_LOCAL_AUTH__ENABLED=false`.

### GitLab caveats, verified at source

- **`nonce`: unverified.** The gateway always sends `nonce` and requires the
  ID token to echo it (replay protection, ADR 0011). GitLab's documentation
  does not mention the nonce claim, and gitlab.com's discovery document does
  not advertise it. If sign-in fails with "ID token nonce does not match the
  login attempt", that is this — resolve it by testing with your GitLab
  version before relying on the integration, not by weakening the check.
- **No RP-initiated logout.** gitlab.com's discovery advertises no
  `end_session_endpoint`, so "Sign out" can only drop the gateway's own
  session; the console falls back to that gracefully. GitLab's own login
  persists — signing in again is one click and no password, which is the
  behaviour the gateway cannot change from its side.
- **RS256 only**, per gitlab.com's live discovery (`id_token_signing_alg_values_supported`),
  which is what the gateway accepts — nothing to configure.
- **Email claim**: only present for users who have set a *public* email in
  their GitLab profile. An account can sign in without it — identity is
  `(issuer, subject)` — but the console shows the subject instead of an
  address until one is public.

## Other providers, same shape

Entra ID, Okta, Auth0, Keycloak (hosted elsewhere) and friends differ only in
the table's values: issuer URL, the groups claim, and whether an audience
mapper is needed for `/v1` tokens. Register the client, point
`GATEWAY_OIDC__ISSUER` at the discovery document's `iss`, set
`GATEWAY_OIDC__GROUPS_CLAIM`, and restart. The live checks to re-run after any
change: `./scripts/test_console_live.py` (session, CSP, pagination) and
`./scripts/test_public_tls_live.py` (TLS, cookies, that nothing else is
published).
