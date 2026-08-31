# Gateway

OpenAI-compatible API gateway with per-user and per-group accounting, quotas,
per-group model availability and a pluggable redaction layer. The reference for
every decision behind it is the [ADR index](adr/README.md).

## Surfaces

| Path | Auth | Purpose |
|---|---|---|
| `POST /v1/chat/completions` | API key | Proxy to the configured upstream, streaming and not |
| `POST /v1/responses` | API key | OpenAI's Responses API. Server-side conversation state (`previous_response_id`, `store`) is refused |
| `POST /v1/messages` | API key | Anthropic's Messages API, over the same chat models |
| `POST /v1/embeddings` | API key | Embeddings, metered and redacted like a completion |
| `POST /v1/images/generations` | API key | Image generation, billed per picture or per token depending on the model |
| `GET /v1/models` | API key | Models the caller may use, by group or personal grant, with capabilities |
| `GET /auth/login`, `/auth/callback` | — | OIDC authorization-code login (PKCE) |
| `/api/me/*` | session cookie | Identity, billing group, API keys, own usage and reports |
| `PUT /api/me/redaction` | session cookie | A user's own redaction policy — may not weaken the admin's floor |
| `/api/admin/*` | session cookie + `is_admin` | Models, prices, group access, quotas, users, providers, redaction rules, reports |
| `GET /healthz`, `/readyz` | — | Liveness (no dependencies) and readiness (one DB round trip) |

All five `/v1` request routes share one metering path — `routers/_metered.py` —
so resolve → reserve → record → settle cannot drift between surfaces
([ADR 0030](adr/0030-more-surfaces.md)).

`GET /v1/models` reports each model's `kind`, `context_window`,
`input_modalities`, `output_modalities` and `supported_features`, so a client
can pick a model that does tool calling or reads images without taking a 400 to
find out. These are non-standard fields, which OpenAI clients ignore
([ADR 0031](adr/0031-model-capabilities.md)).

Every management listing answers with `{items, total, limit, offset}` and takes
`?limit=&offset=` (ceiling 200; out of range is a 400, not a clamp). Users,
models and groups also take `?q=` for case-insensitive substring search.
Reports are aggregations, not listings, and return every row they summed
([ADR 0029](adr/0029-pagination.md)).

There is **no HTML admin panel built by hand**: `/docs` is the operator API
console — Swagger, generated from the same schemas the endpoints validate
against. Sign in at `/auth/login` first so the session cookie travels with the
requests. The React console at `/console` is the human surface
([ADR 0022](adr/0022-administration-surface.md), [ADR 0023](adr/0023-admin-console.md)).

## Two authentication schemes, on purpose

`/v1` is for programs and uses revocable API keys that carry a billing group;
`/api` is for humans and uses OIDC sessions (or local email + password,
[ADR 0043](adr/0043-local-authentication.md)).

- **API keys** are `gwk_...` secrets, shown once, stored as SHA-256 hashes —
  a slow KDF would buy nothing on 256 bits of entropy, but revocation must be
  instant ([ADR 0010](adr/0010-api-keys.md)).
- **OIDC** works against any provider; there is no bundled identity provider
  ([ADR 0044](adr/0044-keycloak-removed.md)). Discovery is read once at
  startup, so changing any `GATEWAY_OIDC__*` value needs a restart. Users are
  keyed on `(issuer, subject)`: changing the issuer re-provisions every user as
  a new row with no memberships at their next login.
- **OIDC access tokens on `/v1`** are accepted when
  `GATEWAY_OIDC__ACCESS_TOKEN_AUDIENCE` names an audience — this is what lets
  chat-api stream as the person typing, and what a device-flow bootstrap needs
  ([ADR 0040](adr/0040-bearer-tokens-on-v1.md)).

## Providers and plugins

A provider record holds credentials (encrypted at rest,
[ADR 0027](adr/0027-inference-providers.md)) and a **plugin** that carries its
vendor knowledge: which header names it wants, what unit it reports cost in,
whether it is a provider or a router ([ADR 0032](adr/0032-provider-plugins.md)).
In-tree plugins cover the generic OpenAI-compatible case, Anthropic and
Cortecs; more can install via the `llmp.providers` entry point.

Two consequences worth knowing:

- a per-row knob became per-plugin behaviour, so two providers of the same type
  that need different answers need two plugins;
