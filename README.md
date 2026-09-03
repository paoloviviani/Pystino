<p align="center">
  <img src="logo-512.png" alt="The Pystino logo" width="256" />
</p>

# Pystino

A self-hosted OpenAI-compatible LLM gateway with per-user and per-group
accounting, quotas, and PII redaction — plus an admin console. A web
frontend, desktop shell and RAG are planned.

> The name is *pistino* — Turin dialect for a nitpicker, the person who checks
> every last detail. Which is what an accounting gateway is for: counting
> every token, every cent, every quota, and telling you exactly where each
> one went.

**Licence: [EUPL-1.2](LICENCE)** for all first-party code — see
[ADR 0001](docs/adr/0001-licensing.md) for the dependency policy it implies.

## Components

| | |
|---|---|
| `apps/gateway` | The gateway: `/v1` chat completions (streaming and not), models, API keys, OIDC, accounting, quotas, redaction; serves the console. |
| `packages/shared-py` | Detection contract and the deterministic placeholder scheme. |
| `services/redaction` | Presidio detection service, out of process. |
| `packages/ui` | Design tokens and UI primitives, shared with the console. |
| `apps/console` | The admin console: usage reports, quotas, providers, models, users, groups, settings. |
| `apps/web`, `apps/desktop`, `services/rag`, `packages/shared` | Placeholders for later phases. |

## Architecture

```
                    ┌──────────────┐        ┌──────────────┐
   browser ────────▶│  apps/web    │        │ apps/desktop │
   (planned)        │  Next.js PWA │        │  Tauri shell │
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

                 services/redaction  (out of process)
```

The four ideas that carry most of the design:

1. **PostgreSQL is the ledger of record; Valkey is a rebuildable cache.**
   Quotas still evaluate correctly with Valkey gone, just more slowly.
   ([0006](docs/adr/0006-counter-store.md))
2. **Quota check before the upstream call, accounting after, a reservation in
   between** — otherwise concurrent requests each read the same under-limit
   total and collectively blow the budget. ([0009](docs/adr/0009-quota-model.md))
3. **Accounting never silently reports zero**: if usage never arrives from the
   upstream, tokens are counted locally and the row is stamped `estimated`.
   ([0008](docs/adr/0008-accounting-model.md))
4. **The server is authoritative for conversations** — which is why the
   desktop app is a shell rather than a second client.

## Quick start

```bash
cp deploy/.env.example deploy/.env
# edit deploy/.env: set POSTGRES_PASSWORD, GATEWAY_SESSION_SECRET,
# and GATEWAY_UPSTREAM_API_KEY
docker compose -f deploy/compose/docker-compose.yml up --build
```

This starts PostgreSQL and Valkey, runs migrations once, and starts the
gateway on `localhost:8000`. Then create something to talk to:

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

### Trying it without a provider key

`docker-compose.smoke.yml` adds a fake OpenAI-compatible upstream so the whole
stack can be exercised with no provider account:

```bash
docker compose --env-file deploy/.env \
  -f deploy/compose/docker-compose.yml \
  -f deploy/compose/docker-compose.smoke.yml up --build
```

### The console

Enable local sign-in and create the first administrator (it prompts, so
nothing lands in shell history):

```bash
docker compose --env-file deploy/.env \
  -f deploy/compose/docker-compose.yml \
  -f deploy/compose/docker-compose.smoke.yml exec gateway \
  gateway passwd admin@local
```

The console is at <http://localhost:8000/console>. Signing in through an
identity provider (GitLab, Entra ID, anything OIDC) is configured in the
console's Settings screen or via env — see
[docs/oidc-generic-provider.md](docs/oidc-generic-provider.md).

### On a public address, behind TLS

`docker-compose.proxy.yml` puts Caddy in front of everything and terminates
TLS — Caddy's own CA for an IP address, Let's Encrypt for a name:

```bash
# deploy/.env
PUBLIC_HOST=your.domain.or.ip
HTTPS_PORT=8443
TLS_DIRECTIVE="tls internal"    # empty for a real name + Let's Encrypt

docker compose --env-file deploy/.env \
  -f deploy/compose/docker-compose.yml \
  -f deploy/compose/docker-compose.smoke.yml \
  -f deploy/compose/docker-compose.redaction.yml \
  -f deploy/compose/docker-compose.proxy.yml up -d --build
```

The console is then at `https://<PUBLIC_HOST>:8443/console`. Read
[ADR 0035](docs/adr/0035-public-tls-exposure.md) before calling this a
production deployment. `./scripts/test_public_tls_live.py` asserts the
certificate verifies and that nothing else is reachable on a routable address.

## Development

```bash
uv sync
uv run pytest                    # SQLite + a fake upstream: no services needed
uv run ruff check . && uv run ruff format --check .
uv run mypy apps/gateway/src packages/shared-py/src
./scripts/smoke_test.sh          # end to end over real HTTP, on temporary ports
```

Live checks against a running stack (`./scripts/test_console_live.py`,
`test_reporting_live.py`, `test_redaction_live.py`, `test_providers_live.py`,
`test_surfaces_live.py`, `test_quota_race_live.py`,
`test_public_tls_live.py`) sign in with the local administrator; set
`GATEWAY_LOCAL_ADMIN_PASSWORD` in `deploy/.env` first.

## Documentation

```bash
uv run mkdocs build --strict   # output in site/; `mkdocs serve` to browse
```

- **[docs/index.md](docs/index.md)** — start here.
- **[docs/adr/](docs/adr/README.md)** — every significant decision, with the
  licence, version and CVE evidence behind it.
- **[apps/gateway/README.md](apps/gateway/README.md)** — the gateway's
  surfaces, layout, and streaming traps.

## Repository layout

```
apps/gateway/        FastAPI service — gateway + console host
apps/console/        React admin console, built into the gateway image
apps/web/            Next.js frontend            (planned)
apps/desktop/        Tauri shell                 (planned)
services/rag/        indexing + retrieval        (planned)
services/redaction/  Presidio detection service
packages/ui/         shared design tokens + UI primitives
packages/shared/     shared TypeScript types     (planned)
packages/shared-py/  shared Python contracts
scripts/             live checks, fake upstream, pricing importer
deploy/compose/      docker compose: base + smoke, redaction and proxy overlays
docs/adr/            architecture decision records
```

## What is deliberately not here yet

The web frontend, RAG, the code sandbox, MCP and the desktop app — each has a
README saying what goes there and an ADR recording the decision already taken.
The gateway's known gaps are listed in
[apps/gateway/README.md](apps/gateway/README.md) and the ADRs.
