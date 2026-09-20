# Connecting a generic OIDC identity provider

The gateway (management API and console) and the chat both authenticate humans
through OpenID Connect. Any server speaking OIDC discovery works, and this page
is the checklist plus a worked example for GitLab. If you would rather not run
any IdP, enable local email + password sign-in instead (ADR 0043,
`GATEWAY_LOCAL_AUTH__ENABLED`) — and if you would rather not run one *elsewhere*,
the installer can deploy one alongside the gateway: the bundled Authelia and
Keycloak sections at the end of this page.

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
     `<PUBLIC_ORIGIN>/chat/login/callback`. Two services that authenticate
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

- **`nonce`: tolerant of absence.** gitlab.com's discovery document
  (`claims_supported`, checked 2026-08-30) names no `nonce` claim, so GitLab's
  ID tokens do not carry one. The gateway sends a nonce on every login and
  accepts a token without the claim, but refuses any token whose nonce does
  not match this browser's flow — replay protection survives to the extent
  the provider ever offered it. Self-managed GitLab versions may differ; if
  yours does emit a nonce, the strict check simply engages.
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

---

## Bundled Authelia (`IDP_BUNDLED=authelia`)

Enterprise installs that have no directory yet deploy Authelia alongside the
gateway as the deployment's own provider — same shape as the house IdP in the
team profile, but as a real OIDC server (OpenID Certified™, Apache-2.0) with
password login today and a path to TOTP/WebAuthn later. The installer mints
every secret locally and writes three generated files into `deploy/idp/` from
the spec in `deploy/idp/generate-authelia-config.sh`; there is nothing to
click and no IdP API call during install. The overlay is
`deploy/compose/docker-compose.idp-authelia.yml`, served on the origin that
is already published — no second port, no second name.

### Issuer shape: a subpath, not a subdomain

Authelia serves under the `/authelia` prefix on the public origin itself
(`server.address: tcp://:9091/authelia`), so the issuer is:

```bash
GATEWAY_OIDC__ISSUER=https://<PUBLIC_HOST>:<HTTPS_PORT>/authelia
```

Discovery lives under it
(`.../authelia/.well-known/openid-configuration`), and every endpoint the
document advertises — authorization, token, JWKS, userinfo — carries the
same prefix. Caddy forwards `/authelia/*` **without stripping** (`handle`,
not `handle_path`): Authelia serves the prefixed paths itself, and stripping
hands it paths it does not have — the portal's assets 404, and the failure
names the asset rather than the routing. This is the deliberate opposite of
the Keycloak `/idp` route beside it, which strips because Keycloak serves at
its own root.

The chat uses the issuer for discovery (`CHAT_OIDC_PROVIDER_URL` below), and
— like the house IdP — the token, JWKS and userinfo exchanges stay inside
the compose network whenever the issuer resolves internally. What the chat
must register is exact: `<PUBLIC_ORIGIN>/chat/login/callback`, never a
wildcard; Authelia compares redirect URIs as strings.

Verified live against `authelia/authelia:4.39.20` (discovery fetched over
real HTTP, subpath issuer, byte-for-byte `iss` match at the gateway):

```json
// .../authelia/.well-known/openid-configuration (abridged)
{
  "issuer": "<origin>/authelia",
  "authorization_endpoint": "<origin>/authelia/api/oidc/authorization",
  "token_endpoint": "<origin>/authelia/api/oidc/token",
  "jwks_uri": "<origin>/authelia/jwks.json",
  "userinfo_endpoint": "<origin>/authelia/api/oidc/userinfo"
}
```

### What the installer writes

