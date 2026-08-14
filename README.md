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
| `apps/web`, `apps/desktop`, `services/rag`, `services/redaction`, `packages/shared` | Placeholders. Each README says what goes there and which decisions are already recorded. |

311 tests pass; `ruff` and `mypy --strict` are clean. `./scripts/smoke_test.sh`
exercises the whole slice over real HTTP, and the full `docker compose` stack has been
built and run against PostgreSQL 18 and Valkey.

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

                 services/redaction  (Phase 2, out of process — never in the
                                      gateway, because spaCy would block the loop)
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

## Local development without Docker

```bash
uv sync
uv run pytest                    # 311 tests, no services needed
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
services/redaction/  Presidio detection service  (Phase 2, placeholder)
packages/shared/     shared TypeScript types     (Phase 2, placeholder)
packages/shared-py/  shared Python contracts
scripts/             Cortecs pricing importer, smoke test; opencode bootstrap (Phase 4)
deploy/compose/      docker compose: base, dev override, smoke overlay
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
