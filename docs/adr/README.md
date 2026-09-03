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
| [0024](0024-billing-periods.md) | Billing periods are calendar-based, and are not rolling quota windows |
| [0025](0025-quota-model-v2.md) | The quota model: stacked, multi-granularity, resettable |
| [0026](0026-pluggable-detection.md) | Detection is a plugin; the Italian NER model is a build flag |
| [0027](0027-inference-providers.md) | Inference providers as configurable records, with keys encrypted at rest |
| [0028](0028-embeddings-and-served-model.md) | Embeddings, and recording what actually served a request |
| [0029](0029-pagination.md) | Pagination on the management listings, and server-side search |
| [0030](0030-more-surfaces.md) | Responses, Anthropic Messages and image generation, over one metering path |
| [0031](0031-model-capabilities.md) | Model capabilities: imported from the catalogue, exposed to callers, editable |
| [0032](0032-provider-plugins.md) | Providers versus routers, and a plugin seam that returns facts rather than computing money |
| [0033](0033-redaction-engine-selection.md) | The redaction engine becomes an admin decision, on an append-only record |
| [0034](0034-bauhaus-design-language.md) | Bauhaus as the design language, and a self-hosted geometric sans |
| [0035](0035-public-tls-exposure.md) | A TLS reverse proxy, and the four holes that made "never on a routable address" the rule |
| [0036](0036-model-page.md) | A model's price belongs on the model's page, not on a screen of its own |
| [0037](0037-redaction-policy.md) | One engine, and a per-entity redaction policy an admin can set |
| [0038](0038-scoped-redaction.md) | Redaction scoped per provider, model, group, user and key, where a scope can only tighten |
| [0039](0039-redaction-rules-only.md) | There is no deployment policy, only rules; the catch-all is one of them, and the default protects nothing |
| [0040](0040-bearer-tokens-on-v1.md) | OIDC access tokens as a `/v1` credential, gated by audience |
| [0042](0042-material-design-3.md) | Material Design 3, tuned for reading (supersedes 0034) |
| [0043](0043-local-authentication.md) | Optional local email + password sign-in beside OIDC; one session, two doors |
| [0044](0044-keycloak-removed.md) | Keycloak leaves the stack; OIDC against any provider, local login as the default way in |
| [0045](0045-public-models.md) | Public models: any authenticated caller may use one, billed to their own group |
| [0046](0046-local-api-credentials.md) | The local door issues `/v1` credentials: the gateway as issuer for its own accounts |
| [0047](0047-tailwind-base-ui-modern-refresh.md) | The UI restyles on Tailwind v4 + Base UI, with a dark theme (supersedes the no-Tailwind stance of 0023/0042) |
| [0048](0048-oidc-policy-configuration.md) | Users are created and deleted in the console; the OIDC identity policy is a runtime decision (amends 0011, 0022, 0043) |
| [0049](0049-password-reset.md) | Self-service password reset by email, opt-in and environment-configured (amends 0043) |
| [0050](0050-group-management.md) | Groups are created, deleted and given members in the console; manual groups only (bounds 0048) |
| [0051](0051-settings-identity-and-email.md) | The Settings screen: identity providers (multiple) and the mail server as console decisions (supersedes 0048's connection boundary) |
| [0052](0052-quota-notifications.md) | Quota notifications: the rule is the administrator's, the threshold is the user's |
| [0053](0053-model-pricing-sources.md) | Where model prices come from: native catalogues, the LiteLLM file, manual entry |
| [0054](0054-currency-conversion.md) | USD-priced models convert at the decision and aggregation points; breakdowns stay native (supersedes 0014's currency refusal) |

**0015, 0016 and 0041 — the chat frontend decisions — moved to the `chat`
branch with the chat application itself** (A Vite SPA on `packages/ui` with
assistant-ui; Next.js 16 + AI SDK superseded by 0041; the PWA). They are
recorded there, with that branch's copy of this index.

## Decisions recorded now, implemented later

| # | Decision | Phase |
|---|---|---|
| [0017](0017-desktop-shell.md) | Tauri v2 (v3 is alpha only) | 4 |
| [0018](0018-vector-store.md) | pgvector first, Qdrant behind an interface | 3 |
| [0019](0019-document-conversion.md) | Document conversion behind an HTTP endpoint; Docling as reference | 3 |
| [0020](0020-embeddings-and-reranking.md) | Configurable OpenAI-compatible endpoints, no bundled model | 3 |
| [0021](0021-code-sandbox.md) | gVisor first; WASM rejected as the security boundary | 4+ |

## Where an assumption was argued against

Worth knowing which of the starting assumptions changed, and why:

- **Bauhaus → Material Design 3** ([0042](0042-material-design-3.md)) — an
  authentic poster idiom, applied to pages of figures, was hard to read.
- **Redis → Valkey** ([0006](0006-counter-store.md)) — licence history and distro default.
- **Qdrant → pgvector first** ([0018](0018-vector-store.md)) — one less stateful service.
- **Python 3.12 → 3.13** ([0004](0004-gateway-runtime.md)) — and *not* 3.14.
- **`packages/shared` → also `packages/shared-py`** ([0002](0002-monorepo-tooling.md)) —
  two languages cannot share one package.

Everything else in the brief's assumptions was validated as correct.

## Plans

- [phase-2-plan.md](../phase-2-plan.md) — redaction and the console, sequenced.
- The chat application's plan (`phase-3-plan.md`) lives on the `chat` branch.

## Writing a new one

Copy the shape of an existing ADR. Number sequentially, never renumber, never delete —
supersede instead, and say which ADR supersedes which. An ADR whose decision was reversed
is more useful than one that was quietly removed, because the reversal is the thing worth
knowing.
