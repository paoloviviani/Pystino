# Operations

## Verifying a change

```bash
uv run ruff check . && uv run mypy apps/gateway/src services   # mypy is --strict
uv run pytest -q                                               # ~950 tests, SQLite, no services
pnpm -r test && pnpm -r typecheck                              # packages/ui + console + web
```

Tests need no PostgreSQL, Valkey or network (SQLite + a fake upstream
transport). Run pytest from the repo root — testpaths and pythonpath live in
the root `pyproject.toml`. A single file:
`uv run pytest apps/gateway/tests/test_cost.py -q`.

Alembic migrations (`apps/gateway/migrations/versions/`) are excluded from ruff
and mypy. Against a real DB:
`uv run alembic -c apps/gateway/alembic.ini upgrade head`.

Anything touching the request path, money or SQL also gets run against the
real stack — the live checks below. More than half the serious bugs in this
project's history were only findable there: a counter seeded at zero, a
migration given the wrong environment, a 500 on `/v1/models` that every unit
test passed through.

### The per-request database budget

`apps/gateway/tests/test_query_counts.py` pins the SQL round trips per request:
**3 SELECTs to authenticate** and **5 SELECTs + 2 writes for a metered
request**. A regression here fails a test rather than a dashboard. Two habits
keep it green: prefer `joinedload` over `selectinload` on many-to-one
relations (the budget assumes it — one SELECT per relation is what was
removed), and remember the two writes are by design, not a saving to go after:
the in-progress row exists before the upstream call so a crash mid-request is
visible.

## The live checks

```bash
docker compose --env-file deploy/.env \
  -f deploy/compose/docker-compose.yml \
  -f deploy/compose/docker-compose.smoke.yml \
  -f deploy/compose/docker-compose.redaction.yml up -d --build
```

Then, with the accounts created (`docker compose ... exec gateway gateway
passwd admin@local`, and `--no-admin user@local` for the 403 checks) and
`GATEWAY_LOCAL_ADMIN_PASSWORD` set in `deploy/.env`:

| Script | Covers |
|---|---|
| `test_reporting_live.py` | dialect-specific SQL the SQLite suite cannot reach |
| `test_redaction_live.py` | the detection service, over real HTTP |
| `test_console_live.py` | the console, CSP, pagination |
| `test_providers_live.py` | provider credentials encrypted in PostgreSQL, routing, per-user access |
| `test_surfaces_live.py` | responses, Anthropic messages, image generation |
| `test_quota_race_live.py` | admission under concurrency, against real Valkey |
| `test_cache_accounting_live.py` | a real cache hit, and what the ledger records |
| `benchmark_live.py` | per-layer cost; reproduces [Measured performance](performance.md) |
| `test_public_tls_live.py` | only with the proxy overlay: TLS, rotated credentials, and that nothing else is on a routable address |

They sign in with local password auth, so with the proxy overlay they also need
Caddy's CA (see [Deployment](deployment.md#verify-what-is-exposed)) and
`deploy/.env` sourced — they follow `PUBLIC_HOST` when it is set, and the
session cookie is scoped to the origin the login happened on.

!!! warning "The demo cap exhausts legitimately"

    The demo user's cap is **EUR 1/hour** and the fake upstream bills ~1M
    tokens per request. Several live scripts back to back will legitimately
    exhaust it, reported as *skipped*, not failed. Flushing Valkey alone does
    **not** reset it — the counters rebuild from the ledger, so clear both
    `usage_records` and the Valkey counters.

## Performance, in one page

The measured numbers ([full page](performance.md)):

- The framework is not the cost: FastAPI + middleware is 0.4ms of CPU; a
  metered completion is ~12ms; everything above that is our own work.
- **Redaction dominates**: ~0.1ms of CPU per prompt token; at realistic sizes
  detection is 85–95% of the total. Capacity planning is redaction planning.
- The detection cache is a per-process LRU; adding workers lowers the hit
  rate. The cached path costs 13ms where the uncached path at the same length
  costs 135ms.
- What binds before CPU: the upstream client's `max_connections = 200`
  (long-held streaming connections), `usage_records` growth (two writes per
  request; wants partitioning and rollups), the DB pool, and Valkey as a
  single point whose loss degrades quotas to the (correct but wider)
  database fallback.
- The stateless property that makes replicas safe — atomic admission in
  Valkey — is verified, not assumed: 40 concurrent clients, exactly the
  arithmetic number admitted.

Reproduce with `uv run python scripts/benchmark_live.py {ladder,load,redaction,all}`
against a running stack. It creates an **unpriced** model and its own key, so
a benchmark cannot distort billing.

## Known open items

None blocking; all recorded so they are not rediscovered as surprises.

- **A stream settled at the moment the client disconnects can lose the write.**
  The `completed` branch of the streaming `finally` awaits a database write
  inside the request task; a client closing the connection at exactly the
  wrong millisecond cancels it mid-write, leaving the row `in_progress` with
  zero tokens for a request the provider served in full. The disconnect branch
  beside it was already made cancellation-proof (the write is detached into a
  task); the fix routes the completed branch through the same mechanism and
  needs a session outside the request scope — its own piece of work with
  tests.
- **The estimated-usage disclosure blames the provider for every case.** It
  cannot yet tell "the provider reported no usage" from "the client
  disconnected mid-stream". The row knows — read `status` before believing the
  sentence.
- **GDPR erasure is incomplete** — `assistant_text` is not covered by the
  identity cascade. See [Gateway — known gaps](gateway.md#known-gaps).
- **Counters rebuild only at startup**: a Valkey wipe while the gateway keeps
  running leaves quotas briefly too permissive. Restart the gateway after any
  cache loss.
- **Cortecs and `stream_options`**: accepted and usage arrives with or without
  it (checked live), so the plugin adds nothing; whether sending it narrows
  the routing pool is unverified — the serving provider is not named in
  stream frames, and `scripts/check_cortecs_stream_options.py` reports that
  part as unknown.

## Where decisions live

Every significant decision is an ADR in [docs/adr/](https://example.invalid/viviani/ai-stack/-/blob/main/docs/adr/README.md), with the
licence, version and CVE evidence behind it and the date it was checked. The
conventions: number sequentially, never renumber, never delete — **supersede
instead**, and say which ADR supersedes which. An ADR whose decision was
reversed is more useful than one that was quietly removed, because the
reversal is the thing worth knowing.
