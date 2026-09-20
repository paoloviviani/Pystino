# The house IdP: the gateway as the issuer for its own clients

ADR 0068. The gateway can act as a minimal identity provider for
browser-facing clients — the chat first — so a deployment needs no bundled
Keycloak (ADR 0044) to have a complete sign-in story. What it issues and what
it deliberately does not:

| It issues | It does not |
|---|---|
| discovery, authorization-code + PKCE, `id_token` (ES256), JWKS, userinfo, `end_session` | JWT access tokens — `/v1` keeps opaque key rows, introspected as always |
| credentials for **registered first-party clients** | anything for unregistered clients — no dynamic registration, no consent screen |

## Enabling it

```bash
# deploy/.env
GATEWAY_IDP__ENABLED=true
GATEWAY_IDP__ISSUER=https://cerea.pviviani.eu      # the public origin, exactly as clients see it
GATEWAY_IDP__SIGNING_KEY=<PEM ES256 private key>   # openssl ecparam -genkey -name prime256v1
GATEWAY_IDP__INTERNAL_TOKEN=<long random value>    # guards the legacy /auth/token + /auth/revoke
GATEWAY_IDP__CLIENTS='[{"client_id":"cerea","redirect_path":"/chat/login/callback","secret":"..."}]'
```

A missing or unreadable piece refuses the startup, with the variable named.
`GATEWAY_IDP__INTERNAL_BASE_URL` (default: the issuer) points discovery's
server-to-server endpoints — token, JWKS, userinfo — at the compose-internal
address, so the chat's exchanges never leave the host:

```bash
GATEWAY_IDP__INTERNAL_BASE_URL=http://gateway:8000
```

Off means absent: the routes are not registered and discovery does not
resolve. The management doors — console login, API keys, external OIDC — are
not part of the IdP and do not answer to this switch.

## Where each endpoint lives

| Reachability | Endpoints | Guard |
|---|---|---|
| Browser, at the origin | `/oauth/authorize`, `/oauth/end_session`, `/.well-known/openid-configuration` | PKCE, one-time codes, state, the registered-client check, the existing throttle |
| Compose network only | `/oauth/token` | client authentication + PKCE; discovery advertises the internal URL, so a standards client follows it inside the network |
| Compose network only | `POST /auth/token`, `POST /auth/revoke` (the ADR 0046 family) | the `x-mint-token` header, matching `GATEWAY_IDP__INTERNAL_TOKEN` |
| Public material | `/oauth/jwks.json` | a JWKS is what lets the world verify, not what lets it in |

## The issuer is `local` by another name

Users are keyed on `(issuer, subject)`. An `id_token` from the house issuer
carries `iss` equal to the deployment's public origin, but the row it names is
the ordinary `issuer="local"` account: `sub` is the casefolded email, which is
what a local account's subject already is. One row per person across both
doors — no adoption decision, no ADR 0056
machinery for our own issuer. An external issuer cannot claim the alias: a
token bearing it must verify against this issuer's own JWKS to be believed at
all.

## First administrator

The house IdP keeps no users of its own: the account it mints codes for is
the ordinary `issuer="local"` row, created out-of-band with `gateway passwd`
(it prompts twice — the password never lands in shell history or any file —
and `--admin` defaults to yes). The chat rides the console session, so
signing into the console is what signs the chat in; there is no separate
chat account to create. A different address later is the same command again:
resets never touch the admin flag unless it is passed.

## Roles stay out of it

The house issuer follows ADR 0069
like every other provider: `first_login` — one provisioning answer, then the
console owns groups and the admin flag. No group claim, from any issuer, ever
confers a role here.

## The chat's side

The chat is an OIDC client like any other: `OPENID_PROVIDER_URL` is the
gateway's issuer, `OPENID_CLIENT_ID` / `OPENID_CLIENT_SECRET` match a client
in `GATEWAY_IDP__CLIENTS`, and the callback is the client's `redirect_path`
plus the public origin. `deploy/compose/docker-compose.chat.yml` wires this
end to end.