| Setting | Value | Why |
|---|---|---|
| `GATEWAY_OIDC__ENABLED` | `true` | Turns the console's external door on. |
| `GATEWAY_OIDC__ISSUER` | `<PUBLIC_ORIGIN>/authelia` | The bundled issuer above. |
| `GATEWAY_OIDC__CLIENT_ID` / `CLIENT_SECRET` | `pystino-console` / minted | The console's client (below). |
| `GATEWAY_OIDC__REDIRECT_URI` | `<PUBLIC_ORIGIN>/auth/callback` | See the note below — the gateway derives the effective URI per provider. |
| `GATEWAY_OIDC__GROUPS_CLAIM` | `groups` | The chat client requests the `groups` scope, so the claim is in the tokens. |
| `GATEWAY_OIDC__ACCESS_TOKEN_AUDIENCE` | `pystino-api` | Deterministic: granted to both clients implicitly, so this is written with no prompting. |
| `CHAT_OIDC_PROVIDER_URL` | same issuer | The chat discovers from the same document. |
| `CHAT_OIDC_CLIENT_ID` / `CLIENT_SECRET` | `cerea` / minted | The chat's client (below). |
| `CHAT_OIDC_SCOPES` | `openid profile email groups` | `groups` is what puts group membership in the `/v1` access token. |

The redirect the gateway actually sends is
`<origin>/auth/callback/default` — the environment-seeded provider is named
`default` (ADR 0051), and the redirect URI is derived from the request
origin per provider row. Both URIs are registered on the console client, so
the bare default and the derived one each match exactly.

### The two clients, and how they differ

Both are confidential, PKCE-S256, authorization-code only, one-factor
(password) policy, remembered consent (one click on first login, then a
week of silence):

| | `pystino-console` (gateway) | `cerea` (chat) |
|---|---|---|
| Redirect URI | `<origin>/auth/callback/default` (+ bare `/auth/callback`) | `<origin>/chat/login/callback` |
| Token auth | `client_secret_post` — the gateway sends the secret in the POST body, never as Basic | `client_secret_basic` — openid-client's default |
| Scopes | `openid profile email` | `openid profile email groups` |
| Access token | JWT (`RS256`), `aud: [pystino-api]` | JWT (`RS256`), `aud: [pystino-api]` |

The `client_secret_post` row is the one to get wrong: Authelia's default is
Basic, and with Basic configured the gateway's exchange is rejected with a
message that names nothing. The `groups` scope on the chat client is the
other: `/v1` access tokens are validated locally with no userinfo round
trip, so whatever is not in the token is not seen — without that scope the
chat signs in fine and bills to no group.

Decoded from a real token minted by the scripted flow (not from config):

```json
// access token claims (abridged; aud is a list, iss byte-matches)
{
  "aud": ["pystino-api"],
  "iss": "<origin>/authelia",
  "sub": "8c8d23f1-… (opaque per JWKS key)",
  "client_id": "cerea",
  "groups": ["users"]
}
```

### Groups, and the first human

Group membership arrives in the `groups` claim (ID token, userinfo, and —
what `/v1` reads — the access token). The gateway auto-creates groups on
first sight and derives billing from effective memberships, exactly as with
any external provider.

The first human is a row in the generated `deploy/idp/users_database.yml`,
written by the installer from an address and password it asks for (or
generates): the password is stored as a SHA512-crypt hash (`$6$`, minted
with `openssl passwd -6` — no new host dependencies), the user starts in
the `users` group, and administration is granted afterwards in the console:
there is deliberately no admin-group setting (ADR 0069). The file backend
reloads without a restart (`watch: true`), so later users are appended in
the same shape. Rotating the JWKS signing key re-provisions everybody —
subjects are opaque per key and users are keyed on `(issuer, subject)` —
so the generator refuses to overwrite an existing directory.

### Behaviour worth knowing before you change anything

- **One consent click per client.** Consent is remembered per
  (user, client, scopes, audience) for a week: the chat's first login and
  the console's first login each ask once. Set `consent_mode: implicit` on
  the clients if even that is too much — both clients are first-party, so
  the screen is ceremony rather than a boundary.
- **Access tokens live one hour.** Group revocation reaches `/v1` within a
  token lifetime, not forever; the console session is unaffected.
