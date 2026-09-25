<p align="center">
  <img src="logo.png" alt="The Pystino logo" width="256" />
</p>

# Pystino

A self-hosted OpenAI-compatible LLM gateway with per-user and per-group
accounting, quotas, and PII redaction — plus an admin console. The chat
application that fronts it is [Cerea](https://github.com/paoloviviani/Cerea),
a separate repository and a plain `/v1` client; a desktop shell is still
planned.

> The name is *pistino* — Turin dialect for a nitpicker, the person who checks
> every last detail. Which is what an accounting gateway is for: counting
> every token, every cent, every quota, and telling you exactly where each
> one went.

**Licence: [EUPL-1.2](LICENCE)** for all first-party code — see
ADR 0001 for the dependency policy it implies.

## Components

| | |
|---|---|
| `apps/gateway` | The gateway: the metered `/v1` surfaces (chat completions, responses, Anthropic messages, embeddings, images, OCR, search — streaming and not), models, files and vector stores, API keys, OIDC, accounting, quotas, redaction; serves the console. |
| `packages/shared-py` | Detection contract and the deterministic placeholder scheme. |
| `services/redaction` | Presidio detection service, out of process. |
| `packages/ui` | Design tokens and UI primitives, shared with the console. |
| `apps/console` | The admin console: usage reports, quotas, providers, models, users, groups, settings. |

The chat application, the desktop shell and the RAG pipeline are **not here**.
They live in [Cerea](https://github.com/paoloviviani/Cerea), which is a `/v1`
*client* of this gateway and imports nothing from it.

`deploy/opencode/` is the exception that proves the rule: it sets up coding
agents on people's own machines, and everything in it speaks to the gateway
over `/v1` like any other client — see
[docs/coding-agents.md](docs/coding-agents.md).

Code and comments here cite decisions by number — `(ADR 0032)`. The decision
record itself is internal and deliberately not linked: a citation names the
reasoning so it can be asked for, where a link would promise a source a reader
cannot open.

## Architecture

```
                    ┌───────────────────────────────┐
   any /v1 client ─▶│  Cerea, opencode, the OpenAI  │
                    │  SDKs — a key or an OIDC      │
                    │  bearer, nothing special      │
                    └───────────────┬───────────────┘
                                    ▼
  apps/console ──▶ ┌────────────────────────────────────────┐
  OpenAI SDK       │           apps/gateway                 │
  any OpenAI       │                                        │
  client           │  /v1/chat/completions   API-key auth   │
                   │  /v1/models             per-group      │
                   │  /api/*                 OIDC session   │
                   │                                        │
                   │  ┌──────────────────────────────────┐  │
                   │  │ quota check  →  reserve          │  │
                   │  │ redact request                   │  │
                   │  │ upstream call (forced usage)     │  │
                   │  │ SSE pipeline → rewrite → client  │  │
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
   (ADR 0006)
2. **Quota check before the upstream call, accounting after, a reservation in
   between** — otherwise concurrent requests each read the same under-limit
   total and collectively blow the budget. (ADR 0009)
3. **Accounting never silently reports zero**: if usage never arrives from the
   upstream, tokens are counted locally and the row is stamped `estimated`.
   (ADR 0008)
4. **The server is authoritative for conversations** — which is why the
   desktop app is a shell rather than a second client.

## Quick start

The host needs Docker and nothing else. `deploy/` in this repository is a
self-contained deployment: the gateway and its console, no chat. Images come
from `ghcr.io/paoloviviani/` (private while the repository is: `docker login
ghcr.io` once with a `read:packages` token).

```bash
cp deploy/.env.example deploy/.env && chmod 600 deploy/.env
$EDITOR deploy/.env      # every variable is explained where it stands
cd deploy && docker compose up -d --wait
```

There is no installer: every secret's minting command is a comment above it
in `.env.example` (`openssl rand -hex 32`, or, for the bundled Authelia's
digests, `docker run --rm authelia/authelia:4.39.22 authelia crypto hash
generate …`). The first sign-in whose email matches
`PYSTINO_BOOTSTRAP_ADMIN_EMAIL` becomes the administrator. `TLS_MODE`:
`acme` (a public name), `internal` (development), `upstream` (TLS ends in
front, e.g. NetBird). Bring your own OIDC provider by leaving the `authelia`
profile out of `COMPOSE_PROFILES` and setting `OIDC_ISSUER` and the
`OIDC_CONSOLE_CLIENT_*` variables yourself. Upgrading is `git pull && docker
compose pull && docker compose up -d --wait`. Full details, including TLS,
identity and backups: [docs/deployment.md](docs/deployment.md).

**Want the chat too?** The separate `cerea-deploy` repository (private for
now) is the full stack, with its own `./configure`; `deploy/.env` here uses
the same variable names, so it carries over.

`deploy/dev/smoke.yml` adds a fake upstream to develop against (no provider
account needed): `docker compose -f deploy/compose.yaml -f
deploy/dev/smoke.yml up -d --wait`, with `PYSTINO_SRC` set to this checkout.
`deploy/dev/keycloak/` is a Keycloak to develop the external-IdP paths
against. The design, and why the old installer, the baked-in proxy and
Authelia images, and the bash installers before them are gone: ADRs
0086–0088, 0091.

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
`test_cache_accounting_live.py`, `test_public_tls_live.py`) sign in with the
local administrator; set `GATEWAY_LOCAL_ADMIN_PASSWORD` in `deploy/.env`
first.

## Documentation

```bash
uv run mkdocs build --strict   # output in site/; `mkdocs serve` to browse
```

- **[docs/index.md](docs/index.md)** — start here.
- **[apps/gateway/README.md](apps/gateway/README.md)** — the gateway's
  surfaces, layout, and streaming traps.
- **[docs/coding-agents.md](docs/coding-agents.md)** — enrolling opencode
  against `/v1`.
- The decision record is **internal and not in this repository**. Code,
  comments and docs cite it by number — `(ADR 0032)` — so a citation names
  reasoning that can be asked for, rather than promising a source the reader
  cannot open.

## Repository layout

```
apps/gateway/        FastAPI service — gateway + console host
apps/console/        React admin console, built into the gateway image

services/redaction/  Presidio detection service
packages/ui/         shared design tokens + UI primitives
packages/shared-py/  shared Python contracts
scripts/             live checks, fake upstream, pricing importer
deploy/              the Pystino-only deployment: compose.yaml, caddy/ and
                     authelia/ (mounted into stock images), .env.example,
                     pin.py, release.env
deploy/dev/          development-only fixtures: fake upstream, Keycloak
deploy/ci/           the image workflow, until it can live in .github/workflows
deploy/opencode/     coding-agent setup: pasted-key installer, enrollment CLI
docs/                the documentation site (mkdocs.yml)
```

## What is deliberately not here

The chat application, the RAG pipeline, the code sandbox and MCP are Cerea's;
the desktop shell is not started. Each has an ADR recording the decision
already taken. The gateway's own known gaps are listed in
[apps/gateway/README.md](apps/gateway/README.md) and in
[docs/gateway.md](docs/gateway.md#known-gaps).
