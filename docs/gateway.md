# Gateway

OpenAI-compatible API gateway with per-user and per-group accounting, quotas,
per-group model availability and a pluggable redaction layer. The reference for
every decision behind it is the ADR index.

## Surfaces

| Path | Auth | Purpose |
|---|---|---|
| `POST /v1/chat/completions` | key or bearer | Proxy to the configured upstream, streaming and not |
| `POST /v1/responses` | key or bearer | OpenAI's Responses API. Server-side conversation state (`previous_response_id`, `store`) is refused |
| `POST /v1/messages` | key or bearer | Anthropic's Messages API, over the same chat models |
| `POST /v1/embeddings` | key or bearer | Embeddings, metered and redacted like a completion |
| `POST /v1/images/generations` | key or bearer | Image generation, billed per picture or per token depending on the model |
| `POST /v1/ocr` | key or bearer | Document extraction, metered per page. Two backends: an upstream OCR model, or this deployment's own extractor (ADR 0055) |
| `POST /v1/search` | key or bearer | Web search against a configured backend (Linkup, Exa, Jina), metered per call (ADR 0058). `POST /v1/search/{backend}` names one explicitly |
| `GET /v1/models` | key, bearer or none | Models the caller may use, by group or personal grant, with capabilities |
| `GET /v1/pystino/usage` | key or bearer | This caller's own spend, for a client that wants to show it without a console session |
| `GET /v1/billing/groups` | key or bearer | Which groups this caller may bill, and which one paid for this request (ADR 0061) |
| `/v1/files` | key or bearer | Upload, list, download and delete the only content this gateway stores (ADR 0062) |
| `/v1/vector_stores` | key or bearer | Knowledge bases: documents in, passages out, and who they are shared with (ADR 0062) |
| `GET /auth/login`, `/auth/callback` | — | OIDC authorization-code login (PKCE) |
| `/api/me/*` | session cookie | Identity, billing group, API keys, own usage and reports |
| `/api/admin/*` | session cookie + `is_admin` | Models, prices, group access, quotas, users, providers, redaction rules, reports |
| `GET /healthz`, `/readyz` | — | Liveness (no dependencies) and readiness (one DB round trip) |

Every metered `/v1` route shares one metering path — `routers/_metered.py` — so
resolve → reserve → record → settle cannot drift between surfaces (ADR 0030).
Seven routers import it today: chat, responses, messages, embeddings, images,
ocr and search.
Since ADR 0062 that path is also what **knowledge-base ingestion** bills
through, which is why `_metered.begin` takes `fx` and `session_factory` rather
than a `Request`: a background task has no request, and a second copy of the
money code would make indexing spend invisible to every report.

Two things this table used to get wrong, corrected here rather than quietly:
`/v1/ocr` was missing entirely, and `PUT /api/me/redaction` was listed but has
never existed — a user's own redaction policy is a scoped rule set by an
administrator (ADR 0038), not something a user can weaken.

**`/v1` and `/api` differ, and that is the whole asymmetry.** One dependency
(`deps.get_principal`) authenticates every `/v1` route, and it takes either
credential: a JWT in the `Authorization` header is verified against the issuer
that minted it (ADR 0040), anything else is looked up as an API key — so
"accepts a key" and "accepts a bearer" are the same list, not two. `/api`
accepts neither; it reads a session cookie and nothing else. That is why
`/v1/billing/groups`, `/v1/files` and `/v1/vector_stores` are on `/v1` at all:
a chat client holding a bearer token cannot reach a management route, so
anything it needs has to live where it can be reached.

The one credential-shaped distinction that survives is `x-bill-to` (ADR 0061):
a bearer caller may name the group to bill, an *issued* key may not — it
already carries its answer, and quietly overriding it is how somebody finds
out from an invoice. `GET /v1/models` is the other exception, in the opposite
direction: it answers unauthenticated too, brochure-level fields only, so a
client can build a catalogue before anyone has signed in (ADR 0081).

`GET /v1/models` reports each model's `kind`, `context_window`,
`max_input_tokens`, `max_output_tokens`, `input_modalities`,
`output_modalities` and `supported_features`, so a client
can pick a model that does tool calling or reads images without taking a 400 to
find out. These are non-standard fields, which OpenAI clients ignore
(ADR 0031). `max_input_tokens` is the one limit the gateway itself enforces:
a prompt estimated above it is a 400 `prompt_too_long` before any
reservation is made, and it is unset for most models — no catalogue reports
a provider's real input cap, so an operator records it by hand and null
means "no local limit", never zero.

Every management listing answers with `{items, total, limit, offset}` and takes
`?limit=&offset=` (ceiling 200; out of range is a 400, not a clamp). Users,
models and groups also take `?q=` for case-insensitive substring search.
Reports are aggregations, not listings, and return every row they summed
(ADR 0029).

There is **no HTML admin panel built by hand**: `/docs` is the operator API
console — Swagger, generated from the same schemas the endpoints validate
against. Sign in at `/auth/login` first so the session cookie travels with the
requests. The React console at `/console` is the human surface
(ADR 0022, ADR 0023).

## Two authentication schemes, on purpose

`/v1` is for programs and uses revocable API keys that carry a billing group;
`/api` is for humans and uses OIDC sessions (or local email + password,
ADR 0043).

- **API keys** are `gwk_...` secrets, shown once, stored as SHA-256 hashes —
  a slow KDF would buy nothing on 256 bits of entropy, but revocation must be
  instant (ADR 0010).
- **OIDC** works against any provider. The gateway itself still ships none —
  ADR 0044 stands — but a deployment need not go find one: the installer can
  bring up Authelia or Keycloak beside the gateway on the origin already
  published (ADR 0084), and the gateway can act as a minimal issuer for its
  own first-party clients (ADR 0068, [the house IdP](idp.md)). All three are
  the same `GATEWAY_OIDC__*` configuration from the gateway's side.
  Discovery is read once at
  startup, so changing any `GATEWAY_OIDC__*` value needs a restart. Users are
  keyed on `(issuer, subject)`: changing the issuer re-provisions every user as
  a new row with no memberships at their next login.
- **OIDC access tokens on `/v1`** are accepted when
  `GATEWAY_OIDC__ACCESS_TOKEN_AUDIENCE` names an audience — what lets a
  first-party client stream as the person typing, and what a device-flow
  bootstrap needs (ADR 0040).

## Providers and plugins

A provider record holds credentials (encrypted at rest,
ADR 0027) and a **plugin** that carries its
vendor knowledge: which header names it wants, what unit it reports cost in,
whether it is a provider or a router (ADR 0032).
In-tree plugins cover the generic OpenAI-compatible case (`generic`), OpenAI,
Anthropic, Mistral, Nebius, OpenRouter, Cortecs and Tensorix, this
deployment's own extractor, and the search backends DuckDuckGo, Exa, Jina and
Linkup. More can install via the `llmp.providers` entry point.

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
  routers/_metered.py  resolve, reserve, record, settle — shared by every
                    metered /v1 route so the ordering cannot drift between them
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

- **OIDC is fully verified against Keycloak 26.7 and Authelia 4.39.22**, the
  two versions the bundled overlays pin. Entra ID, Google and others differ in exactly
  the places ADR 0011 makes configurable — where
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
  cache loss (ADR 0006).
- **Load behaviour is reasoned, not measured.** The arithmetic in
  ADR 0004 says Python is not the constraint;
  the [measured numbers](performance.md) cover one small box. Profile before
  scaling.
