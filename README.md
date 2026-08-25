# LLM Platform

A self-hosted LLM platform for a European research foundation: an OpenAI-compatible
gateway with per-user and per-group accounting, quotas and policy, plus (later) a web
frontend, a desktop shell and RAG.

**Licence: [EUPL-1.2](LICENCE)** for all first-party code. This is a hard requirement,
not a preference — see [ADR 0001](docs/adr/0001-licensing.md) for the dependency policy
it implies.

## Status

| Component | State |
|---|---|
| `apps/gateway` | **Built and tested.** Chat completions (streaming and not), models, API keys, OIDC, accounting, quotas, redaction interface. |
| `packages/shared-py` | **Built.** Detection contract and the deterministic placeholder scheme. |
| `services/redaction` | **Built and tested.** Presidio behind a swappable detection contract; PII never reaches the upstream. |
| `packages/ui` | **Built.** Design tokens and primitives, shared with the Phase 3 chat app. |
| `apps/console` | **Built.** Self-service spend plus reports, quotas, providers, models (with their prices and access), and users at `/console`. |
| `CLAUDE.md` | The context needed to pick this up elsewhere: ground rules, the traps, and how to verify a change. |
| `apps/web`, `apps/desktop`, `services/rag`, `packages/shared` | Placeholders. Each README says what goes there and which decisions are already recorded. |

844 tests pass; `ruff` and `mypy --strict` are clean. `./scripts/smoke_test.sh`
exercises the whole slice over real HTTP, and the full `docker compose` stack has been
built and run against PostgreSQL 18, Valkey and Keycloak — including the complete OIDC
login flow and the reporting API (`./scripts/test_reporting_live.py`, which covers the
dialect-specific SQL the SQLite test suite cannot reach).

## Architecture

```
                    ┌──────────────┐        ┌──────────────┐
   browser ────────▶│  apps/web    │        │ apps/desktop │
   (Phase 2)        │  Next.js PWA │        │  Tauri shell │
                    └──────┬───────┘        └──────┬───────┘
                           │  OIDC + /api          │ wraps the same web app
                           ▼                       ▼
  opencode ──────▶ ┌────────────────────────────────────────┐
  OpenAI SDK       │           apps/gateway                 │
  any OpenAI       │                                        │
  client           │  /v1/chat/completions   API-key auth   │
                   │  /v1/models             per-group      │
                   │  /api/*                 OIDC session   │
                   │                                        │
                   │  ┌──────────────────────────────────┐  │
                   │  │ quota check  →  reserve          │  │
                   │  │ redact request                   │  │
                   │  │ upstream call (forced usage)      │  │
                   │  │ SSE pipeline → rewrite → client   │  │
                   │  │ accounting  →  settle            │  │
                   │  └──────────────────────────────────┘  │
                   └───┬─────────────┬───────────────┬──────┘
                       │             │               │
              ┌────────▼───┐  ┌──────▼─────┐  ┌──────▼──────────┐
              │ PostgreSQL │  │   Valkey   │  │ upstream        │
              │ the ledger │  │  counters  │  │ OpenAI-compat   │
              │ of record  │  │ (a cache)  │  │ (Cortecs, …)    │
              └────────────┘  └────────────┘  └─────────────────┘

                 services/redaction  (out of process — never in the gateway,
                                      because spaCy would block the loop)
                 services/rag        (Phase 3, pgvector)
```

Four ideas carry most of the design:

1. **PostgreSQL is the ledger of record; Valkey is a rebuildable cache.** Quotas still
   evaluate correctly with Valkey gone, just more slowly. Nothing about money is stored
   only in a cache. ([0006](docs/adr/0006-counter-store.md))
2. **Quota checks before the upstream call, accounting after, a reservation in between.**
   Without the reservation, concurrent requests each read the same under-limit total and
   collectively blow the budget. ([0009](docs/adr/0009-quota-model.md))
3. **Accounting never silently reports zero.** Usage is forced out of the upstream, and if
   it never arrives the tokens are counted locally and the row is stamped
   `estimated`. ([0008](docs/adr/0008-accounting-model.md))
4. **The server is authoritative for conversations.** That is why the desktop app gets
   sync for free and why it is a shell rather than a second client.

## Quick start

```bash
cp deploy/.env.example deploy/.env
# edit deploy/.env: set POSTGRES_PASSWORD, GATEWAY_SESSION_SECRET,
# and GATEWAY_UPSTREAM_API_KEY
docker compose -f deploy/compose/docker-compose.yml up --build
```