- **ID tokens are for login, access tokens for `/v1`.** Only the chat
  client's access token carries the audience the gateway checks; the
  console's login tokens never leave the browser flow.
- **Password-only today.** The clients require one factor, so TOTP/WebAuthn
  devices change nothing until the policy is raised to `two_factor` — which
  needs users enrolled and (for TOTP reset/self-service) a real notifier;
  the bundled notifier writes to a file.

### Authelia caveats, verified at source or live

- **Subpath serving is Authelia's documented non-default** (Caddy guide,
  "Subpath": the server address must carry the path, and the proxy must not
  strip it) — verified live here, not just read. A bare `/authelia` without
  the trailing slash redirects to `/authelia/`, same as the chat route.
- **Opaque by default.** Authelia mints opaque access tokens unless the
  client sets `access_token_signed_response_alg` — without `RS256` there the
  gateway has no JWT to validate and `/v1` rejects every token with the
  same message a bad API key gets.
- **Audience is per-client and opt-in.** No `aud` claim lands in any token
  unless the client whitelists it *and* the grant mode is `implicit`
  (neither the gateway nor the chat ever sends an `audience` parameter, so
  `explicit` — the default — grants nothing).
- **The edge shape needs one manual block.** `Caddyfile.netbird` has no
  `conf.d` import, so the overlay's snippet only routes under the proxy
  overlay; behind the edge shape the equivalent `handle /authelia/*` block
  is appended to both sites at install time.
- **Not covered by this section's live test:** the full chat login in a
  browser (integration phase, once), and the production TLS trust dance
  (Caddy root into the gateway bundle + restart) — the arrangement is the
  Keycloak overlay's measured one, verbatim, but the handshake against a
  real certificate was not re-run here.

---

## Bundled Keycloak (`IDP_BUNDLED=keycloak`)

An enterprise install needs no external directory. With `IDP_BUNDLED=keycloak`
the installer deploys Keycloak **alongside** the gateway as the deployment's
own OIDC provider — same shape as the external-provider setup above, with the
provider half already done. The audience is pre-determined
(`GATEWAY_OIDC__ACCESS_TOKEN_AUDIENCE=pystino-api`), baked into the realm the
installer generates, so the installer writes it with no prompting.

### What the installer creates

Two generated files in `deploy/idp/` (minted locally, never committed),
from the spec in `deploy/idp/keycloak-realm.json.template`:

- `keycloak-realm.json` — the realm import: realm `pystino`, two confidential
  clients (`pystino-console`, `pystino-chat`), an audience mapper fixing
  `aud` to `pystino-api` on both, a groups mapper naming the flat claim
  `groups`, the `research` and `platform-admins` groups, and the first human
  (`owner@example.org`, password shown once at install).
- `10-idp-keycloak.caddy` — the `/idp` route, mounted into the proxy's
  `conf.d` by `docker-compose.idp-keycloak.yml`.

And in `deploy/.env`, from the same minted values:

```bash
IDP_BUNDLED=keycloak
GATEWAY_OIDC__ENABLED=true
GATEWAY_OIDC__ISSUER=<PUBLIC_ORIGIN>/idp/realms/pystino   # exactly the discovery document's iss
GATEWAY_OIDC__CLIENT_ID=pystino-console
GATEWAY_OIDC__CLIENT_SECRET=<minted gateway client secret>
GATEWAY_OIDC__GROUPS_CLAIM=groups
GATEWAY_OIDC__ACCESS_TOKEN_AUDIENCE=pystino-api
KEYCLOAK_ADMIN=admin
KEYCLOAK_ADMIN_PASSWORD=<minted master-admin password>
```

