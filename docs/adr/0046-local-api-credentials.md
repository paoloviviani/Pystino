# 0046 — The local door issues `/v1` credentials: the gateway as issuer for its own accounts

- Date: 2026-08-31
- Status: **accepted, built** (gateway and chat-api unit-tested; the SPA renders
  its door from `/api/auth/methods`, showing the password form, the OIDC
  redirect, or both)
- Related: [0043](0043-local-authentication.md) (local sign-in, whose credential
  this extends), [0040](0040-bearer-tokens-on-v1.md) (the `/v1` token path this
  rides), [0044](0044-keycloak-removed.md) (why there may be no issuer at all),
  [0010](0010-api-keys.md) (the key machinery this reuses),
  [phase-3-plan.md](../phase-3-plan.md) (the chat as a `/v1` client).
- All code references verified against the working tree on 2026-08-31.

## Context

[ADR 0044](0044-keycloak-removed.md) removed the bundled identity provider:
local email + password ([0043](0043-local-authentication.md)) is the default way
in, and OIDC is configured only when the operator runs a directory. [ADR
0040](0040-bearer-tokens-on-v1.md) made `/v1` accept bearer tokens — but tokens
*from that external issuer*, gated by `GATEWAY_OIDC__ACCESS_TOKEN_AUDIENCE`
(`deps.py:247`). The chat backend calls `/v1` as the person typing, per the
phase-3 plan, and authenticates those people with its own OIDC client against
the same realm.

Put the three together and a local-only deployment — the shape 0044 declares
*complete, not crippled* — has a hole where the chat should be:

- The chat SPA has no way to sign in: chat-api is OIDC-only
  (`chat_api/routers/auth.py` redirects to the IdP unconditionally).
- Even with a session, chat-api holds no token `/v1` accepts: there is no
  issuer, no JWKS, no audience. `/v1` bearer auth is off by its own switch.
- The `opencode` device flow has the same shape of gap, though its API-key door
  still works.

The user-visible symptom is the request that opened this: *the chat UI should be
able to authenticate against the local gateway login.* It cannot, structurally —
this is an ADR, not a login form.

