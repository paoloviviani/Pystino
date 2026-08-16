# Working on this repository

A self-hosted LLM platform for LINKS Foundation. Read this before changing
anything; it is the context that is not recoverable from the code.

## Ground rules, in priority order

1. **EUPL-1.2 for all first-party code, and licensing is a hard requirement.**
   Ask before adopting anything with a non-OSI licence, a CLA, or an open-core
   model. Not a preference to be traded off — see
   [ADR 0001](docs/adr/0001-licensing.md).
2. **Where you are unsure whether a library or version is current, say so.**
   Research at source rather than from memory; several decisions here turned on
   details found only by reading the provider's live schema. Guessing a version
   number and being wrong is worse than asking.
3. **Accounting and quota logic get tests specifically.** That is where
   correctness actually matters, and where a wrong answer is a wrong invoice
   rather than a stack trace.
4. **Never put the dev stack on a routable address.** Keycloak runs `start-dev`
   with `admin`/`admin`, there is no TLS, the session cookie is not `Secure`,
   and the seeded passwords are in the repo. The base compose files bind
   `127.0.0.1` for this reason. To reach it from another machine, use the SSH
   tunnel or `docker-compose.overlay.yml`, which publishes on a private
   WireGuard-mesh address as well — see the README. A private overlay address
   satisfies this rule; a public one does not, whatever else is done to it.

## Explaining the work

Commit messages and code comments here carry the *reasoning*, not a summary of
the diff. The house style, worth matching:

- Say **why**, especially where the obvious approach was rejected and why it
  was wrong. A comment that restates the code earns nothing.
- Name the failure a decision prevents. "Refused rather than converted: an
  exchange rate applied silently produces invoices that look right and are
  wrong."
- Record bugs found while building, in the commit and in the ADR. Several of
  the most valuable notes in this repo are of that shape.
- No hedging in reports. If a test fails, say so with the output. If something
  is unverified, say which part.

## Where things are

```
apps/gateway     the whole backend: /v1 proxy surfaces, /api management, console hosting
apps/console     React admin SPA, served by the gateway at /console
packages/ui      design tokens and primitives, shared with the Phase 3 chat app
packages/shared-py  detection contract and the deterministic placeholder scheme
services/redaction  Presidio behind a swappable contract, out of process
deploy/compose   the stack: base + smoke + keycloak + redaction overlays
scripts/         live checks against a running stack (see below)
docs/adr/        31 ADRs. Read the index; they are the design record.
```

Inside the gateway, the pieces that carry the most weight:

| Path | What it owns |
|---|---|
| `routers/_metered.py` | resolve → reserve → record → settle, shared by all five `/v1` routes |
| `protocols.py` | per API surface: where usage, the served model and assistant text live in a frame |
| `accounting/cost.py` | the money arithmetic, and the three prompt slices |
| `quota/engine.py` | admission; `counters.py` has the three stores |
| `access.py` | one predicate for "may this caller use this model" |
| `pagination.py` | the listing envelope every management route returns |

## Non-obvious things that will bite you

- **The two prompt conventions are opposites.** OpenAI's `prompt_tokens`
  *includes* cached tokens; Anthropic's `input_tokens` *excludes* them. Each
  surface has its own named reader in `accounting/cost.py` for this reason. Do
  not "simplify" them into one tolerant parser.
- **SQLite is more forgiving than PostgreSQL** in ways that hide real bugs. It
  will `SELECT DISTINCT` over a JSON column; PostgreSQL has no equality
  operator for `json` at all. The unit suite runs on SQLite, so anything
  dialect-shaped needs a live script or a compile-against-the-dialect test.
- **Money is a string end to end.** `Numeric(24,12)` round-trips zero as
  `Decimal("0E-12")`; the `Money` annotated type in `schemas.py` forces plain
  digits. Never parse an amount into a float, including in the browser.
- **`InMemoryCounterStore` is atomic for an uninteresting reason** — it never
  awaits. It cannot prove anything about `MULTI`/`EXEC`, which is why
  `scripts/test_quota_race_live.py` exists.
- **Keycloak's advertised hostname must match however you reached it.**
  `KEYCLOAK_PORT` moves the published port *and* `KC_HOSTNAME` together;
  `OVERLAY_ADDR` moves the host part of both, plus the gateway's redirect URI
  and the realm's registered callback. Changing either needs the gateway
  restarted — it reads OIDC discovery once at startup. And the login-state
  cookie is per-origin, so with the overlay active you can only *sign in* on
  the overlay address; `localhost` still serves `/v1` and the API, which is
  what the live scripts need. They follow `OVERLAY_ADDR` when it is set, so
  source `deploy/.env` before running them.
- **The demo user's cap is EUR 1/hour and the fake upstream bills 1M tokens per
  request.** Running several live scripts back to back exhausts it legitimately;
  they report that as skipped. Flushing Valkey alone does not reset it — the
  counters rebuild from `usage_records`, so clear both.

## Verifying a change

```bash
uv run ruff check . && uv run mypy apps/gateway/src services
uv run pytest -q                       # 648 gateway tests, SQLite
pnpm -r test                           # 10 packages/ui + 68 console
```

Then, for anything touching the request path, money, or SQL, against the real
stack:

```bash
docker compose --env-file deploy/.env \
  -f deploy/compose/docker-compose.yml \
  -f deploy/compose/docker-compose.smoke.yml \
  -f deploy/compose/docker-compose.keycloak.yml \
  -f deploy/compose/docker-compose.redaction.yml up -d --build

./scripts/test_oidc_flow.py         # the whole authorization-code flow
./scripts/test_reporting_live.py    # dialect-specific SQL the suite cannot reach
./scripts/test_redaction_live.py
./scripts/test_console_live.py      # console, CSP, pagination
./scripts/test_providers_live.py    # credentials encrypted in PostgreSQL, routing
./scripts/test_surfaces_live.py     # responses, anthropic messages, images
./scripts/test_quota_race_live.py   # admission under concurrency, real Valkey
```

**Run the live scripts.** More than half the serious bugs in this project's
history were only findable against the running stack: a counter seeded at zero,
a migration given the wrong environment, a 500 on `/v1/models` that every unit
test passed through.

This machine has 3 GB of RAM and 2 cores. The compose stack plus a `pnpm test`
will swap, and the symptom is tests that fail having done nothing wrong — check
`free -g` before believing a frontend failure.

## Deployment

Development host `130.192.84.52`, console over an SSH tunnel — the README's
"Reaching the console from another machine" section has the command and why
both ports must match. Git remote is GitLab; the token is in `.gitlab.env`,
which may be sourced but should not be read.

## Where the project is

Phase 2 is complete: gateway, redaction, console, providers, quotas, reporting,
five `/v1` surfaces. `docs/phase-2-plan.md` records what was planned and what
was added afterwards, including the bugs each addition surfaced.

Known open items, none of them blocking:

- **Pagination**: done. **Concurrency**: done and verified. Both were the last
  outstanding items from Phase 2.
- Whether Cortecs rejects `stream_options` is still unverified — it needs a real
  API key. `scripts/check_cortecs_stream_options.py` answers it in four requests.
- Deferred by the user: per-provider default body params (`eu_native`,
  `allow_zero_data_retention`), image editing and variations, per-size image
  pricing, reranking.
- Phase 3 is the chat frontend, RAG, MCP, the code sandbox, the desktop app and
  the `opencode` device flow. Nothing has been started.