That starts PostgreSQL, Valkey, runs migrations once, and starts the gateway on
`localhost:8000`. Then create something to talk to:

```bash
docker compose -f deploy/compose/docker-compose.yml exec gateway \
  gateway seed --model my-model --upstream-model gpt-4o-mini
```

It prints an API key (once) and a ready-made `curl`. Any OpenAI client works:

```python
from openai import OpenAI

client = OpenAI(base_url="http://localhost:8000/v1", api_key="gwk_...")
client.chat.completions.create(model="my-model", messages=[{"role": "user", "content": "hello"}])
```

### Trying the stack without a provider key

`docker-compose.smoke.yml` adds a fake OpenAI-compatible upstream so the whole topology
can be exercised with no provider account:

```bash
docker compose --env-file deploy/.env \
  -f deploy/compose/docker-compose.yml \
  -f deploy/compose/docker-compose.smoke.yml up --build
```

It is a separate overlay on purpose — a fake upstream in the base file would be one
careless `-f` away from production.

### Trying the OIDC login

`docker-compose.keycloak.yml` adds Keycloak with a seeded realm, so the login flow can be
exercised without wiring up a real identity provider:

```bash
docker compose --env-file deploy/.env \
  -f deploy/compose/docker-compose.yml \
  -f deploy/compose/docker-compose.smoke.yml \
  -f deploy/compose/docker-compose.keycloak.yml up -d --build

./scripts/test_oidc_flow.py
./scripts/test_reporting_live.py
./scripts/test_redaction_live.py   # needs the redaction overlay too
./scripts/test_console_live.py     # the console, served by the gateway
./scripts/test_providers_live.py   # provider credentials, routing and per-user access
./scripts/test_surfaces_live.py    # responses, anthropic messages and image generation
./scripts/test_quota_race_live.py  # quota admission under concurrency, against real Valkey
```

Each of those bills the demo user, whose cap is EUR 1 an hour, and the fake
upstream reports a million tokens per request. Running several back to back will
legitimately exhaust it; the scripts report that as skipped rather than failed.

That drives the whole authorization-code flow and checks the result: PKCE, ID token
validation, group mapping, the provisioning rules, and a session minting an API key that
then serves a billed completion. Keycloak admin console on <http://localhost:8080> —
or whatever `KEYCLOAK_PORT` is set to — (`admin`/`admin`);
seeded users are described in [deploy/keycloak/README.md](deploy/keycloak/README.md).

### Reaching the console from another machine

The console is at **<http://localhost:8000/console>** on the host running the stack.
If you are working on a server over SSH, forward both ports rather than exposing
them:

```bash
# the LINKS development host, where KEYCLOAK_PORT is set to 18080
ssh -L 8000:localhost:8000 -L 18080:localhost:18080 ubuntu@130.192.84.52
```

Then open <http://localhost:8000/console> in your own browser and sign in as one
of the [seeded users](deploy/keycloak/README.md) — `dave` / `dave-password` is the
administrator.

**Both ports matter.** 8000 is the gateway; 18080 is Keycloak, and the browser
is redirected there to log in. Forwarding only 8000 gets you a page that loads
and a login that goes nowhere.

**And the local port must match the remote one**, not just be any free port. The
browser goes wherever Keycloak advertises, so `-L 9000:localhost:18080` would
serve the console and break the login.

Keycloak is on **18080 rather than 8080** on that host because 8080 is taken on
the machine we tunnel from — when that happens `ssh` prints
`bind: Address already in use`, carries on with the other forward, and you get
exactly that symptom: a console that loads and a login that cannot connect.
`KEYCLOAK_PORT` in `deploy/.env` moves the published port *and* the hostname
Keycloak advertises, so the browser, the tunnel and the `iss` claim keep
agreeing:

```bash
KEYCLOAK_PORT=18080 docker compose --env-file deploy/.env \
  -f deploy/compose/docker-compose.yml \
  -f deploy/compose/docker-compose.smoke.yml \
  -f deploy/compose/docker-compose.keycloak.yml up -d
```

Changing it needs the **gateway restarted too** — it reads OIDC discovery once
at startup, so a gateway that was already running keeps sending browsers to the
old port. To check a forward is actually up before blaming the stack:

```bash
curl -s http://localhost:18080/realms/llm-platform | head -c 200  # realm JSON
lsof -nP -iTCP:18080 -sTCP:LISTEN                                 # who has the port
```