What [0043](0043-local-authentication.md) deliberately did not build: its local
login mints exactly the management cookie the OIDC callback mints, and nothing
else. That was right for the console-only scope it had. It is insufficient now,
because the chat's session must end in a `/v1` credential, and a management
cookie is neither scoped for it nor carryable across a service boundary
([0035](0035-public-tls-exposure.md)'s path-scoping discipline).

## Decision

**The gateway becomes the issuer of `/v1` credentials for its own local
accounts — not a full identity provider.** Three pieces, each the smallest
shape that closes the hole:

1. `POST /auth/login` (0043's local login) gains an opt-in request field
   naming the client (e.g. `"client": "chat"`). When present, the response
   carries a **refresh credential** alongside the existing JSON, plus the
   identity payload a client session needs (email, display name, group names,
   admin flag). The console ignores the extra field; the throttle and the
   Argon2 verification are exactly 0043's, unchanged — a chat login *is* a
   local login, with the same failure answers and the same cost per attempt.
2. `POST /auth/token` exchanges a refresh credential for a short-lived
   **access credential**, and `POST /auth/revoke` deletes the credential
   family. Logout that only deleted chat-api's session row would leave a
   working `/v1` credential behind — revocation is what makes logout real,
   the same reasoning 0043 applied to cookies.
3. chat-api grows one local door: `POST /chat/api/auth/local` forwards
   email + password to the gateway and fills **the same `Session` row** the
   OIDC callback fills — `issuer="local"`, `subject` = casefolded email
   (the gateway casefolds; the two must agree to the character), the refresh
   credential encrypted into the same column the IdP's refresh token occupies.
   From there nothing downstream changes: per-turn access tokens come from
   either the IdP's token endpoint or the gateway's token endpoint behind one
   internal seam, and the relay to `/v1` is untouched.

The chat SPA learns which doors exist from chat-api (which asks the gateway's
unauthenticated `/auth/methods`, the same call the console's login page makes)
and shows the password form, the OIDC redirect, or both — 0043's console
arrangement, replayed one layer out.

### Opaque credentials, not JWTs

The decisive fact is in `deps.py:222`: a presented credential is routed by
*shape* — `looks_like_jwt` goes to the OIDC validator, everything else goes to
`resolve_api_key` (prefix lookup → SHA-256 compare → `is_usable()` →
`user.is_active` → membership re-check). An **opaque credential is therefore
already a fully validated, fully attributed `/v1` credential with zero new
validation code**: revocation, disabled users, group changes and the query
budget (`test_query_counts.py` pins it) all come free, because access
credentials *are* API-key rows.

A gateway-minted JWT was the first design and is rejected: it adds JOSE
signing, parsing and claim rules on the hot path, whose only validator would be
the gateway itself, to avoid a database lookup the API-key path already pays
happily. And the audience gate of [0040](0040-bearer-tokens-on-v1.md) becomes
inapplicable rather than bypassed — a key row cannot name an audience, and
needs none: it is a `/v1` credential by construction, valid on nothing else.

### Two credentials, one exchange

- **Refresh credential**: high-entropy random, `<prefix>_<handle>_<secret>`
  shaped like every other key (prefix-indexed lookup, `security.py`),
  SHA-256 at rest — a key has 2^256 of entropy, so Argon2 would tax the
  exchange to defend nothing ([0043](0043-local-authentication.md)'s argument,
  reused). Stored in a **separate `refresh_credentials` table** — 0043's
  separate-table rationale applies verbatim: one row per (user, client),
  `expires_at` set to the client's session lifetime, revocation is a row
  deletion.
- **Access credential**: an API-key row carrying a marker naming its minting
  client. TTL short (~15 min). Minting is **idempotent while unexpired**: an
  exchange for (user, client) that finds a live access credential returns it
  rather than minting another. The failure this prevents is a table growing
  one row per chat turn — chat-api exchanges per request, and a cleanup job
  nobody wrote is not a design.
- **Invariants on machine-minted keys**, enforced in the routes, not by
  convention: never listed in the console's key management, never manually
  creatable, never carrying a pinned billing group — a bearer caller cannot
  pin either (`resolve_billing_group`), and the user's default applies with
  the same membership re-check. Revoking a local user (disable) refuses
  every path; `is_usable()` and `user.is_active` already sit on it.
- **The browser never holds either credential.** The SPA's rule stands:
  no token in JavaScript, nothing in localStorage (`apps/web/src/lib/api.ts`).
  An alternative considered — the browser obtaining the refresh credential
  from the gateway directly and handing it to chat-api — was rejected on
  exactly this rule: it puts a long-lived credential where the architecture
  keeps none.

### What was rejected

- **The gateway as a minimal OIDC provider** (discovery + authorization-code
  flow so chat-api's existing client works unchanged). Attractive on paper;
  in practice a *real* IdP is a large permanent security surface — redirect
  endpoints, consent, key management, spec compliance — and the password
  grant it would rest on is deprecated in OAuth 2.1. If a genuine requirement
  for the gateway to be an IdP ever appears, this ADR's credential machinery
  is the substrate to promote, and only the browser-facing flow changes.
- **Sharing the management cookie** across gateway and chat-api. It breaks the
  service boundary the phase-3 plan holds, and it collides with the
  path-scoping discipline 0035 paid for.
- **chat-api mints per-user API keys** — the phase-3 plan's rejection stands:
  a credential-management system invented to route around a missing token path.
- **chat-api stores passwords or password-derived material.** It never sees
  anything but the forwarded POST body; the gateway's throttle and Argon2id
  remain the only verifiers. The cost named rather than argued away: the
  password transits one more service, which is acceptable only because every
  deployment shape is loopback or TLS (ground rule 4) — and it is the reason
  the direct-to-IdP redirect flow stays for OIDC deployments rather than being
  collapsed into this one.

## Consequences

- One new table (`refresh_credentials`), one marker column on `ApiKey`, three
  route changes (login field, token, revoke), one alembic migration. chat-api:
  one route, one internal seam for token acquisition, no schema change. The
  console is untouched.
- A local login that names no client mints nothing new — existing console
  behaviour is bit-identical.
- `POST /chat/api/auth/local` is a JSON endpoint, so cross-site form posts
  cannot reach it; the session cookie chat-api then issues is the same one the
  OIDC callback issues, scoped to `/chat` as ever.
- Tests, per ground rule 3, named now: exchange mints / reuses while live /
  refuses expired; revoking the refresh credential kills its access rows and
  both are refused afterwards; a disabled local user is refused at exchange
  *and* on `/v1`; a `/v1` request with a local access credential lands on the
  `(issuer="local", subject)` row's default billing group; failed logins
  through chat-api's door still count against the gateway's throttle; two
  exchanges inside the access TTL produce one row.
- Live coverage returns through the local door: 0044 deleted
  `test_chat_live.py` with the IdP it drove; its replacement signs in locally,
  streams a turn, and finds the ledger row it produced — exercising this ADR
  and 0043 on every run.
- `opencode` on a local-only deployment keeps its API-key door; the device
  flow still needs a real IdP, unchanged by this ADR.
- Deferred as hardening, named so it is not rediscovered as a bug: refresh
  credential rotation on exchange. Expiry and revocation cover the v1 threat
  model; rotation is cheap to add behind the same endpoint contract later.
