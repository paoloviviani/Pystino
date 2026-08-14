# Keycloak (development identity provider)

A seeded realm so the OIDC path can be brought up and exercised in one command,
rather than being the one part of the gateway nobody can test.

```bash
docker compose --env-file deploy/.env \
  -f deploy/compose/docker-compose.yml \
  -f deploy/compose/docker-compose.smoke.yml \
  -f deploy/compose/docker-compose.keycloak.yml up -d --build

./scripts/test_oidc_flow.py
```

Admin console: <http://localhost:8080> — `admin` / `admin`.

**Development only.** Keycloak runs `start-dev` (in-memory H2, no TLS) and the realm
below contains plaintext credentials on purpose. A production deployment needs its
own database, TLS and real secrets.

`realm-llm-platform.json` deliberately contains **no comments**: Keycloak's realm
importer rejects unrecognised fields outright, so a `_comment` key anywhere in the
file makes the container exit 1 with `Unrecognized field "_comment"`. Hence this
README.

## The seeded users are chosen to exercise provisioning, not just login

Each one pins a different branch of `gateway/oidc.py:provision_user`:

| User | Password | Groups | What it proves |
|---|---|---|---|
| `alice` | `alice-password` | `research` | Exactly one group, so the gateway adopts it as her default billing group and she can spend immediately |
| `bob` | `bob-password` | `research`, `finance` | Two groups, so no default is guessed — he must choose one before spending |
| `carol` | `carol-password` | none | Can sign in, but cannot bill anything; minting a key fails with a readable reason |
| `erin` | `erin-password` | `research`, `finance` | The *mutation* target: changes her own default billing group, and it survives her next login |
| `dave` | `dave-password` | `platform-admins` | A separate group for the admin endpoints |

`bob` and `erin` are deliberately near-identical. `bob` is never mutated, so "no default was
guessed" holds on a re-run; `erin` is the one the test changes. An earlier version used one
user for both and failed the second time — because the gateway had correctly *preserved* the
default that user had explicitly chosen.

`research` is also the group name `gateway seed` creates, so an OIDC user lands in the
same group as the seeded model and price and can make a real billed request.

## The two-hostname problem, and how it is handled

An ID token's `iss` is compared byte-for-byte against the issuer in the discovery
document, and in Docker there are two names for the same Keycloak: `keycloak:8080`
from inside the compose network, `localhost:8080` from your browser. Left to infer
the issuer per request, Keycloak mints a token whose `iss` does not match what the
gateway discovered, and every login fails validation.

So `KC_HOSTNAME` is pinned to `http://keycloak:8080`, making the issuer stable, with
`KC_HOSTNAME_STRICT=false` so the admin console still answers on `localhost`.
`scripts/test_oidc_flow.py` rewrites `keycloak:8080` to `localhost:8080` for its own
requests — safe, because an authorization code is bound to the client and redirect
URI, not to the hostname the browser used.

If you point a real browser at this, the redirect to Keycloak will send you to
`http://keycloak:8080/...`, which your machine cannot resolve. Either add
`127.0.0.1 keycloak` to `/etc/hosts`, or use the test script.

## Group claim

The realm publishes group membership twice, so both shapes of
`GATEWAY_OIDC__GROUPS_CLAIM` can be tried against a real provider:

- `groups` — a flat array. This is what the overlay configures.
- `realm_groups.names` — nested, for exercising dotted-path resolution.

Keycloak's own `realm_access.roles` is a third shape worth trying if you want to key
authorisation off roles rather than groups.

The device authorization grant is enabled on the client because the Phase 4
`opencode` bootstrap needs it; `test_oidc_flow.py` asserts that discovery advertises
`device_authorization_endpoint`.