- a plugin that does not read a cost reports none, which is the safe default —
  see [Accounting and quotas](accounting-and-quotas.md#three-cost-figures-one-meaning-each).

## Things that are easy to get wrong, and where they are handled

| Trap | Where |
|---|---|
| Streamed responses carry no token counts unless `stream_options.include_usage` is forced | `routers/chat.py:build_upstream_payload` |
| The usage frame must be stripped if the client did not ask for it — but only the *usage-only* frame may be dropped whole | `routers/chat.py:usage_visibility_stage` |
| SSE must be parsed at `\n\n` boundaries, not network chunk boundaries | `sse/parser.py` |
| A trailing `\r` split across chunks is ambiguous and must be held back | `sse/parser.py:_pop_line` |
| httpx's 5s default read timeout kills long streams | `upstream.py:build_http_client` |
| Client disconnect must close the upstream *and* still record accrued usage | `routers/chat.py:_spawn_finalisation` |
| Assistant output must be persisted as it streams, not only at the end | `accounting/recorder.py:maybe_flush` |
| An absent usage frame must not be recorded as zero spend | `accounting/tokens.py` |
| Concurrent requests must not each pass the same under-limit check | `quota/engine.py:check_and_reserve` |
| Money must never touch a float, including inside the counter store | `types.py`, `quota/counters.py` |
| A wiped counter cache reports zero spend, not an error, so every group gets a fresh budget | `quota/engine.py:rebuild_if_cache_is_cold` |
| Editing a price would rewrite what past requests cost, so prices are append-only | `routers/admin.py:create_price` |
| Deleting a model orphans the usage rows that reference it, so models only deactivate | `routers/admin.py` (no DELETE route) |

## Layout

```
src/gateway/
  config.py         all configuration; nothing else reads the environment
  models.py         SQLAlchemy ORM — the usage ledger lives here
  pagination.py     the listing envelope, its bounds and the count query
  protocols.py      per API surface: where usage, the served model and the
                    assistant text live in a response frame
  routers/_metered.py  resolve, reserve, record, settle — shared by all five
                    /v1 routes so the ordering cannot drift between them
  types.py          Money (Numeric, never float) and UTC-safe datetimes
  security.py       API key generation, SHA-256 hashing, verification
  oidc.py           discovery, PKCE, ID token validation, group claim mapping
  upstream.py       the provider client, with read timeout deliberately None
  sse/              event-boundary parsing and the transform pipeline
  accounting/       token counts, cost, and the ledger writer
  quota/            rolling windows, counter stores, reserve-then-settle
  redaction/        the interface, the buffering rewriter, the resolver
  plugins/          provider plugins: facts about a counterparty, no arithmetic
  routers/          HTTP endpoints: /v1, /auth, /api/me, /api/admin, /console
```

## Known gaps

Stated plainly, so none of them is a surprise later.

- **OIDC is only fully verified against Keycloak 26.7** (`scripts/test_oidc_flow.py`,
  since removed from the stack). Entra ID, Google and others differ in exactly
  the places [ADR 0011](adr/0011-oidc-integration.md) makes configurable — where
  groups live, whether they appear in the ID token at all, how they are named.
  Test any new provider against your instance before relying on it; see the
  [OIDC guide](oidc-generic-provider.md) for the checklist.
- **GDPR erasure is incomplete.** Identity foreign keys use
  `ON DELETE SET NULL` so the financial ledger survives deleting a user — but
  `assistant_text` may itself contain personal data and is *not* cleared by
  that. An erasure procedure has to blank it explicitly; none exists yet.
- **The redaction HMAC key must be backed up with the transcripts it
  labelled.** Losing or rotating it breaks cross-turn placeholder consistency
  for existing conversations.
- **Counters rebuild from the ledger only at startup.** If Valkey is wiped
  while the gateway keeps running, the cache reports zero and quotas are
  briefly too permissive until the gateway restarts. An empty cache is not a
  failed read, so nothing detects it at runtime. Restart the gateway after any
  cache loss ([ADR 0006](adr/0006-counter-store.md)).
- **Load behaviour is reasoned, not measured.** The arithmetic in
  [ADR 0004](adr/0004-gateway-runtime.md) says Python is not the constraint;
  the [measured numbers](performance.md) cover one small box. Profile before
  scaling.