**Why a tunnel and not just the server's address.** The OIDC configuration is
pinned to `localhost` in three places — Keycloak's `KC_HOSTNAME`, the gateway's
`GATEWAY_OIDC__REDIRECT_URI`, and the realm's `redirectUris`. Over a tunnel
`localhost` is true at both ends and everything works unchanged. To serve the
console at a real hostname instead, all three have to change together; the
frontend/backchannel split in
[docker-compose.keycloak.yml](deploy/compose/docker-compose.keycloak.yml)
explains why.

And do not put this stack on a routable address *as it stands*: Keycloak runs
`start-dev` with `admin`/`admin` and an in-memory database, there is no TLS, the
session cookie is not `Secure`, and the seeded users have published passwords.
Those four things are the whole reason for the rule — "On a public address,
behind TLS" below closes all four, and
[ADR 0035](docs/adr/0035-public-tls-exposure.md) says what is still true
afterwards.

### Without a tunnel, over a private overlay network

A WireGuard mesh — NetBird, Tailscale, plain WireGuard — gives the host an
address only enrolled peers can reach, authenticated and encrypted below HTTP.
That is the one way to drop the tunnel without putting any of the above on a
routable address.

Set `OVERLAY_ADDR` in `deploy/.env` to an address that **already exists on this
host** (`ip -brief addr` will show it; on the LINKS development host it is
`100.124.242.79` on `wt0`), and add the overlay file:

```bash
docker compose --env-file deploy/.env \
  -f deploy/compose/docker-compose.yml \
  -f deploy/compose/docker-compose.smoke.yml \
  -f deploy/compose/docker-compose.keycloak.yml \
  -f deploy/compose/docker-compose.redaction.yml \
  -f deploy/compose/docker-compose.overlay.yml up -d
```

Then open `http://<OVERLAY_ADDR>:8000/console` from any enrolled peer. The
overlay moves all three pinned URLs together, and registers the extra callback
on the realm with a one-shot `kcadm` container — the exact URI, never a
wildcard, because an open redirect on an OIDC client hands the authorization
code to whoever asks.

Three things worth knowing before you use it:

- **Publishing is additive.** `127.0.0.1` is still bound, so the live scripts
  and healthchecks keep working when the mesh is down. Without the overlay file
  the stack is loopback-only, which is the default the base files now set —
  Docker's own default is `0.0.0.0`, meaning every interface the host has.
- **Signing in works on the overlay address only.** The gateway sends exactly
  one `redirect_uri`, and the login-state cookie is scoped to the origin the
  flow began on, so starting at `localhost:8000` and being sent back to the
  overlay address loses it. The symptom is a callback answered with *"No login
  is in progress in this browser"*, which reads like a broken flow rather than
  a mismatched hostname. `/v1` is unaffected: it uses API keys, not sessions.
- **It narrows where the stack is reachable from, and nothing else.** Keycloak
  still has `admin`/`admin`, there is still no TLS, and the gateway still holds
  real provider credentials that anyone with a session can spend.

## On a public address, behind TLS

`docker-compose.proxy.yml` puts Caddy in front of everything and terminates TLS,
in either of two configurations. Which one you are in is decided by whether you
have a name, and `deploy/.env` says so rather than the file guessing:

| | `PUBLIC_HOST` | `TLS_DIRECTIVE` | certificate |
|---|---|---|---|
| dev | an IP address | `tls internal` | Caddy's own CA — a browser warns once |
| prod | an FQDN | *(empty)* | Let's Encrypt, obtained and renewed automatically |

Let's Encrypt will not issue for an IP address, so the first is the only option
without a name; automatic HTTPS additionally needs the name to resolve to this
host and **ports 80 and 443 reachable from the internet**, because that is where
the challenge arrives.

```bash
# deploy/.env
PUBLIC_HOST=130.192.84.103
HTTPS_PORT=8443
TLS_DIRECTIVE="tls internal"
KEYCLOAK_ADMIN_PASSWORD=...    # required, no default
KEYCLOAK_SEED_PASSWORD=...     # required, no default
KEYCLOAK_CLIENT_SECRET=...     # required, no default

docker compose --env-file deploy/.env \
  -f deploy/compose/docker-compose.yml \
  -f deploy/compose/docker-compose.smoke.yml \
  -f deploy/compose/docker-compose.keycloak.yml \
  -f deploy/compose/docker-compose.redaction.yml \
  -f deploy/compose/docker-compose.proxy.yml up -d --build
```

