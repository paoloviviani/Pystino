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

The host needs Docker and nothing else. One directory holds the deployment:
a `.env` (every secret, minted for you) and, for a distributed install, the
release's `compose.yaml`. Images come from `ghcr.io/paoloviviani/` (private
while the repositories are: `docker login ghcr.io` once with a `read:packages`
token — `init` says so if it is missing).

```bash
mkdir /srv/pystino && cd /srv/pystino
docker run --rm -it -u "$(id -u):$(id -g)" -v "$PWD:/deploy" -w /deploy \
  ghcr.io/paoloviviani/pystino-gateway:<version> pystino init \
  --origin https://llm.example.org --admin-email you@example.org --preset team
./pystino doctor && docker compose up -d --wait
```

`init` prints the bundled Authelia's first password once; the first sign-in
with the administrator email becomes the administrator. Presets: `homelab`,
`team`, `enterprise` (a full stack), and `satellite` / `generic` for the chat
alone (`cerea init` is the same thing, Cerea-branded). TLS: `--tls acme`
(a public name), `internal` (development), `upstream` (TLS ends in front,
e.g. NetBird). Bring your own OIDC provider with `--idp external
--oidc-issuer …`. Upgrading is `./pystino upgrade <version>` and the same
`docker compose up -d --wait`.

### Development

The same stack, built from your checkouts instead of pulled:

```bash
uv run pystino init --dir ~/pystino-dev --mode dev --tls internal \
  --origin https://dev.example.test:8443 --admin-email you@example.org \
  [--cerea-src ../Cerea]
cd ~/pystino-dev && docker compose up -d --build --wait
```

`deploy/dev/smoke.yml` adds a fake upstream (no provider account needed), and
`deploy/dev/keycloak/` a Keycloak to develop the external-IdP paths against.
The design, and why the old overlays, the edge Caddyfile, the CA bundle, the
house IdP and the bash installers are gone: ADRs 0086–0088.

Moving an install made by the old installer: `pystino adopt <old deploy dir>`
(reads only; carries every secret over and prints the cutover).

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
deploy/stack/        the one topology: compose.yaml (+ compose.build*.yaml for
                     development), the proxy and Authelia images, release.env
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
