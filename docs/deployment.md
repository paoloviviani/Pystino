# Deployment

## The compose overlay model

Everything ships as docker compose overlays on one base file
(`deploy/compose/docker-compose.yml`), each overlay adding one deliberate
capability:

| Overlay | Adds |
|---|---|
| *(base)* | PostgreSQL, Valkey, migrations, the gateway — published on **127.0.0.1 only** |
| `docker-compose.override.yml` | development conveniences: mounted sources with `--reload`, published loopback ports for PostgreSQL and Valkey. Loaded automatically when it sits beside the base file |
| `docker-compose.smoke.yml` | a fake OpenAI-compatible upstream — exercise the whole topology with no provider account |
| `docker-compose.redaction.yml` | the Presidio detection service, plus the local extractor as a second deployment of the same image |
| `docker-compose.chat.yml` | the chat application (Cerea, a sibling checkout) at `/chat`, with its own MongoDB |
| `docker-compose.edge.yml` | the NetBird edge shape: the origin router in plain HTTP on loopback, TLS terminated upstream at the edge — for a box that never sees a certificate |
| `docker-compose.proxy.yml` | Caddy terminating TLS — the only route to a routable address with a locally held certificate (ADR 0035) |
| `docker-compose.keycloak.yml` | a Keycloak to develop against — a development dependency, not a bundled identity provider (ADR 0044 stays removed) |

```bash
# the common development shape:
docker compose --env-file deploy/.env \
  -f deploy/compose/docker-compose.yml \
  -f deploy/compose/docker-compose.smoke.yml \
  -f deploy/compose/docker-compose.redaction.yml up -d --build
```

The overlays are separate files rather than profiles on purpose: a fake
upstream in the base file would be one careless `-f` away from production.

## Configuration

`deploy/.env.example` is the annotated source of truth — copy it to
`deploy/.env` (gitignored) and edit. The variables that carry weight:

| Variable | Why it exists |
|---|---|
| `POSTGRES_PASSWORD` | the ledger of record |
| `GATEWAY_SESSION_SECRET` | signs management session cookies |
| `GATEWAY_SECRET_KEY` | encrypts provider API keys at rest. Comma-separated to rotate (first encrypts, any decrypt). Back it up with the database — losing every value means re-entering each provider credential by hand |
| `GATEWAY_SESSION_COOKIE_SECURE` | set `true` behind TLS; left false for local http, the cookie would be dropped |
| `GATEWAY_BILLING_CURRENCY` | all prices and quotas share it; a model priced otherwise is **refused rather than converted** — a silent exchange rate produces invoices that look right and are wrong |
| `GATEWAY_UPSTREAM__BASE_URL` / `GATEWAY_UPSTREAM__API_KEY` | the default upstream provider, read once by migration 0003 to seed the `default` provider |
| `GATEWAY_REDACTION__ENGINE`, `GATEWAY_REDACTION__PLACEHOLDER_KEY` | see [Redaction](redaction.md) — the key must be backed up with the transcripts it labelled |
| `GATEWAY_ACCOUNTING__ENABLED`, `GATEWAY_QUOTA__ENABLED` | the ledger and its ceilings — see [Unmetered deployments](#unmetered-deployments-no-ledger-no-quotas) below. Quotas without accounting are refused at startup |
| `GATEWAY_OIDC__*` | any OIDC provider. Discovery is read **once at startup** — changing any value needs a gateway restart. `iss` is part of user identity, so changing the issuer re-provisions every user as a new row with no memberships. See [OIDC against any provider](oidc-generic-provider.md) |
| `GATEWAY_LOCAL_AUTH__ENABLED` | local email + password sign-in beside OIDC (ADR 0043); passwords are set out-of-band with `gateway passwd` and never through the environment |
| `GATEWAY_IDP__*` | the house identity provider for the chat (ADR 0068); off means its routes do not exist. See [the chat](#the-chat) below |
| `PUBLIC_HOST`, `HTTPS_PORT`, `TLS_DIRECTIVE`, `PUBLIC_ORIGIN`, `ACME_EMAIL`, `PUBLIC_BIND` | only with the proxy overlay (below) |
| `CHAT_REPO`, `CHAT_PG_URL`, `CHAT_IDP_CLIENT_SECRET`, `CHAT_SECRET_KEY` | only with the chat overlay (below) |

Nested gateway settings use the double underscore (`GATEWAY_REDACTION__ENGINE`):
that is the delimiter the gateway reads, and a single underscore is silently
ignored. The compose files interpolate these same names, so what `deploy/.env`
holds is what the gateway receives.

### Migrating an existing `deploy/.env` to the `__` spelling

Deployments created before the profiles work may still set the house-IdP
variables in their old single-underscore spelling. Those names no longer
interpolate into any container, so the gateway would start with the IdP
silently off. Rename all six before the next deploy — the rename and the new
compose files land in the same act, never one without the other:

| Old (no longer read) | New |
|---|---|
| `GATEWAY_IDP_ENABLED` | `GATEWAY_IDP__ENABLED` |
| `GATEWAY_IDP_ISSUER` | `GATEWAY_IDP__ISSUER` |
| `GATEWAY_IDP_INTERNAL_BASE_URL` | `GATEWAY_IDP__INTERNAL_BASE_URL` |
| `GATEWAY_IDP_INTERNAL_TOKEN` | `GATEWAY_IDP__INTERNAL_TOKEN` |
| `GATEWAY_IDP_SIGNING_KEY` | `GATEWAY_IDP__SIGNING_KEY` |
| `GATEWAY_IDP_CLIENTS` | `GATEWAY_IDP__CLIENTS` |

Those six are the entire blast radius: every other nested variable in a live
`deploy/.env` is already double-underscore. Until the rename happens, keep the
committed compose files untouched — a renamed `.env` against the old compose
breaks the house IdP just as surely as the reverse.

## Deployment profiles

Five named shapes cover the range from a self-hosted box to a central
gateway, and from there to a site with no gateway of its own: `homelab`,
`team`, `enterprise`, and the two **standalone** profiles `satellite` and
`generic`. A profile is an **env fragment** plus a **derived overlay list** —
never new compose topology, which would put a fake upstream one careless `-f`
away from production.

| Profile | Fragment | Overlays (before exposure) | Footprint |
|---|---|---|---|
| `homelab` | `deploy/profiles/homelab.env` | base + chat | ~700 MB RSS, ~3.8 GB disk, 2 vCPU |
| `team` | `deploy/profiles/team.env` | base + redaction (pattern-only) + chat | + ~300 MB RSS over homelab |
| `enterprise` | `deploy/profiles/enterprise.env` | base + redaction (NER) + chat + Playwright (from Cerea) | + ~750 MB for NER, + 3.45 GB disk for the browser image |
| `satellite` | `deploy/profiles/satellite.env` | chat + its databases + proxy — **no gateway** | less than homelab: no gateway, no Valkey, no redaction |
| `generic` | `deploy/profiles/generic.env` | chat + its databases + proxy — **no gateway** | as satellite |

The two standalone profiles are installed by **Cerea's** installer, not by
this repository's `install.sh`, because the box they describe runs no gateway
for `install.sh` to set up. ADR 0082 carries the reasoning; the short version:

- **satellite** is Cerea against a *central* Pystino. `OPENAI_BASE_URL` is
  central's `/v1`, OIDC is central's issuer, `USE_USER_TOKEN=true`, and **no
  key is stored on the box at all** — the boot catalogue fetch needs none
  (ADR 0081) and every other call carries the signed-in person's own token. One
  directory serves every satellite, so adding a site adds no users and no
  secrets. It also means there are no satellite-local administrators, and that
  central's redaction and retention policy apply to that site with no way to
  diverge.
- **generic** is Cerea against any OpenAI-compatible third party with one
  shared key. `USE_USER_TOKEN` is forced to `false` **in code** rather than
  offered: user-token mode would put the signed-in person's IdP access token
  into a bearer header sent to a third party. OIDC is still mandatory, but it
  only establishes who somebody is — there is no gateway to bill, meter or
  grant against. Document reading, which elsewhere is `/v1/ocr`, is a
  configured endpoint here (`CHAT_OCR_BASE_URL`, ADR 0083) or absent.

Both need the browser, not only the server, to reach their identity provider:
a satellite whose server can see central while its users' browsers cannot will
never complete a login.

What differs between them is flags and services, not topology:

- **homelab** keeps no ledger (`GATEWAY_ACCOUNTING__ENABLED=false` with
  `GATEWAY_QUOTA__ENABLED=false` — the two travel together or startup refuses
  them), no redaction service (`noop`), local passwords plus the house IdP,
  `FETCH_BACKEND=direct`, the browser code tool on (free — it runs
  client-side), and an empty `CHAT_USAGE_ENABLED` (with no ledger there is
  nothing for the tab to read). The knowledge pipeline stays on in every
  profile: its Postgres is a second database on the gateway's instance, not a
  second container, so it costs nothing extra to keep.
- **team** adds pattern-only redaction (empty `SPACY_MODELS` build **and**
  `REDACTION_NLP_ENGINE=disabled`, ~150 MB) with the local extractor, and
  switches the ledger and quotas back on.
- **enterprise** adds NER redaction (`SPACY_MODELS=en_core_web_lg`, ~900 MB —
  and its licence, CC BY-NC-SA 3.0 where the default build is MIT throughout),
  the headless-browser fetch backend, and an external OIDC provider **instead
  of** the house IdP (which the chat's `CHAT_OIDC_*` overrides follow — left
  unset, the chat still signs in against the house IdP and fails loudly
  against a disabled one). Per-group billing is console records after boot,
  not environment.

The overlay list is derived, not restated: `deploy/profiles/overlays.sh` is
the single source both installers call, with exposure (`edge` or `proxy`) as
the second axis. There is no loopback profile: the chat publishes no port, so
without an exposure overlay it is unreachable — and compose refuses the set
outright, because the chat depends on the proxy.

```bash
. deploy/profiles/overlays.sh
docker compose --env-file deploy/.env $(profile_overlays team edge) up -d --build
```

No profile selects the smoke overlay, and that is the point of the exercise:
a fake upstream in any production set is one careless `-f` from the failure
the overlay model exists to prevent. Smoke stays a development shape, combined
by hand with whatever it verifies — base + smoke + redaction for the redaction
check (`scripts/test_redaction_live.py` reads what the gateway sent at
`localhost:8081`, which is smoke's own loopback pin), base + smoke for the
rest. The redaction overlay carries no fixture stanza at all, so a profile set
with redaction has nowhere for a fake to hide; the edge and proxy overlays
likewise know nothing about it. Where smoke is absent, the gateway's upstream
is whatever `deploy/.env` names — a fresh profile serves nothing real until
its operator adds providers in the console, as database rows rather than
environment variables.

The override and keycloak overlays are never selected there: the first mounts
development sources, the second is a development identity provider. By hand,
a profile is `cp deploy/profiles/<name>.env deploy/.env`, the empty values
filled (each names its generation command), then the compose invocation
above. Every secret is generated locally — nothing arrives over the network.

## Redaction: three build shapes

Which shape a deployment runs is decided by whether the redaction overlay is
passed, and — when it is — by the `SPACY_MODELS` build argument plus the
`REDACTION_NLP_ENGINE` runtime switch. The two must match: a no-NER service in
a model-carrying image wastes the weights, and a `spacy` service in a
model-less image fails visibly at startup rather than detecting nothing.

| Shape | How | Footprint |
|---|---|---|
| Presidio + NER (default) | `SPACY_MODELS="en_core_web_lg"` — the model loads at startup and PERSON/LOCATION/NRP/ORGANIZATION are detectable | ~900 MB RAM |
| Presidio, pattern-only | `SPACY_MODELS=` (empty build) **and** `REDACTION_NLP_ENGINE=disabled` — no model loads, the pattern and checksum recognisers work, and `/healthz` reports the model-backed entities as absent so the console hides them | ~150 MB |
| Redaction off | drop the overlay and set `GATEWAY_REDACTION__ENGINE=noop` on the gateway | nothing |

The default build is MIT-licensed throughout and detects Italian
*identifiers* (fiscal code, VAT, ID card, driving licence, passport) but not
Italian personal names — those need a spaCy model licensed CC BY-NC-SA 3.0.
To accept that obligation and add it, build with
`SPACY_MODELS="en_core_web_lg it_core_news_lg"`.

The overlay also deploys the extractor — the local backend of `/v1/ocr` — as a
second instance of the same image with its NLP engine switched off. Not a
second Dockerfile and deliberately not the same container: extraction is bursty
and payload-heavy while detection is already ~90% of this deployment's CPU, so
one process serving both means a batch of PDFs stalls every prompt waiting on
redaction. With no model loaded the second copy costs ~150 MB instead of ~900.

Redaction refuses open on failure: if the detector is unreachable the request
is refused rather than forwarded unredacted (`GATEWAY_REDACTION__FAIL_OPEN`,
default `false`). A redaction layer that silently stops redacting is worse
than an outage, because nobody finds out.

## Unmetered deployments: no ledger, no quotas

A deployment that wants routing, keys, model access control and redaction —
and has no interest in what anything cost — pays two database writes per
request to fill a table nobody would ever open. The passthrough shape is both
flags off together (ADR 0065):

```bash
# deploy/.env
GATEWAY_ACCOUNTING__ENABLED=false
GATEWAY_QUOTA__ENABLED=false
```

Off means *no row*, never a row of zeros: a zero-cost row cannot be told apart
from one where the arithmetic failed, so the gateway writes no
`usage_records` at all and the report says metering is off rather than showing
an empty table that reads as an idle week. The tables still exist — turning
metering back on records from that moment with no schema change, leaving a gap
in history rather than a broken database.

Quotas without a ledger are **refused at startup**, not warned about: counters
rebuild from `usage_records`, so with none every counter returns zero and every
ceiling silently passes — quotas an administrator believes in and no limit that
can ever fire. Metering without quotas is fine (a ledger with no ceilings is
just reporting); the reverse is incoherent.

## The chat

`docker-compose.chat.yml` deploys the chat application at `/chat`, on the
origin already published. The chat lives in its own repository (Cerea) and is
built from a sibling checkout this deployment names:

```bash
# deploy/.env
CHAT_REPO=/home/you/Cerea   # absolute path — a relative default resolves
                            # against the compose file, which is wrong from a
                            # git worktree, so asking is more honest
```

It is a client, and the overlay is the proof: the chat imports nothing from
the gateway, mounts no gateway source, shares no database and reads no gateway
environment. The only things it is told are URLs and credentials, arriving as
one blob (`DOTENV_LOCAL`, which the image's entrypoint writes as `.env.local`
over its baked `.env`):

- `OPENAI_BASE_URL=http://gateway:8000/v1` — inside the compose network, never
  the public origin: a server-to-server hop needs no TLS, and the browser never
  sees this URL. Every inference call carries the signed-in person's own access
  token (`USE_USER_TOKEN=true`), so the gateway bills that person's group.
- `OPENAI_API_KEY` is unset. The chat builds its model catalogue once at boot,
  before anyone has signed in, by fetching `GET /v1/models`, and that endpoint
  answers publicly without a key (ADR 0081) — the full catalogue,
  brochure-level fields only. Nothing is minted for this container to boot
  with; a real grant is still checked on every actual inference call.
- Sign-in is against the gateway's own IdP: `OPENID_PROVIDER_URL` is
  `PUBLIC_ORIGIN`, the issuer the gateway was configured with, and the chat is
  its own client (`cerea`, with `CHAT_IDP_CLIENT_SECRET`) beside any other.
- `CHAT_SECRET_KEY` encrypts the chat's MCP connector credentials at rest —
  required rather than defaulted, because a default shared by every deployment
  that forgot is indistinguishable from no encryption while looking like it.
- `CHAT_PG_URL` is the chat's own Postgres database **on the gateway's
  instance** — a second database, not a second container — where the knowledge
  pipeline's passages and vectors live, with the chat's own credentials, so
  neither component can read the other's.
- `APP_BASE=/chat` is a **build-time** value: SvelteKit bakes `paths.base`
  into the bundle, so it is an argument to `docker build` and not an
  environment variable at run time. Setting it only in `environment:` builds an
  application rooted at `/` whose every asset 404s behind the prefix.

Nothing new is published: no `ports:`. A browser reaches the chat through the
origin's reverse proxy at `/chat` — one origin keeps the session cookie
same-origin. The chat's MongoDB (`chat-mongo`, pinned to 4.4 — this host's CPU
has no AVX, which every MongoDB since 5.0 requires) is the application's own
store and nothing else reads it.

What reads a pasted URL is the chat's configured fetch seam (`FETCH_BACKEND`,
`direct` by default): the headless-browser overlay lives in the chat
repository with its consumer, publishes no port, and keeping its address in the
overlay rather than the image means an operator may still choose direct HTTPS
fetch without rebuilding.

## Reaching the stack from another machine

The default shape publishes nothing but loopback. From your laptop, forward
one port:

```bash
ssh -L 8000:localhost:8000 user@your-server
```

Then open <http://localhost:8000/console>. **The local port must match the
remote one**: the session cookie is scoped to the origin the login happened
on, so `localhost:8000` on both ends is what keeps it. `localhost` on the far
end still serves `/v1` and the API to the live scripts either way.

## On a public address, behind TLS

`docker-compose.proxy.yml` puts Caddy in front of everything and terminates
TLS. Which of two configurations you are in is decided by whether you have a
name, and `deploy/.env` says so rather than the file guessing:

| | `PUBLIC_HOST` | `TLS_DIRECTIVE` | certificate |
|---|---|---|---|
| dev | an IP address | `tls internal` | Caddy's own CA — a browser warns once |
| prod | an FQDN | *(empty)* | Let's Encrypt, obtained and renewed automatically |

Let's Encrypt will not issue for an IP address, so the first is the only
option without a name. Automatic HTTPS additionally needs the name to resolve
to this host and **ports 80 and 443 reachable from the internet**, because
that is where the challenge arrives.

```bash
# deploy/.env
PUBLIC_HOST=llm.example.org
HTTPS_PORT=443
PUBLIC_ORIGIN=https://llm.example.org
TLS_DIRECTIVE=
ACME_EMAIL=platform@example.org

docker compose --env-file deploy/.env \
  -f deploy/compose/docker-compose.yml \
  -f deploy/compose/docker-compose.smoke.yml \
  -f deploy/compose/docker-compose.redaction.yml \
  -f deploy/compose/docker-compose.proxy.yml up -d --build
```

One origin, one port, everything behind TLS — the console, the chat and `/v1`
alike.

**Behind the proxy, local sign-in is on by default.** The only management
credential this shape seeds is the local admin's (ADR 0043),
and its password comes from `deploy/.env`, which is gitignored and never
committed. Create the account the usual way
(`docker compose ... exec gateway gateway passwd admin@local`).

### The edge shape, for a box that never sees a certificate

`docker-compose.edge.yml` is the shape for a box behind a NetBird (or
equivalent) edge that terminates TLS upstream: the origin router runs in plain
HTTP on loopback, published where the edge's reverse proxy forwards to it.
This still counts as loopback-only under the fourth ground rule — the edge is
the deliberate public act. The session cookie stays `Secure` (the browser's
door is TLS even though this box's hop is not), the minting paths
(`/oauth/token`, `/auth/token`, `/auth/revoke`) are refused at the edge, and
the gateway believes the forwarded proto via `FORWARDED_ALLOW_IPS`. Do not pass
both this and the proxy overlay: one terminates TLS locally, the other assumes
the edge does it.

### A Keycloak to develop against, not a bundled IdP

`docker-compose.keycloak.yml` adds a Keycloak for developing the OIDC paths
(bearer tokens on `/v1`, account linking, group sync) against a real provider.
It does not reverse ADR 0044: the deployment still ships no identity provider
and configures OIDC against whatever issuer `deploy/.env` names — pointing at
a corporate directory instead needs no change here. There are deliberately no
`ports:`: Keycloak listens on the compose network only and reaches a browser
through Caddy at `/idp` on the origin already published. Realm, clients and
test user come from `deploy/keycloak/setup.sh`, which is idempotent and so is
the source of truth rather than a committed realm file.

### The three traps behind a proxy

All three were found building it, and all are recorded in
ADR 0035:

1. **SNI may not carry an IP address**, so an address-only deployment offers no
   certificate at all until Caddy's `default_sni` names one — every handshake
   fails with a TLS "internal error" and nothing above debug in the log.
2. **uvicorn trusts forwarded headers from `127.0.0.1` only**, and the proxy
   arrives from the compose network — without `FORWARDED_ALLOW_IPS` the app
   believes every request is http. The only place that shows is the
   post-logout URL built from `request.base_url`, which an https-registered
   OIDC provider then refuses with a 400 *after a login that worked*.
3. **The session cookie is scoped to the origin the login happened on** — sign
   in on the address you will keep using. The live scripts follow
   `PUBLIC_HOST` when it is set, so source `deploy/.env` before running them.

### Verify what is exposed

```bash
# Caddy's CA, so the checks can verify properly rather than skip verification
docker compose --env-file deploy/.env -f deploy/compose/docker-compose.yml \
  -f deploy/compose/docker-compose.proxy.yml exec proxy \
  cat /data/caddy/pki/authorities/local/root.crt > deploy/tls/caddy-root.crt

set -a; . deploy/.env; set +a     # the live scripts follow PUBLIC_HOST
./scripts/test_public_tls_live.py
```

It asserts the certificate verifies, that a wrong password is refused over
TLS, that the session cookie is `Secure`, that a completion still streams
through the proxy, and that PostgreSQL, Valkey, the fake upstream and the
plaintext application ports are reachable on loopback and **refused on this
host's routable address**.

!!! warning "This is still not a production deployment"

    Read ADR 0035 before calling it one.
    The self-signed configuration still trains people to click through a
    warning, and a session holder can still spend real provider credentials.