`KEYCLOAK_ADMIN` / `KEYCLOAK_ADMIN_PASSWORD` bootstrap the master-realm admin
(the overlay maps them onto Keycloak 26's `KC_BOOTSTRAP_ADMIN_*` names).
The first human is `owner@example.org` — or that admin, in Admin Console at
`<PUBLIC_ORIGIN>/idp/admin/`. There is deliberately no admin-group setting
(ADR 0069): make administrators in the console or with
`gateway passwd --admin`, as with any provider.

The chat needs nothing extra: it signs in against the same issuer with its
own client (`pystino-chat`, redirect `<PUBLIC_ORIGIN>/chat/login/callback`
exactly), and its per-user `/v1` calls already carry the audience above.

### The two facts this relies on, verified against the live server

**Access tokens carry no `aud` unless a mapper puts one there** — only `azp`,
naming the client that asked. The realm's `pystino-audience` mapper
(`oidc-audience-mapper`, `included.custom.audience=pystino-api`,
`access.token.claim=true`, `id.token.claim=false`) is what makes `/v1`
accept the token, and the gateway matches `aud` deliberately rather than
`azp`: `azp` says who *requested* the token, not who it is *for*. A decoded
chat-client token reads:

```json
{
  "iss": "https://<host>:<port>/idp/realms/pystino",
  "aud": "pystino-api",
  "azp": "pystino-chat",
  "typ": "Bearer",
  "preferred_username": "owner@example.org",
  "groups": ["research"]
}
```

**Group membership arrives in a flat `groups` claim** (`full.path=false`, so
`research` rather than `/research`), in the access token, the ID token and
userinfo alike — which is why `GATEWAY_OIDC__GROUPS_CLAIM=groups` works for
both console login and `/v1` tokens. The default `realm_access.roles` stays
where Keycloak put it; the gateway is pointed at the claim instead.

### First boot, and what later edits cost

Keycloak starts with `--import-realm`, which consumes the generated JSON
**at first boot only** (measured ~80 s to discovery on a fresh volume, ~20 s
on later boots; the server itself starts in ~10–15 s). Regenerating the JSON
later changes nothing on an existing `keycloak-data` volume: post-install
edits — users, groups, client secrets — go through Admin Console, and a
re-import means wiping the volume, which deletes every user created since.
Treat the template as install-time truth, the console as maintenance truth.

Restart the gateway after install — OIDC discovery is read once at startup,
and the trust bundle (Caddy's local authority appended to
`deploy/tls/caddy-root.crt`) only exists after the proxy's first run.

### Footprint and shape

One container, no second database: Keycloak runs on its default dev-file H2
on the `keycloak-data` volume (fresh realm well under a megabyte), not on a
shared Postgres — no role for the installer to create, no coupling to the
gateway's database. Measured on the live host: image 756 MB on disk,
~730 MB RSS at idle. The price of the bundle is RAM and a slow first boot,
not topology.

### Behind the edge shape

`KC_HOSTNAME` is the public origin either way, so Keycloak needs nothing
different — but `Caddyfile.netbird` has no `conf.d` import, so the mounted
snippet only routes under the proxy overlay. Behind the edge shape the same
`handle_path /idp/*` block (header `X-Forwarded-Prefix /idp`, upstream
`keycloak:8080`) is appended to both sites in `Caddyfile.netbird` at install
time.

### Realm import constraints, verified live

- **The import runs at first boot only** and **refuses unknown JSON fields** —
  the realm file may carry no `_comment` key or any other key outside
  `RealmRepresentation` (the failure reads `Unrecognized field "_comment"`,
  and nothing starts). The template is therefore bare JSON; this section is
  its documentation.
- **An explicit top-level `clientScopes` key replaces the auto-created
  built-in scopes.** A realm imported with one must define everything the
  clients request — including `basic`, whose `oidc-sub-mapper` is what puts
  `sub` in the access token (a realm without it signs tokens that fail
  validation with `Missing claim: 'sub'`). The template defines `basic`,
  `profile`, `email` and `groups` explicitly, with the mapper shapes copied
  from the live server rather than from memory.
- The `groups` **scope** exists so the chat's requested scope string
  (`openid profile email groups`) validates; the same group-membership mapper
  also rides each client directly, which is how the console's tokens carry
  `groups` without requesting the scope.
