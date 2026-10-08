# Operations

## Verifying a change

```bash
uv run ruff check . && uv run ruff format --check .
uv run mypy apps/gateway/src packages/shared-py/src services   # --strict
uv run pytest -q                                               # about 1,900 tests, SQLite, no services
pnpm -r test && pnpm -r typecheck                              # packages/ui + console
uv run mkdocs build --strict                                   # this site
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
**4 SELECTs for `GET /v1/models`** (the key with its user and group,
memberships, the catalogue, and the agents the caller may reach) and
**5 SELECTs + 2 writes for a metered request**; a bearer token may cost no more
than a key. A regression here fails a test rather than a dashboard. Two habits
keep it green: prefer `joinedload` over `selectinload` on many-to-one
relations (the budget assumes it — one SELECT per relation is what was
removed), and remember the two writes are by design, not a saving to go after:
the in-progress row exists before the upstream call so a crash mid-request is
visible.

## The live checks

Bring up a stack with the bundled Authelia (see
[Deployment](deployment.md)); a development one with the fake upstream is the
usual target.

| Script | Covers |
|---|---|
| `test_reporting_live.py` | dialect-specific SQL the SQLite suite cannot reach |
| `test_redaction_live.py` | the detection service, over real HTTP |
| `test_console_live.py` | the console, CSP, pagination |
| `test_providers_live.py` | provider credentials encrypted in PostgreSQL, routing, per-user access |
| `test_surfaces_live.py` | responses, Anthropic messages, image generation |
| `test_quota_race_live.py` | admission under concurrency, against real Valkey |
| `test_cache_accounting_live.py` | a real cache hit, and what the ledger records |
| `test_bill_to_live.py` | `x-bill-to` with a real access token: which group actually paid |
| `test_pystino_usage_live.py` | `GET /v1/pystino/usage` under both credentials, through real discovery and a live JWKS fetch |
| `test_citations_live.py` | a citation that still quotes the right word after a real placeholder changed the offsets |
| `test_web_search_live.py` | per-search billing through PostgreSQL's `Numeric`, including the CSV export's own column list |
| `benchmark_live.py` | per-layer cost; reproduces [Measured performance](performance.md) |
| `test_public_tls_live.py` | TLS, the IdP refusing a wrong password, a Secure session cookie, and that nothing else is on a routable address |

They sign in the way a browser does — the gateway's OIDC login through the
bundled Authelia (`scripts/live_session.py`) — so they run against the public
origin, with the deployment's `.env` sourced for `PUBLIC_ORIGIN`:

```bash
cd <deploy dir>; set -a; . ./.env; set +a
export PYSTINO_LIVE_ADMIN_PASSWORD=…        # the password behind AUTHELIA_ADMIN_PASSWORD_DIGEST
export PYSTINO_LIVE_USER=… PYSTINO_LIVE_USER_PASSWORD=…   # optional non-admin
# a trial stack with no DNS / on Caddy's internal CA:
export PYSTINO_LIVE_RESOLVE=127.0.0.1 GATEWAY_CA_BUNDLE=$PWD/caddy-root.crt
uv run --project <checkout> python <checkout>/scripts/test_console_live.py
```

The non-admin is added in the console (the Users page).

!!! warning "A cost ceiling exhausts legitimately"

    The fake upstream bills ~1M tokens per request, so a small cost ceiling on
    the test user's group (the scripts' comments assume EUR 1/hour; `gateway
    seed` gives its group EUR 10/day) is spent after a few requests. Several
    live scripts back to back will legitimately exhaust it, reported as
    *skipped*, not failed. Flushing Valkey alone does **not** reset it — the
    counters rebuild from the ledger, so clear both `usage_records` and the
    Valkey counters, or raise the rule while testing.

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

- **The estimated-usage disclosure blames the provider for every case.** It
  cannot yet tell "the provider reported no usage" from "the client
  disconnected mid-stream". The row knows — read `status` before believing the
  sentence.
- **GDPR erasure is incomplete** — deleting a person does not blank
  `assistant_text` on their ledger rows, but it is cleared after `GATEWAY_TRANSCRIPT_RETENTION_HOURS`
  (24 by default). See [Gateway — known gaps](gateway.md#known-gaps).
- **Counters rebuild only at startup**: a Valkey wipe while the gateway keeps
  running leaves quotas briefly too permissive. Restart the gateway after any
  cache loss.
- **Cortecs and `stream_options`**: accepted and usage arrives with or without
  it (checked live), so the plugin adds nothing; whether sending it narrows
  the routing pool is unverified — the serving provider is not named in
  stream frames, and `scripts/check_cortecs_stream_options.py` reports that
  part as unknown.

## Where decisions live

Every significant decision is an ADR — a dated design record with the
licence, version and CVE evidence behind it and the date it was checked.
The ADRs are kept with the project's private planning material rather than
in this repository; ask the maintainers for a specific decision. The
conventions: number sequentially, never renumber, never delete — **supersede
instead**, and say which ADR supersedes which. An ADR whose decision was
reversed is more useful than one that was quietly removed, because the
reversal is the thing worth knowing.
