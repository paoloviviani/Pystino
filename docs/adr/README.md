# Architecture Decision Records

Each ADR states the context, the options considered, the decision and its
consequences. Where a decision rests on a version, a licence or a security advisory,
that evidence is written into the ADR with its date — so it can be re-checked rather
than re-researched, and so a reader months from now can tell what was true when the
choice was made.

**Everything here was verified against primary sources on 2026-08-14** (repositories,
licence files, release pages, advisories), not from memory. Anything that could not be
verified is marked as such in the ADR that depends on it.

## Foundational

| # | Decision |
|---|---|
| [0001](0001-licensing.md) | EUPL-1.2 for first-party code; inbound dependency licence policy |
| [0002](0002-monorepo-tooling.md) | Monorepo layout; uv + pnpm workspaces, no orchestrator |
| [0003](0003-build-vs-adopt-gateway.md) | Build the gateway rather than adopt LiteLLM |

## Gateway — implemented in Phase 1

| # | Decision |
|---|---|
| [0004](0004-gateway-runtime.md) | Python 3.13 + FastAPI, and why streaming load is not the constraint |
| [0005](0005-persistence.md) | PostgreSQL as ledger of record; SQLAlchemy 2.0 async; Alembic |
| [0006](0006-counter-store.md) | Valkey, not Redis, and why counters are never authoritative |
| [0007](0007-sse-streaming.md) | The SSE architecture: event boundaries, forced usage, disconnect |
| [0008](0008-accounting-model.md) | The accounting model, and never reporting zero |
| [0009](0009-quota-model.md) | Quota rules, rolling windows, reserve-then-settle, overrun policy |
| [0010](0010-api-keys.md) | API key format, SHA-256 rather than a slow KDF, revocation |
| [0011](0011-oidc-integration.md) | OIDC for the management API; the configurable group claim |
| [0012](0012-redaction-interface.md) | The redaction interface and deterministic placeholders |
| [0013](0013-upstream-http-client.md) | httpx, its timeout traps, and a maintenance risk |
| [0014](0014-model-catalogue-and-pricing.md) | The model catalogue and the Cortecs pricing importer |
| [0022](0022-administration-surface.md) | Administration is an API; admin follows an IdP group (UI stance superseded by 0023) |
| [0023](0023-admin-console.md) | The console: a SPA served by the gateway, optional at build time |
| [0024](0024-billing-periods.md) | Billing periods are calendar-based, and are not quota windows |

## Decisions recorded now, implemented later

| # | Decision | Phase |
|---|---|---|
| [0015](0015-frontend-stack.md) | Next.js 16 + assistant-ui + AI SDK v6 | 2 |
| [0016](0016-pwa.md) | Serwist (next-pwa is archived) | 2 |
| [0017](0017-desktop-shell.md) | Tauri v2 (v3 is alpha only) | 4 |
| [0018](0018-vector-store.md) | pgvector first, Qdrant behind an interface | 3 |
| [0019](0019-document-conversion.md) | Document conversion behind an HTTP endpoint; Docling as reference | 3 |
| [0020](0020-embeddings-and-reranking.md) | Configurable OpenAI-compatible endpoints, no bundled model | 3 |
| [0021](0021-code-sandbox.md) | gVisor first; WASM rejected as the security boundary | 4+ |

## Where an assumption was argued against

Worth knowing which of the starting assumptions changed, and why:

- **Redis → Valkey** ([0006](0006-counter-store.md)) — licence history and distro default.
- **Qdrant → pgvector first** ([0018](0018-vector-store.md)) — one less stateful service.
- **Python 3.12 → 3.13** ([0004](0004-gateway-runtime.md)) — and *not* 3.14.
- **`packages/shared` → also `packages/shared-py`** ([0002](0002-monorepo-tooling.md)) —
  two languages cannot share one package.

Everything else in the brief's assumptions was validated as correct.

## Plans

- [phase-2-plan.md](../phase-2-plan.md) — redaction and the console, sequenced.

## Writing a new one

Copy the shape of an existing ADR. Number sequentially, never renumber, never delete —
supersede instead, and say which ADR supersedes which. An ADR whose decision was reversed
is more useful than one that was quietly removed, because the reversal is the thing worth
knowing.
