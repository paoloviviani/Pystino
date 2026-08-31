# Architecture

## Topology

```
                     ┌──────────────────┐
  browser ──────────▶│  apps/web (Vite) │  the chat, at /chat
                     └────────┬─────────┘
                              │ OIDC + /chat/api
                              ▼
                     ┌──────────────────┐
                     │   apps/chat-api  │  a /v1 client — imports nothing
                     └────────┬─────────┘  from the gateway (ADR 0040)
                              │
  opencode ────────▶ ┌────────▼─────────────────────────────┐
  OpenAI SDK         │             apps/gateway              │
  any OpenAI client  │                                       │
  apps/console ◀─────│  /v1/*        API key (or OIDC token, │
  (served by the     │               ADR 0040)               │
   gateway)          │  /api/*       session cookie          │
                     │  /console     the admin SPA           │
                     │                                       │
                     │  ┌────────────────────────────────┐   │
                     │  │ resolve → reserve → redact →   │   │
                     │  │ upstream → record → settle     │   │
                     │  └────────────────────────────────┘   │
                     └───┬─────────────┬──────────────┬──────┘
                         │             │              │
                ┌────────▼───┐  ┌──────▼─────┐  ┌─────▼───────────┐
                │ PostgreSQL │  │   Valkey   │  │ upstream        │
                │ the ledger │  │  counters  │  │ OpenAI-compat   │
                │ of record  │  │ (a cache)  │  │ (Cortecs, …)    │
                └────────────┘  └────────────┘  └─────────────────┘

                services/redaction   out of process — spaCy must never
                (Presidio)           enter the gateway's event loop
```

Everything a browser sees comes from two FastAPI services: the gateway (console,
management API, `/v1`) and chat-api (the chat backend and the chat SPA at
`/chat`). They are separate processes on separate ports, coupled only through
`/v1` and OIDC — which is what makes each of them replaceable and, behind the
proxy overlay, routable under one origin.

## Components

| Component | Role |
|---|---|
| `apps/gateway` | The substance. `/v1` proxy surfaces, management API, console hosting, the ledger, quotas, redaction interface. |
| `apps/chat-api` | The chat backend: conversations, and the loop that produces them. A `/v1` client; imports nothing from the gateway. |
| `apps/web` | The chat SPA (Vite), served by chat-api at `/chat`. Lives under `/chat` **in every deployment shape** — behind the proxy, `/api` and `/auth` at the origin root belong to the gateway. |
| `apps/console` | The admin SPA, served by the gateway at `/console`. Optional at build time. |
| `packages/ui` | Design tokens and primitives — Material Design 3 in one file (`tokens.css`), shared by console and chat. |
| `packages/shared-py` | The detection contract and the deterministic placeholder scheme — the wire format between gateway and redaction. |
| `services/redaction` | Presidio behind the detection contract, out of process. |
| `deploy/compose` | The stack: base + dev override + smoke + redaction + chat + proxy overlays. |
| `scripts/` | Live checks against a running stack; the fake upstream; the pricing importer; the benchmark. |

## Two workspaces, one deliberate exception

Python is a `uv` workspace (`apps/gateway`, `apps/chat-api`,
`packages/shared-py`); TypeScript is a `pnpm` workspace (`packages/ui`,
`apps/console`, `apps/web`). No orchestrator binds them
([ADR 0002](adr/0002-monorepo-tooling.md)).

`services/redaction` is **not** a workspace member on purpose: it depends on
Presidio and spaCy, and the point of keeping redaction out of process is that
those must never enter the gateway's dependency tree or its lockfile. The
service narrows what it uses to a Protocol, so the coupling is one interface.
Its tests still run from the repo root via the root pytest config.

## Inside the gateway

| Path | What it owns |
|---|---|
| `routers/_metered.py` | resolve → reserve → record → settle, shared by all five `/v1` routes so the ordering cannot drift between them |
| `protocols.py` | per API surface: where usage, the served model and assistant text live in a frame |
| `accounting/cost.py` | the money arithmetic, and the two prompt-slice readers |
| `quota/engine.py` | admission; `counters.py` has the three counter stores |
| `access.py` | one predicate for "may this caller use this model" |
| `plugins/` | vendor quirks per provider — header names, cost units, router vs provider ([ADR 0032](adr/0032-provider-plugins.md)) |
| `pagination.py` | the listing envelope every management route returns |
| `types.py` | `Money` (Numeric, never float) and UTC-safe datetimes |
| `sse/` | event-boundary parsing and the stream transform pipeline |

## Deliberate boundaries

These are not accidents to tidy away; each one prevents a named failure.

- **chat-api imports nothing from the gateway.** It is a `/v1` client
  ([ADR 0040](adr/0040-bearer-tokens-on-v1.md)). The alternative — a shared
  in-process library — couples the two release cycles and lets the chat bypass
  the metering path it exists to exercise.
- **A plugin returns facts and never computes money.** There is deliberately
  nothing a plugin can return that would let it price a request. A provider's
  own cost report is data, not the charge — see
  [Accounting and quotas](accounting-and-quotas.md).
- **Redaction is out of process** ([ADR 0012](adr/0012-redaction-interface.md)).
  Detection is CPU-bound NER; in-process it would block the event loop for the
  length of every prompt. It is also ~90% of the CPU — see
  [Measured performance](performance.md).
- **Prices are append-only and effective-dated.** Editing a price would rewrite
  what past requests cost, so it cannot be done; a change adds a row.
- **Models only deactivate, never delete.** Deleting one would orphan the usage
  rows that reference it.
- **The base compose files bind `127.0.0.1`.** A public address is a deliberate
  act through the proxy overlay ([ADR 0035](adr/0035-public-tls-exposure.md)),
  and `scripts/test_public_tls_live.py` checks that nothing else leaked onto
  one.
- **Quotas are "all rules must pass"; redaction is "strictest answer wins".**
  Inverted on purpose: a permissive quota rule raises a ceiling, a permissive
  redaction rule removes protection — see
  [Redaction](redaction.md#adding-a-scope-can-only-tighten).
