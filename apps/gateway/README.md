# Gateway

OpenAI-compatible API gateway with per-user and per-group accounting, quotas,
per-group model availability and a pluggable redaction layer.

## Surfaces

| Path | Auth | Purpose |
|---|---|---|
| `POST /v1/chat/completions` | API key | Proxy to the configured upstream, streaming and not |
| `GET /v1/models` | API key | Models the caller's groups may use, from our catalogue |
| `GET /auth/login`, `/auth/callback` | — | OIDC authorization-code login |
| `GET /api/me` | session cookie | Identity, groups, default billing group |
| `PUT /api/me/default-billing-group` | session cookie | Users change their own billing group |
| `GET|POST|DELETE /api/me/keys` | session cookie | Mint and revoke API keys |
| `GET /api/me/usage` | session cookie | Own spend over a rolling window |
| `GET /healthz`, `/readyz` | — | Liveness (no dependencies) and readiness |

Two authentication schemes on purpose: `/v1` is for programs and uses revocable
API keys that carry a billing group; `/api` is for humans and uses OIDC.

## Layout

```
src/gateway/
  config.py         all configuration; nothing else reads the environment
  models.py         SQLAlchemy ORM — the usage ledger lives here
  types.py          Money (Numeric, never float) and UTC-safe datetimes
  security.py       API key generation, SHA-256 hashing, verification
  oidc.py           discovery, PKCE, ID token validation, group claim mapping
  upstream.py       the provider client, with read timeout deliberately None
  sse/              event-boundary parsing and the transform pipeline
  accounting/       token counts, cost, and the ledger writer
  quota/            rolling windows, counter stores, reserve-then-settle
  redaction/        the interface, the buffering rewriter, and the no-op engine
  routers/          HTTP endpoints
```

## Running

```bash
uv sync
# from the repository root, with PostgreSQL and Valkey available:
uv run alembic -c apps/gateway/alembic.ini upgrade head
uv run gateway seed          # prints an API key, shown once
uv run gateway serve --reload
```

Or `docker compose -f deploy/compose/docker-compose.yml up` for the whole stack.

## Testing

```bash
uv run pytest              # 311 tests, no services needed
../../scripts/smoke_test.sh  # end-to-end over real HTTP
```

Tests run against SQLite by default so they need no services. Money arithmetic is
tested as pure `Decimal` functions, because SQLite cannot store `Numeric`
natively; the schema itself is exercised against PostgreSQL by the migration in
CI and by `docker compose`.

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

## Known gaps

Stated plainly, so none of these is a surprise later.

**Not tested, and needs verifying against the real thing**

- **The full OIDC browser flow.** It needs a live identity provider. Everything on our
  side of the redirect is tested — claim resolution (including dotted and namespaced
  paths), group normalisation, provisioning, membership reconciliation, session tokens,
  cookie type confusion. The redirect round trip itself is not. Verify against the actual
  IdP before relying on it. ([ADR 0011](../../docs/adr/0011-oidc-integration.md))
- **Load behaviour.** Nothing here has been run under concurrency at the few-hundred
  simultaneous streams the design argues about. The arithmetic in
  [ADR 0004](../../docs/adr/0004-gateway-runtime.md) says Python is not the constraint;
  that is reasoning, not a measurement. Profile before scaling.
- **The Cortecs catalogue envelope.** The pricing *fields* were verified against the
  documentation; the JSON shape wrapping the model list was not seen live. The parser
  accepts several plausible shapes and reports what it cannot read.
  ([ADR 0014](../../docs/adr/0014-model-catalogue-and-pricing.md))

**Verified against a real running stack**

`docker compose up` was built and run: PostgreSQL 18, Valkey, migrations and the gateway
all healthy, real requests served, quotas enforced, and the database fallback confirmed by
stopping Valkey mid-workload. See [ADR 0005](../../docs/adr/0005-persistence.md) and
[ADR 0006](../../docs/adr/0006-counter-store.md) for what was checked and the two problems
it found.

**Deliberately not implemented**

- `POST /v1/embeddings`. Needed in Phase 3 so indexing spend is accounted for rather than
  invisible. ([ADR 0020](../../docs/adr/0020-embeddings-and-reranking.md))
- Device authorization flow endpoints, which the `opencode` bootstrap needs. See
  `scripts/README.md`.
- Admin write endpoints for models, prices, group access and limit rules. Currently those
  are set via `gateway seed`, the pricing importer, or SQL. A real deployment will want
  them.
- Retrying an upstream request without `stream_options` when a provider rejects unknown
  parameters. Left out rather than shipped untested. ([ADR 0013](../../docs/adr/0013-upstream-http-client.md))
- A hard mid-stream quota ceiling via `max_tokens` clamping. The chosen policy admits the
  request that crosses the limit. ([ADR 0009](../../docs/adr/0009-quota-model.md))

**Known operational sharp edges**

- **GDPR erasure is incomplete.** Identity foreign keys use `ON DELETE SET NULL` so the
  financial ledger survives deleting a user — but `assistant_text` may itself contain
  personal data and is *not* cleared by that. An erasure procedure has to blank it
  explicitly, and no such procedure exists yet.
- **The redaction HMAC key must be backed up with the transcripts it labelled.** Losing or
  rotating it breaks cross-turn placeholder consistency for existing conversations.
- **Counters rebuild from the ledger only at startup.** If Valkey is wiped while the
  gateway keeps running, the cache reports zero and quotas are briefly too permissive until
  the gateway restarts. An empty cache is not a failed read, so nothing detects it at
  runtime. Restart the gateway after any cache loss.
  ([ADR 0006](../../docs/adr/0006-counter-store.md))
- Rules are loaded per request. Cacheable if it ever shows up in a profile.
