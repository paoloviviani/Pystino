# 0011 — OIDC for the management API, and the configurable group claim

- Status: accepted
- Date: 2026-08-14

## Context

Users log in with OIDC, may hold several groups, and the claim carrying those groups
differs by identity provider. Group membership drives billing and model access, so
getting it wrong grants or denies the wrong access.

## Decisions

### OIDC authenticates humans; API keys authenticate programs

`/v1` traffic **always** uses API keys, never OIDC tokens. Two reasons: a key is
revocable server-side and carries a billing group, which a bearer ID token does not;
and no chat client knows how to refresh an OIDC token.

`/api` and `/auth` are the human surface, authenticated by a session cookie.

### Libraries

- **`joserfc`** for JOSE/JWT. `authlib.jose` is deprecated in favour of it and will be
  removed in Authlib 1.8; `python-jose` is not a candidate. Resolved to 1.7.4.
- No full OIDC client library. Discovery, PKCE, code exchange and ID-token validation
  are about 200 lines against `httpx` + `joserfc`, and one developer maintaining this
  benefits more from code they can read than from a dependency.

### Security specifics

- **Only asymmetric algorithms accepted for ID tokens** (RS*/ES*/PS256). Permitting
  HS256 would let anyone who learns the (non-secret) `client_id` forge tokens.
- `iss`, `aud`, `exp` and `sub` validated via `JWTClaimsRegistry`, with configurable
  clock skew.
- **`nonce` checked**, binding the token to the browser session that started the flow.
- **PKCE (S256) used even though this is a confidential client.** It costs nothing and
  removes the authorization-code interception class entirely.
- JWKS cached, and **refetched once on a signature failure** — that is how key
  rotation is meant to be handled, because providers rotate without warning. A failed
  refresh falls back to cached keys rather than failing every login on a blip.
- Flow state (`state`, `nonce`, PKCE verifier) travels in a short-lived signed cookie,
  not server-side storage, so login works across several workers with no shared session
  store.
- Session cookies are signed JWTs with `typ: "gw-session"`; the login cookie uses
  `typ: "gw-login"`. **Type confusion between the two is tested against.**

### The configurable group claim

`GATEWAY_OIDC__GROUPS_CLAIM` accepts:

| Provider | Value |
|---|---|
| Keycloak | `realm_access.roles` |
| Entra ID | `groups` |
| Namespaced | `https://example\.org/groups` |

Dotted paths walk nested objects. A literal dot is escaped `\.` — and the **full path
is tried as a flat key first**, because namespaced claim names contain dots that are
not nesting.

A bare string claim is treated as **one** group, not split on whitespace or commas.
Splitting is guesswork, group names containing spaces are common in directory systems,
and inventing two groups out of one would silently grant or deny the wrong access.

`fetch_userinfo` exists because several providers omit groups from the ID token to keep
it small. `group_allowlist` restricts which groups are imported; `auto_create_groups`
can be turned off to make membership purely administrative.

### Provisioning

Users are auto-provisioned on first login. Identity is **(issuer, subject)** — email is
not identity, since it is mutable and can be reassigned between people.

**Group membership is replaced, not merged.** The identity provider is authoritative, so
a group removed there must disappear here, or revoking access in the directory would not
revoke the ability to bill. This deliberately also removes manually-added memberships;
mixing authoritative and local membership silently produces access nobody intended.

Default billing group handling, in this order:

1. If the current default is no longer a group the user holds, clear it.
2. If there is then no default and the user has exactly one group, adopt it.

The order matters and was found by a test: doing it the other way round leaves a
single-group user with no default at all, and therefore unable to make a request until
they call the management API.

## Verified against a real Keycloak (2026-08-14)

`deploy/compose/docker-compose.keycloak.yml` adds Keycloak **26.7** with a seeded realm
(`deploy/keycloak/realm-llm-platform.json`), and `scripts/test_oidc_flow.py` drives the
entire authorization-code flow: `/auth/login` → Keycloak login form → credential POST →
callback → session cookie → `/api/me`. All of it passes, twice in a row.

What that actually confirms, beyond "login works":

- PKCE S256 is offered and accepted; discovery advertises
  `device_authorization_endpoint`, which the Phase 4 `opencode` bootstrap needs.
- ID token validation succeeds against a real RS256 JWKS, with `iss`, `aud`, `exp` and
  `nonce` all checked.
- The group claim maps through: users land in the same groups the realm defines.
- The provisioning branches behave as designed — and each seeded user exists to pin one:
  **alice** (one group) has it adopted as her default billing group; **bob** (two) gets
  no guess; **carol** (none) can sign in but cannot mint a key, and is told why.
- The loop closes: a session mints an API key, and that key then serves a completion
  billed to her OIDC group — €0.45 against `research`, `usage_source=upstream_exact`.
- Identity really is `(issuer, subject)`: users are stored against
  `http://keycloak:8080/realms/llm-platform` and Keycloak's UUID, not their email.

**Two things this found.**

*The two-hostname problem.* An ID token's `iss` is compared byte-for-byte with the
discovery document's issuer, and in Docker there are two names for the same Keycloak —
`keycloak:8080` inside the network, `localhost:8080` from a browser. Left to infer the
issuer per request, Keycloak mints tokens whose `iss` does not match what the gateway
discovered and every login fails validation. `KC_HOSTNAME` is therefore pinned. This is
not a quirk of the test rig: any deployment where the gateway and the browser reach the
IdP by different names hits it.

*Re-login must not reset an explicit choice.* The first version of the test used one
user both to assert "no default is guessed" and to change that default, and it passed
once and failed on re-run — because the gateway had correctly **preserved** the group
the user chose. The behaviour was right and the test was wrong. It is now asserted
deliberately: an explicit default survives re-authentication, and only an invalid one
is cleared.

## Consequences

- The redirect flow is now covered end to end, but **only against Keycloak**. Entra ID,
  Google and others differ in exactly the places this ADR makes configurable — claim
  location, whether groups appear in the ID token at all, group naming. Re-run
  `scripts/test_oidc_flow.py`'s logic against the real provider before going live.
- A user with several groups gets no default and must choose one before their first
  request. That is deliberate: guessing which group to charge is worse than asking.
- **A bug this surfaced:** membership reconciliation originally read
  `user.memberships`, which lazy-loads and raises `MissingGreenlet` in async code — on
  the first login of every new user. It now queries explicitly. Touching an unloaded
  relationship from async code is the trap to watch for in this codebase.
