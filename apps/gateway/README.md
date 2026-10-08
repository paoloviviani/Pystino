# Gateway

OpenAI-compatible API gateway with per-user and per-group accounting, quotas,
per-group model availability and a pluggable redaction layer.

The reader-facing reference is [docs/gateway.md](../../docs/gateway.md): every
surface, the two authentication schemes, and the streaming traps with the file
that handles each. This file is the package's own README — what it is made of,
how to run it, and what it does not do.


## What it serves

- `/v1` — the metered surfaces (chat completions, responses, Anthropic
  messages, embeddings, image generation, OCR, web search), plus models,
  billing groups, `/v1/me`, `/v1/me/identities`, `pystino/usage`, and the
  chat's sign-in door `/v1/session/announce`. One dependency
  authenticates all of them and takes either an API key or an OIDC access
  token.
- `/api/me` and `/api/admin` — the management API, session cookie only.
  `/docs` is the generated Swagger over the same schemas; the human surface is
  the React console at `/console`.
- `/auth/*` — the OIDC sign-in, `/scim/v2/<provider>` — SCIM pushes from an
  external directory, `/opencode/install.sh` — the opencode setup script.
- `/healthz`, `/readyz` — liveness with no dependencies, readiness with one DB
  round trip.

## Layout

```
src/gateway/
  config.py         all configuration; nothing else reads the environment
  models.py         SQLAlchemy ORM — the usage ledger lives here
  pagination.py     the listing envelope, its bounds and the count query
  protocols.py      per API surface: where usage, the served model and the
                    assistant text live in a response frame
  types.py          Money (Numeric, never float) and UTC-safe datetimes
  security.py       API key generation, SHA-256 hashing, verification
  oidc.py           discovery, PKCE, ID token validation, group claim mapping
  access.py         who may reach which model, by group or personal grant
  upstream.py       the provider client, with read timeout deliberately None
  sse/              event-boundary parsing and the transform pipeline
  accounting/       token counts, cost, and the ledger writer
  quota/            rolling windows, counter stores, reserve-then-settle
  redaction/        the interface, the buffering rewriter, the resolver
  plugins/          provider and search-backend plugins: facts about a
                    counterparty, never arithmetic
  routers/          HTTP endpoints, with _metered.py the shared
                    resolve → reserve → record → settle path every metered
                    /v1 route goes through so the ordering cannot drift
```

## Running

```bash
uv sync
# from the repository root, with PostgreSQL and Valkey available:
uv run alembic -c apps/gateway/alembic.ini upgrade head
uv run gateway seed          # prints an API key, shown once
uv run gateway serve --reload
```

Or `docker compose -f deploy/compose.yaml up -d --wait` for the Pystino-only deployment (see `deploy/.env.example`).

## Testing

```bash
# from the repository root:
uv run pytest -q               # ~1,900 tests, SQLite, no services needed
./scripts/smoke_test.sh        # end-to-end over real HTTP, no Docker
```

Tests run against SQLite by default so they need no services. Money arithmetic
is tested as pure `Decimal` functions, because SQLite cannot store `Numeric`
natively. `test_migrations_sqlite.py` runs the whole migration chain on SQLite;
the schema meets PostgreSQL only in `docker compose` (the `migrate` service), in
the `stack` workflow on a release tag, and in the live checks — CI's unit job
has no PostgreSQL.

