# Design decisions

The decision record lives in the
[ai-stack](https://example.invalid/viviani/ai-stack) repository, not
here. It is one numbered series covering the gateway, the chat application, the
RAG pipeline and three successive design languages — an *endeavour* rather than
a component — and a numbered series cannot be cut in half without renumbering,
which its own index forbids.

Code in this repository cites decisions **by number**: `(ADR 0032)`. That
reference resolves wherever the file lives, which is the whole reason the
numbers exist. This table is the mapping, and it is generated — if it drifts
from ai-stack, ai-stack is right.

[The index over there](https://example.invalid/viviani/ai-stack/-/blob/main/docs/adr/README.md) groups these by area and says which
supersede which; read it before adding one. The next number is the next number
**there**, whichever repository the work lands in.

| | Decision |
|---|---|
| [0001](https://example.invalid/viviani/ai-stack/-/blob/main/docs/adr/0001-licensing.md) | EUPL-1.2 for first-party code, and the dependency licence policy |
| [0002](https://example.invalid/viviani/ai-stack/-/blob/main/docs/adr/0002-monorepo-tooling.md) | Monorepo layout and tooling |
| [0003](https://example.invalid/viviani/ai-stack/-/blob/main/docs/adr/0003-build-vs-adopt-gateway.md) | Build the gateway rather than adopt LiteLLM |
| [0004](https://example.invalid/viviani/ai-stack/-/blob/main/docs/adr/0004-gateway-runtime.md) | Python + FastAPI for the gateway, and how to keep it fast |
| [0005](https://example.invalid/viviani/ai-stack/-/blob/main/docs/adr/0005-persistence.md) | PostgreSQL as the ledger of record, SQLAlchemy 2.0 async, Alembic |
| [0006](https://example.invalid/viviani/ai-stack/-/blob/main/docs/adr/0006-counter-store.md) | Valkey, not Redis, for quota counters |
| [0007](https://example.invalid/viviani/ai-stack/-/blob/main/docs/adr/0007-sse-streaming.md) | The SSE streaming architecture |
| [0008](https://example.invalid/viviani/ai-stack/-/blob/main/docs/adr/0008-accounting-model.md) | The accounting model |
| [0009](https://example.invalid/viviani/ai-stack/-/blob/main/docs/adr/0009-quota-model.md) | Quota rules, rolling windows, and the mid-stream overrun policy |
| [0010](https://example.invalid/viviani/ai-stack/-/blob/main/docs/adr/0010-api-keys.md) | API key format, hashing and revocation |
| [0011](https://example.invalid/viviani/ai-stack/-/blob/main/docs/adr/0011-oidc-integration.md) | OIDC for the management API, and the configurable group claim |
| [0012](https://example.invalid/viviani/ai-stack/-/blob/main/docs/adr/0012-redaction-interface.md) | The redaction interface and deterministic placeholders |
| [0013](https://example.invalid/viviani/ai-stack/-/blob/main/docs/adr/0013-upstream-http-client.md) | The upstream HTTP client (httpx), and a maintenance risk to watch |
| [0014](https://example.invalid/viviani/ai-stack/-/blob/main/docs/adr/0014-model-catalogue-and-pricing.md) | The model catalogue and the Cortecs pricing importer |
| [0015](https://example.invalid/viviani/ai-stack/-/blob/main/docs/adr/0015-frontend-stack.md) | Frontend stack: Next.js 16, assistant-ui, AI SDK v6 |
| [0016](https://example.invalid/viviani/ai-stack/-/blob/main/docs/adr/0016-pwa.md) | PWA via Serwist |
| [0017](https://example.invalid/viviani/ai-stack/-/blob/main/docs/adr/0017-desktop-shell.md) | Desktop shell: Tauri v2 |
| [0018](https://example.invalid/viviani/ai-stack/-/blob/main/docs/adr/0018-vector-store.md) | Vector store: pgvector first, Qdrant behind an interface |
| [0019](https://example.invalid/viviani/ai-stack/-/blob/main/docs/adr/0019-document-conversion.md) | Document conversion / OCR behind a configurable HTTP endpoint |
| [0020](https://example.invalid/viviani/ai-stack/-/blob/main/docs/adr/0020-embeddings-and-reranking.md) | Embeddings and reranking via configurable OpenAI-compatible endpoints |
| [0021](https://example.invalid/viviani/ai-stack/-/blob/main/docs/adr/0021-code-sandbox.md) | Code execution sandbox: gVisor first, WASM rejected as the boundary |
| [0022](https://example.invalid/viviani/ai-stack/-/blob/main/docs/adr/0022-administration-surface.md) | Administration is an API, and admin follows an IdP group |
| [0023](https://example.invalid/viviani/ai-stack/-/blob/main/docs/adr/0023-admin-console.md) | The console: a SPA served by the gateway, optional at build time |
| [0024](https://example.invalid/viviani/ai-stack/-/blob/main/docs/adr/0024-billing-periods.md) | Billing periods are calendar-based, and are not quota windows |
| [0025](https://example.invalid/viviani/ai-stack/-/blob/main/docs/adr/0025-quota-model-v2.md) | The quota model: stacked, multi-granularity, resettable |
| [0026](https://example.invalid/viviani/ai-stack/-/blob/main/docs/adr/0026-pluggable-detection.md) | Detection is a plugin, and the Italian model is a build flag |
| [0027](https://example.invalid/viviani/ai-stack/-/blob/main/docs/adr/0027-inference-providers.md) | Inference providers as configurable records |
| [0028](https://example.invalid/viviani/ai-stack/-/blob/main/docs/adr/0028-embeddings-and-served-model.md) | Embeddings, and recording what actually served a request |
| [0029](https://example.invalid/viviani/ai-stack/-/blob/main/docs/adr/0029-pagination.md) | Pagination on the management listings |
| [0030](https://example.invalid/viviani/ai-stack/-/blob/main/docs/adr/0030-more-surfaces.md) | Responses, Anthropic Messages and image generation |
| [0031](https://example.invalid/viviani/ai-stack/-/blob/main/docs/adr/0031-model-capabilities.md) | Model capabilities |
| [0032](https://example.invalid/viviani/ai-stack/-/blob/main/docs/adr/0032-provider-plugins.md) | Providers, routers, and a plugin seam for both |
| [0033](https://example.invalid/viviani/ai-stack/-/blob/main/docs/adr/0033-redaction-engine-selection.md) | The redaction engine becomes an admin decision, on the record |
| [0034](https://example.invalid/viviani/ai-stack/-/blob/main/docs/adr/0034-bauhaus-design-language.md) | Bauhaus as the design language, and a self-hosted geometric sans |
| [0035](https://example.invalid/viviani/ai-stack/-/blob/main/docs/adr/0035-public-tls-exposure.md) | Serving the stack on a real address, behind a TLS proxy |
| [0036](https://example.invalid/viviani/ai-stack/-/blob/main/docs/adr/0036-model-page.md) | A model's price belongs on the model's page |
| [0037](https://example.invalid/viviani/ai-stack/-/blob/main/docs/adr/0037-redaction-policy.md) | One engine, and a per-entity policy an admin can set |
| [0038](https://example.invalid/viviani/ai-stack/-/blob/main/docs/adr/0038-scoped-redaction.md) | Redaction scoped per provider, model, group, user and key |
| [0039](https://example.invalid/viviani/ai-stack/-/blob/main/docs/adr/0039-redaction-rules-only.md) | There is no deployment policy, only rules; the catch-all is one of them |
| [0040](https://example.invalid/viviani/ai-stack/-/blob/main/docs/adr/0040-bearer-tokens-on-v1.md) | OIDC access tokens as a `/v1` credential, gated by audience |
| [0041](https://example.invalid/viviani/ai-stack/-/blob/main/docs/adr/0041-chat-frontend-stack.md) | A Vite SPA on `packages/ui`, with assistant-ui for the thread |
| [0042](https://example.invalid/viviani/ai-stack/-/blob/main/docs/adr/0042-material-design-3.md) | Material Design 3, tuned for reading |
| [0043](https://example.invalid/viviani/ai-stack/-/blob/main/docs/adr/0043-local-authentication.md) | Local authentication: email + password beside OIDC |
| [0044](https://example.invalid/viviani/ai-stack/-/blob/main/docs/adr/0044-keycloak-removed.md) | Keycloak leaves the stack; OIDC becomes generic-provider-only |
| [0045](https://example.invalid/viviani/ai-stack/-/blob/main/docs/adr/0045-public-models.md) | Public models: access for every authenticated caller |
| [0046](https://example.invalid/viviani/ai-stack/-/blob/main/docs/adr/0046-local-api-credentials.md) | The local door issues `/v1` credentials: the gateway as issuer for its own accounts |
| [0047](https://example.invalid/viviani/ai-stack/-/blob/main/docs/adr/0047-tailwind-base-ui-modern-refresh.md) | A modern design language on Tailwind CSS v4 and Base UI |
| [0048](https://example.invalid/viviani/ai-stack/-/blob/main/docs/adr/0048-oidc-policy-configuration.md) | Users are created and deleted in the console; OIDC policy is a decision, not an env var |
| [0049](https://example.invalid/viviani/ai-stack/-/blob/main/docs/adr/0049-password-reset.md) | Self-service password reset by email, opt-in and environment-configured |
| [0050](https://example.invalid/viviani/ai-stack/-/blob/main/docs/adr/0050-group-management.md) | Groups are created, deleted and given members in the console; manual groups only |
| [0051](https://example.invalid/viviani/ai-stack/-/blob/main/docs/adr/0051-settings-identity-and-email.md) | The Settings screen: identity providers and the mail server as console decisions |
| [0052](https://example.invalid/viviani/ai-stack/-/blob/main/docs/adr/0052-quota-notifications.md) | Quota notifications: the rule is the administrator's, the threshold is the user's |
| [0053](https://example.invalid/viviani/ai-stack/-/blob/main/docs/adr/0053-model-pricing-sources.md) | Where model prices come from |
| [0054](https://example.invalid/viviani/ai-stack/-/blob/main/docs/adr/0054-currency-conversion.md) | Currency conversion at the decision and aggregation points |
| [0055](https://example.invalid/viviani/ai-stack/-/blob/main/docs/adr/0055-ocr-surface.md) | Document extraction as a `/v1` surface, with a local backend |
| [0056](https://example.invalid/viviani/ai-stack/-/blob/main/docs/adr/0056-account-linking.md) | An identity provider may adopt the local account with the same verified address |
| [0057](https://example.invalid/viviani/ai-stack/-/blob/main/docs/adr/0057-group-ownership-and-sync.md) | A directory owns the memberships it granted, and nothing else |
| [0058](https://example.invalid/viviani/ai-stack/-/blob/main/docs/adr/0058-per-search-pricing.md) | Provider-side web search is a billable unit, priced per search |
| [0059](https://example.invalid/viviani/ai-stack/-/blob/main/docs/adr/0059-citation-offsets.md) | Restoring a placeholder moves the citations that point past it |
| [0060](https://example.invalid/viviani/ai-stack/-/blob/main/docs/adr/0060-settle-after-the-response.md) | The final ledger write happens after the answer is sent |
