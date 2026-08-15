# Gateway

OpenAI-compatible API gateway with per-user and per-group accounting, quotas,
per-group model availability and a pluggable redaction layer.

## Surfaces

| Path | Auth | Purpose |
|---|---|---|
| `POST /v1/chat/completions` | API key | Proxy to the configured upstream, streaming and not |
| `POST /v1/embeddings` | API key | Embeddings, metered and redacted like a completion |
| `GET /v1/models` | API key | Models the caller may use, by group or personal grant |
| `GET /auth/login`, `/auth/callback` | — | OIDC authorization-code login |
| `GET /api/me` | session cookie | Identity, groups, default billing group |
| `PUT /api/me/default-billing-group` | session cookie | Users change their own billing group |
| `GET|POST|DELETE /api/me/keys` | session cookie | Mint and revoke API keys |
| `GET /api/me/usage` | session cookie | Own spend over a rolling window |
| `GET /api/me/reports/usage[.csv]` | session cookie | Own spend over a calendar period, by model, day, group or key |
| `/api/admin/*` | session cookie + `is_admin` | Models, prices, group access, limits, users, usage |
| `GET /api/admin/models/discover` | session cookie + `is_admin` | What the provider offers that we do not carry, and drift the other way |
| `POST /api/admin/models/import` | session cookie + `is_admin` | Adopt named upstream models with their published prices |
| `GET /api/admin/reports/usage[.csv]` | session cookie + `is_admin` | Chargeback reporting for a calendar period, `group_by` group/user/model/api_key/day/total |
| `POST /api/admin/limits/{id}/reset` | session cookie + `is_admin` | Set a quota's consumption to zero (reason required); leaves billing untouched |
| `GET /healthz`, `/readyz` | — | Liveness (no dependencies) and readiness |

There is **no HTML admin panel**. `/docs` is the operator console — Swagger, generated
from the same schemas the endpoints validate against. Sign in at `/auth/login` first, so
the session cookie travels with the requests. See
[ADR 0022](../../docs/adr/0022-administration-surface.md).

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
  routers/          HTTP endpoints: /v1, /auth, /api/me (me.py), /api/admin (admin.py)
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
uv run pytest                  # 343 tests, no services needed
../../scripts/smoke_test.sh    # end-to-end over real HTTP, no Docker
../../scripts/test_oidc_flow.py  # full OIDC login against Keycloak (needs the stack up)
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
| Editing a price would rewrite what past requests cost, so prices are append-only | `routers/admin.py:create_price` |
| Deleting a model orphans the usage rows that reference it, so models only deactivate | `routers/admin.py` (no DELETE route) |

## Known gaps

Stated plainly, so none of these is a surprise later.

**Not tested, and needs verifying against the real thing**

- **OIDC against providers other than Keycloak.** The full redirect flow is now verified
  end to end against Keycloak 26.7 (`scripts/test_oidc_flow.py`), but Entra ID, Google and
  others differ in exactly the places ADR 0011 makes configurable: where groups live,
  whether they appear in the ID token at all, how they are named. Re-run the same checks
  against the real provider before going live.
  ([ADR 0011](../../docs/adr/0011-oidc-integration.md))
- **Load behaviour.** Nothing here has been run under concurrency at the few-hundred
  simultaneous streams the design argues about. The arithmetic in
  [ADR 0004](../../docs/adr/0004-gateway-runtime.md) says Python is not the constraint;
  that is reasoning, not a measurement. Profile before scaling.
- **The Cortecs catalogue envelope.** The pricing *fields* were verified against the
  documentation; the JSON shape wrapping the model list was not seen live. The parser
  accepts several plausible shapes and reports what it cannot read.
  ([ADR 0014](../../docs/adr/0014-model-catalogue-and-pricing.md))

**Verified against a real running stack**

`docker compose up` was built and run: PostgreSQL 18, Valkey, Keycloak, migrations and the
gateway all healthy, real requests served, quotas enforced, the database fallback confirmed
by stopping Valkey mid-workload, and the **full OIDC login flow** driven against a real
identity provider — including a session minting an API key that then served a billed
completion. See ADRs [0005](../../docs/adr/0005-persistence.md),
[0006](../../docs/adr/0006-counter-store.md) and
[0011](../../docs/adr/0011-oidc-integration.md) for what was checked and the problems it
found.

**Deliberately not implemented**

- Device authorization flow endpoints, which the `opencode` bootstrap needs. See
  `scripts/README.md`.
- Reranking. The embedding half of [ADR 0020](../../docs/adr/0020-embeddings-and-reranking.md)
  is now served; `/v1/rerank` is not, and has no OpenAI-compatible shape to copy.
- Retrying an upstream request without `stream_options` when a provider rejects unknown
  parameters. A provider that does is configured with `forward_stream_options = false`
  instead, which costs nothing at request time.
  ([ADR 0028](../../docs/adr/0028-embeddings-and-served-model.md))
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