Anything touching the request path, money or SQL also wants the live checks in
`scripts/` against a running stack — see
[docs/operations.md](../../docs/operations.md#the-live-checks). More than half
the serious bugs in this project's history were only findable there.

## Things that are easy to get wrong, and where they are handled

| Trap | Where |
|---|---|
| Streamed responses carry no token counts unless `stream_options.include_usage` is forced | `routers/chat.py:build_upstream_payload` |
| The usage frame must be stripped if the client did not ask for it — but only the *usage-only* frame may be dropped whole | `routers/chat.py:usage_visibility_stage` |
| SSE must be parsed at `\n\n` boundaries, not network chunk boundaries | `sse/parser.py` |
| A trailing `\r` split across chunks is ambiguous and must be held back | `sse/parser.py:_pop_line` |
| httpx's 5s default read timeout kills long streams | `upstream.py:build_http_client` |
| Client disconnect must close the upstream *and* still record accrued usage | `routers/chat.py:spawn_finalisation` |
| A client that hangs up the instant the stream ends must not cancel the settling write | `routers/chat.py:settle_completed` |
| Assistant output must be persisted as it streams, not only at the end | `accounting/recorder.py:maybe_flush` |
| An absent usage frame must not be recorded as zero spend | `accounting/tokens.py` |
| Concurrent requests must not each pass the same under-limit check | `quota/engine.py:check_and_reserve` |
| Money must never touch a float, including inside the counter store | `types.py`, `quota/counters.py` |
| A wiped counter cache reports zero spend, not an error, so every group gets a fresh budget | `quota/engine.py:rebuild_if_cache_is_cold` |
| Editing a price would rewrite what past requests cost, so prices are append-only | `routers/admin.py:create_price` |
| Deleting a model must not lose the spend recorded against it: `usage_records.model_id` is `ON DELETE SET NULL` and every row keeps `model_name` | `routers/admin.py:delete_model` |

## Known gaps

Stated plainly, so none of these is a surprise later.

**Needs verifying against the real thing**

- **OIDC against providers other than Keycloak and Authelia.** The full
  redirect flow is verified end to end against Keycloak 26.7 and Authelia
  4.39.22, but Entra ID, Google
  and others differ in exactly the places the OIDC settings make configurable: where
  groups live, whether they appear in the ID token at all, how they are named.
  Re-run the same checks against the real provider before going live; the
  checklist is in [docs/oidc-generic-provider.md](../../docs/oidc-generic-provider.md).
- **Load behaviour beyond one small box.** It has been measured on 2-core and
  5-core hosts with everything co-resident; profile before scaling.
- **The Cortecs catalogue envelope.** The pricing *fields* were verified against
  the documentation; the JSON shape wrapping the model list was not seen live.
  The parser accepts several plausible shapes and reports what it cannot read.

**Deliberately not implemented**

- **Device authorization endpoints of the gateway's own.** The coding-agent
  enrollment that would have needed them authenticates against the deployment's
  identity provider instead, and keeps a refresh credential rather than a
  minted key — see
  [docs/coding-agents.md](../../docs/coding-agents.md). Nothing is waiting on
  this.
- **Reranking.** `/v1/embeddings` is served; `/v1/rerank` is not, and has no
  OpenAI-compatible shape to copy.
- **Image editing and variations** (`/v1/images/edits`,
  `/v1/images/variations`). They take multipart uploads, which the redaction
  layer has no story for.
- **Server-side conversation state on `/v1/responses`.** `previous_response_id`
  and `store` are refused: a stored prefix is billed on every follow-up and this
  gateway would have no record of what it contained.
- **Retrying an upstream request without `stream_options`** when a provider
  rejects unknown parameters. A provider that does gets a plugin whose
  `prepare_payload` never adds it, which costs nothing at request time.
- **A hard mid-stream quota ceiling via `max_tokens` clamping.** The chosen
  policy admits the request that crosses the limit.

**Known operational sharp edges**

- **GDPR erasure is incomplete.** Deleting a user removes the account and
  erases them from the chat; identity foreign keys on the ledger use `ON DELETE
  SET NULL` so the financial rows survive — but `assistant_text` on them may
  itself contain personal data and is *not* cleared by the delete.
  Retention bounds it: the text is cleared after
  `GATEWAY_TRANSCRIPT_RETENTION_HOURS` (default 24, `0` keeps it).
- **The redaction HMAC key must be backed up with the transcripts it labelled.**
  Losing or rotating it breaks cross-turn placeholder consistency for existing
  conversations.
- **Counters rebuild from the ledger only at startup.** If Valkey is wiped while
  the gateway keeps running, the cache reports zero and quotas are briefly too
  permissive until the gateway restarts. An empty cache is not a failed read, so
  nothing detects it at runtime. Restart the gateway after any cache loss.