The console is then at **<https://130.192.84.103:8443/console>** and Keycloak at
`/kc` of the same origin — one origin and one port, because the gateway owns
`/auth/callback` and Keycloak owns `/realms`, and `KC_HTTP_RELATIVE_PATH` is what
separates them.

**The three required passwords are the point.** This stack's fourth ground rule
is that it never goes on a routable address, and that rule is a list of four
specific holes: plaintext, `admin`/`admin`, a session cookie without `Secure`,
and seeded users whose passwords are in this repository. The proxy overlay closes
all four — the last by resetting the admin password, every seeded user's password
and the OIDC client secret from the environment on **every** `up`, because
Keycloak's in-memory database re-imports the fixture, published passwords and all,
whenever the container is recreated. Set them and the fixture stops being a
credential; leave them unset and compose refuses to start.

To check it is only what it claims to be:

```bash
# Caddy's CA, so the checks can verify properly rather than skip verification
docker compose --env-file deploy/.env -f deploy/compose/docker-compose.yml \
  -f deploy/compose/docker-compose.proxy.yml exec proxy \
  cat /data/caddy/pki/authorities/local/root.crt > deploy/tls/caddy-root.crt

set -a; . deploy/.env; set +a     # the live scripts follow PUBLIC_HOST
./scripts/test_public_tls_live.py
```

It asserts the certificate verifies, that `admin`/`admin` and `dave-password` and
the published client secret are refused, that the session cookie is `Secure`,
that a completion still streams through the proxy, and that PostgreSQL, Valkey,
the fake upstream and both plaintext application ports are reachable on loopback
and **refused on this host's routable address**.

Read [ADR 0035](docs/adr/0035-public-tls-exposure.md) before calling this a
production deployment. It is not one: Keycloak is still `start-dev` on an
in-memory database, the self-signed configuration still trains people to click
through a warning, and a session holder can still spend real provider
credentials.

## Local development without Docker

```bash
uv sync
uv run pytest                    # 343 tests, no services needed
uv run ruff check . && uv run ruff format --check .
uv run mypy apps/gateway/src packages/shared-py/src
```

Tests run against SQLite and a fake upstream transport, so they need no PostgreSQL, no
Valkey and no network.

For an end-to-end check over a real socket — a real server, a real database and a real
streaming upstream — run:

```bash
./scripts/smoke_test.sh
```

It starts everything on temporary ports, drives the endpoints, prints the resulting
ledger and cleans up. It also demonstrates the quota overrun policy: the request that
crosses the limit is admitted, the next one gets a 429.

Against a real database:

```bash
export GATEWAY_DATABASE_URL=postgresql+asyncpg://gateway:gateway@localhost:5432/gateway
uv run alembic -c apps/gateway/alembic.ini upgrade head
uv run gateway seed
uv run gateway serve --reload
```

## Documentation

- **[docs/adr/](docs/adr/README.md)** — every significant decision, with the licence,
  version and CVE evidence behind it and the date it was checked. Start here.
- **[apps/gateway/README.md](apps/gateway/README.md)** — the gateway's surfaces, layout,
  and a table of the streaming traps with the file that handles each.

## Repository layout

```
apps/gateway/        FastAPI service — the only thing built this session
apps/web/            Next.js frontend            (Phase 2, placeholder)
apps/desktop/        Tauri shell                 (Phase 4, placeholder)
services/rag/        indexing + retrieval        (Phase 3, placeholder)
services/redaction/  Presidio detection service, behind a swappable contract
packages/shared/     shared TypeScript types     (Phase 2, placeholder)
packages/shared-py/  shared Python contracts
scripts/             Cortecs pricing importer, smoke test; opencode bootstrap (Phase 4)
deploy/compose/      docker compose: base, dev override, smoke + keycloak overlays
deploy/keycloak/     seeded dev realm for the OIDC flow
docs/adr/            architecture decision records
```

## What is deliberately not here yet

Phase 2 onward: the frontend, RAG, the code sandbox, MCP, the desktop app, the Presidio
engine, and the `opencode` device-flow bootstrap. Each has a README saying what goes there
and an ADR recording the decision already taken, so none of that research needs repeating.

The gateway's own known gaps are listed at the end of
[apps/gateway/README.md](apps/gateway/README.md) and in the ADRs — most notably: the full
OIDC browser flow is untested without a real identity provider, and there is no GDPR
erasure procedure for `assistant_text` yet.
