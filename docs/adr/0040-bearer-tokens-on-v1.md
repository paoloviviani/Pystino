# 0040 — OIDC access tokens as a `/v1` credential, gated by audience

- Date: 2026-08-28
- Status: **accepted, built**
- Requested as: the first milestone of the Phase 3 chat application — "go with
  M0 and lay down the foundation of M1".
- Related: [0022](0022-administration-surface.md) (the API-key invariants this
  does not change), the Phase 3 plan on the `chat` branch (why the chat app is a
  separate service, which is what makes this necessary).

## Context

The chat application calls `/v1` on behalf of the person using it. Until now
`/v1` accepted exactly one credential — an API key — so a separate service had
two options: hold one shared key and lose per-user attribution entirely, or mint
and store a key per user, which is a credential-management system invented to
work around a missing token path.

The `opencode` device flow needs the same thing and has needed it since Phase 1:
a device flow ends in an OIDC access token, and the CLI then has to call `/v1`
with it. One gap, two consumers.

## Decision

### 1. Naming an audience is the switch

`GATEWAY_OIDC__ACCESS_TOKEN_AUDIENCE` — empty by default, which is exactly the
behaviour every existing deployment has. Set it, and access tokens naming that
audience are accepted on `/v1` alongside API keys.

One knob, not two. An `enabled` flag without an audience would accept any token
the realm ever issued to anybody; the audience *is* the security property, so it
is also the switch, and a deployment cannot turn the feature on without deciding
who it is for.

### 2. The audience must be in `aud`, and `azp` is not a substitute

**Measured against this deployment's Keycloak on 2026-08-28, not taken from the
specification:** a Keycloak access token carries **no `aud` claim at all** unless
an audience mapper puts one there. It carries `azp` naming the client that asked
for the token, and nothing else about who the token is for.

So a validator written the obvious way — copy `validate_id_token`, which requires
`aud == client_id` — rejects every token the realm issues, and a validator
written to accept `azp` instead accepts too much: `azp` says who *requested* the
token, so any token that client ever obtained for any purpose would open the API.

Every client permitted to call `/v1` therefore carries an `oidc-audience-mapper`
naming `llm-gateway`, on the access token only. Permission to reach the API is an
explicit per-client grant in the identity provider, which is where that decision
belongs.

**A client scope was the first design and it broke the realm.** A `gateway-api`
client scope holding the mapper is the idiomatic Keycloak arrangement and reads
better — permission as a grant rather than a copied mapper. But declaring
`clientScopes` in a realm import file **replaces the built-in set entirely**:
after the import the realm held `gateway-api` and `offline_access` and nothing
else, `profile`, `email`, `roles`, `web-origins`, `acr` and `basic` were gone,
and every login failed with `invalid_scope` — console logins included, from a
change that was about `/v1`. Found by running the live script; no unit test could
see it, because the realm file is not something the unit suite loads.

### 3. An ID token is not an API credential

Keycloak marks an ID token `typ: ID` and an access token `typ: Bearer`, and the
validator refuses the former. The claim is not standard, so its absence proves
nothing and is allowed; its presence saying `ID` is refused.

The hole this closes is narrow and easy to miss: an ID token names the client as
its audience, so a deployment that set `access_token_audience` to its own
`client_id` would accept the very token it hands to the browser at login.

### 4. Memberships reconcile when the token disagrees, not on a timer

A bearer request resolves `(issuer, subject)` to the same user row browser login
keys on — one person, one spend total, one set of quotas — provisioning on first
sight so somebody who has never opened the console can still use the API.

Group memberships are then reconciled **only when the token's claims differ from
the stored row**. Reconciling on every request puts writes on the hot path of
every chat message. Reconciling never means that removing someone from a group
in the directory stops them signing into the console while leaving them able to
bill that group through the API — the half that costs money. Comparing costs
nothing, since the claims are parsed and the memberships loaded either way, and
the staleness window is one token lifetime.

Measured: a bearer `/v1/models` costs 3 selects and 0 writes, the same as an API
key, and `test_query_counts.py` pins it.

### 5. No userinfo request, ever

The login flow may call the userinfo endpoint for claims the ID token omits.
The bearer path may not: it runs on every request, and an HTTP round trip to the
identity provider per API call is not a thing a gateway may do. Whatever the
token does not carry, the token does not carry.

## Consequences

- `Principal.api_key` is `None` for a bearer caller, and `usage_records.api_key_id`
  was already nullable — the ledger attributes to the user and the group, which
  is what quotas, access control, redaction scoping and reporting all read. No
  migration was needed.
- Every rejection returns the message a bad API key gets, and the reason is
  logged rather than returned. The reason a token failed is a map of the
  validator to anyone holding a forged one. A deployment that does not accept
  tokens does not even confirm it has an identity provider.
- API keys remain the credential for programs acting as themselves: revocable
  server-side, carrying a billing group, and not expiring in five minutes.
- A bearer caller cannot pin a billing group, because pinning lives on the key.
  The user's default applies, with the same membership re-check.
- `scripts/test_bearer_tokens_live.py` checks the half the unit suite cannot: that
  the tokens a real realm issues are the tokens the validator accepts. It asserts
  the `aud` claim is there *before* anything else, because every later check
  would fail identically and misleadingly without it.
- Found writing the tests: a first draft asserted an expired token was refused
  using `exp = now - 60` against a 60-second skew tolerance, so it passed on the
  boundary while proving nothing. Leeway makes any expiry test written at the
  tolerance vacuous.
