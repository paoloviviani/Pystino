<p align="center">
  <img src="logo-512.png" alt="The Pystino logo" width="256" />
</p>

# Pystino

A self-hosted LLM platform, built and run by its author at personal expense: an
OpenAI-compatible
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
| `packages/ui` | **Built.** Design tokens and primitives, shared with the admin console. |
| `apps/console` | **Built.** Self-service spend plus reports, quotas, providers, models (with their prices and access), and users at `/console`. |
| `CLAUDE.md` | The context needed to pick this up elsewhere: ground rules, the traps, and how to verify a change. |
| `apps/web`, `apps/desktop`, `services/rag`, `packages/shared` | Placeholders. Each README says what goes there and which decisions are already recorded. |

844 tests pass; `ruff` and `mypy --strict` are clean. `./scripts/smoke_test.sh`
exercises the whole slice over real HTTP, and the full `docker compose` stack has been
built and run against PostgreSQL 18 and Valkey — console sign-in is by local
email + password ([ADR 0043](docs/adr/0043-local-authentication.md)), with OIDC
available against any provider you point it at
([docs/oidc-generic-provider.md](docs/oidc-generic-provider.md)) and the reporting
API covered live (`./scripts/test_reporting_live.py`, which covers the
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

### Signing in to the console

Enable local sign-in and create the first administrator (it prompts, so nothing
lands in shell history):

```bash
docker compose --env-file deploy/.env \
  -f deploy/compose/docker-compose.yml \
  -f deploy/compose/docker-compose.smoke.yml up -d --build

docker compose --env-file deploy/.env \
  -f deploy/compose/docker-compose.yml \
  -f deploy/compose/docker-compose.smoke.yml exec gateway \
  gateway passwd admin@local
```

The console is at <http://localhost:8000/console>; sign in as the account you
just created. OIDC against GitLab, Entra ID or any other provider is a
`.env` change, not a new component — see
[docs/oidc-generic-provider.md](docs/oidc-generic-provider.md).

The live checks sign in the same way; set `GATEWAY_LOCAL_ADMIN_PASSWORD` in
`deploy/.env` to the password you chose, then:

```bash
set -a; . deploy/.env; set +a
./scripts/test_reporting_live.py
./scripts/test_redaction_live.py   # needs the redaction overlay too
./scripts/test_console_live.py     # the console, served by the gateway
./scripts/test_providers_live.py   # provider credentials, routing and per-user access
./scripts/test_surfaces_live.py    # responses, anthropic messages and image generation
./scripts/test_quota_race_live.py  # quota admission under concurrency, against real Valkey
```

Each of those bills its user, whose cap is EUR 1 an hour, and the fake
upstream reports a million tokens per request. Running several back to back will
legitimately exhaust it; the scripts report that as skipped rather than failed.

### Reaching the console from another machine

The console is at **<http://localhost:8000/console>** on the host running the stack.
If you are working on a server over SSH, forward that one port rather than exposing
it:

```bash
ssh -L 8000:localhost:8000 ubuntu@130.192.84.52
```

Then open <http://localhost:8000/console> in your own browser and sign in with
the local administrator you created above. **The local port must match the
remote one**, not just be any free port: the session cookie is scoped to the
origin the login happened on, so `localhost:8000` on both ends is what keeps
it.

**Why a tunnel and not just the server's address.** The stack is loopback-only
by default — nothing is published on any interface the host has. Serving it on
a real address is a deliberate act covered by
["On a public address, behind TLS"](#on-a-public-address-behind-tls) below.

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

docker compose --env-file deploy/.env \
  -f deploy/compose/docker-compose.yml \
  -f deploy/compose/docker-compose.smoke.yml \
  -f deploy/compose/docker-compose.redaction.yml \
  -f deploy/compose/docker-compose.proxy.yml up -d --build
```

The console is then at **<https://130.192.84.103:8443/console>** — one origin,
one port, everything behind TLS.

**Behind the proxy, local sign-in is on by default.** The only management
credential this shape seeds is the local admin's (ADR 0043), and its password
comes from `deploy/.env` — which is gitignored and never committed. Create the
account the same way as above (`docker compose ... exec gateway gateway passwd
admin@local`). OIDC, when configured, rides the same TLS origin with the
provider hosted wherever you run it.

To check it is only what it claims to be:

```bash
# Caddy's CA, so the checks can verify properly rather than skip verification
docker compose --env-file deploy/.env -f deploy/compose/docker-compose.yml \
  -f deploy/compose/docker-compose.proxy.yml exec proxy \
  cat /data/caddy/pki/authorities/local/root.crt > deploy/tls/caddy-root.crt

set -a; . deploy/.env; set +a     # the live scripts follow PUBLIC_HOST
./scripts/test_public_tls_live.py
```

It asserts the certificate verifies, that a wrong password is refused over TLS,
that the session cookie is `Secure`, that a completion still streams through the
proxy, and that PostgreSQL, Valkey, the fake upstream and the plaintext
application ports are reachable on loopback and **refused on this host's
routable address**.

Read [ADR 0035](docs/adr/0035-public-tls-exposure.md) before calling this a
production deployment. It is not one: the self-signed configuration still
trains people to click through a warning, and a session holder can still spend
real provider credentials.

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

The full documentation is a Material for MkDocs site, built from `docs/`:

```bash
uv run mkdocs build --strict   # output in site/; `mkdocs serve` to browse
```

- **[docs/index.md](docs/index.md)** — start here: status, the four ideas, and
  a map of the rest.
- **[docs/adr/](docs/adr/README.md)** — every significant decision, with the licence,
  version and CVE evidence behind it and the date it was checked.
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
scripts/             live checks, fake upstream, pricing importer
deploy/compose/      docker compose: base, dev override, smoke + redaction + proxy overlays
docs/adr/            architecture decision records
```

## What is deliberately not here yet

Phase 2 onward: the frontend, RAG, the code sandbox, MCP, the desktop app, the Presidio
engine, and the `opencode` device-flow bootstrap. Each has a README saying what goes there
and an ADR recording the decision already taken, so none of that research needs repeating.

The gateway's own known gaps are listed at the end of
[apps/gateway/README.md](apps/gateway/README.md) and in the ADRs — most notably: the
OIDC flow is untested against a real identity provider (the bundled Keycloak rig was
removed in [ADR 0044](docs/adr/0044-keycloak-removed.md); test any new provider against
your instance before relying on it), and there is no GDPR
erasure procedure for `assistant_text` yet.
