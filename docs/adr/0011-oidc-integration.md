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

## Consequences

- **The full browser redirect flow is not covered by tests.** It needs a real identity
  provider. Everything on our side of the redirect — claim resolution, group
  normalisation, provisioning, membership reconciliation, session tokens — is tested.
  Verify end-to-end against the real IdP before relying on it.
- A user with several groups gets no default and must choose one before their first
  request. That is deliberate: guessing which group to charge is worse than asking.
- **A bug this surfaced:** membership reconciliation originally read
  `user.memberships`, which lazy-loads and raises `MissingGreenlet` in async code — on
  the first login of every new user. It now queries explicitly. Touching an unloaded
  relationship from async code is the trap to watch for in this codebase.
