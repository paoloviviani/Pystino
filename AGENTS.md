# AGENTS.md: working on this repository

Pystino is a self-hosted OpenAI-compatible model gateway with accounting,
quotas, redaction and OIDC sign-in, plus its admin console. Read this before
changing anything: it is the context that is not recoverable from the code.

## Ground rules

1. **Apache-2.0 for all first-party code.** Ask before adopting anything with a
   non-OSI licence, a CLA, or an open-core model.
2. **Where you are unsure whether a library or version is current, say so.**
   Check at source rather than from memory; several choices here turned on
   details found only in a provider's live schema.
3. **Accounting and quota logic get tests specifically.** A wrong answer there
   is a wrong invoice, not a stack trace.
4. **Never `ruff format` this repository.** Verification is `ruff check`; the
   tree has never been `ruff format`-clean, and running it rewraps dozens of
   unrelated files. Format only files you have just created.
5. **Nothing but the proxy is published.** The compose file binds the gateway
   to `127.0.0.1`; the world reaches it through Caddy.

Commit messages and comments carry the *reasoning*, not a summary of the diff:
say why, especially where the obvious approach was rejected; name the failure
a decision prevents; record bugs found while building. In reports, no hedging:
if a test fails, say so with the output.

## Layout

```
apps/gateway        the gateway: /v1 surfaces, /api management, console hosting
apps/console        React admin SPA, served by the gateway at /console
packages/ui         design tokens and primitives, shared with the console
packages/shared-py  detection contract and the deterministic placeholder scheme
services/redaction  Presidio behind a swappable contract, out of process
deploy/             the Pystino-only deployment: compose.yaml, caddy/ and
                    authelia/ (mounted read-only into stock images),
                    .env.example, pin.py, release.env
deploy/dev/         development-only fixtures: fake upstream, Keycloak
scripts/            live checks against a running stack, the fake upstream
docs/               how to run, deploy and operate it (mkdocs)
```

Two workspaces. Python is a `uv` workspace of `apps/gateway` and
`packages/shared-py`; `services/redaction` is deliberately **not** a member
(spaCy must never enter the gateway's lockfile), but its tests still run from
the repository root. JS/TS is a `pnpm` workspace of `packages/ui` and
`apps/console`. Alembic migrations (`apps/gateway/migrations/versions/NNNN_*.py`)
are excluded from ruff and mypy.

The gateway accepts a caller-supplied `x-request-id` and never returns the one
it uses: a caller who wants a transcript tied to cost mints it and sends it.

The deployment CLI in the gateway image (`pystino`, `gateway/deploy/cli.py`,
whose `build_parser` is the list to trust) runs inside a deployment and never
writes `.env`; every command takes its answers as flags, so none prompts:

| Command | What it is |
|---|---|
| `bootstrap` | the one-shot compose service, run on every `up` |
| `admin grant\|revoke <email> [--issuer]` | the ordinary-case admin recovery |
| `break-glass --email …` | the deeper recovery to the bundled Authelia; prints a login and password once, to stdout only; cerea-deploy's `./configure --break-glass` runs it |
| `idp check` | a live probe of the configured identity provider |
| `email export-env` | the mail configuration in force as `KEY=VALUE` lines, password included, for `./configure --import-smtp` |
| `erasure list\|retry <id>` | the chat erasure queue the background retry loop owns: see it, or force one attempt now |
| `quota health` | each quota rule's counter against the ledger, as JSON; read-only |
| `release-pin [--manifest] [--check]` | a maintainer tool: pin the release manifest's images by digest |

The separate `gateway` command (`gateway/cli.py`) is the development one:
`serve` and `seed`. The chat, Cerea, and its machine agent, galopin, live in
their own repository; the full-stack deployment lives in cerea-deploy.

Inside the gateway, the pieces that carry the most weight:

| Path | What it owns |
|---|---|
| `routers/_metered.py` | resolve → reserve → record → settle, shared by every metered `/v1` route |
| `protocols.py` | per API surface: where usage, the served model and assistant text live in a frame |
| `accounting/cost.py` | the money arithmetic, and the three prompt slices |
| `quota/engine.py` | admission; `quota/counters.py` has the three stores |
| `access.py` | one predicate for "may this caller use this model" |
| `pagination.py` | the listing envelope every management route returns |

## Verifying a change

```bash
uv sync
uv run ruff check . && uv run mypy apps/gateway/src packages/shared-py/src services   # --strict
uv run pytest -q                       # gateway tests: SQLite, a fake upstream, no services
pnpm -r test && pnpm -r typecheck      # packages/ui and the console
uv run mkdocs build --strict           # the docs site
```

Tests need no PostgreSQL, Valkey or network. Run pytest from the repository
root; a single file is `uv run pytest apps/gateway/tests/test_cost.py -q`. The
mypy pre-commit hook covers `apps/gateway/src` and `packages/shared-py/src`
only, so run mypy on `services` by hand; CI runs all three.

**Migrations** are Alembic, under `apps/gateway/migrations`:
`uv run alembic -c apps/gateway/alembic.ini revision -m "…"` to add one,
`… upgrade head` to apply. The compose `migrate` service runs them on every
`up`. A migration must also run on SQLite (the smoke test and the quick start use
it): branch on `op.get_bind().dialect.name`, as 0027 and 0047 do, and put any
`ALTER` of a constraint through `op.batch_alter_table`. `test_migrations_sqlite.py`
runs the whole chain there.

**The console** is built into the gateway image (`INCLUDE_CONSOLE=true`);
for development, `pnpm --filter console dev`. Without a Node toolchain on the
host, run `pnpm` in a container (`node:24-bookworm-slim`, with `CI=true` and
`corepack enable`), and never `pnpm config set --location project`: it writes
the container's store path into `pnpm-workspace.yaml`, a committed file.

Then, for anything touching the request path, money or SQL, against a real
stack: fill `deploy/.env` from `deploy/.env.example`, bring it up with the fake
upstream, and run the live checks:

```bash
PYSTINO_SRC=$PWD docker compose -f deploy/compose.yaml -f deploy/dev/smoke.yml up -d --wait
set -a; . deploy/.env; set +a            # the scripts read the deployment's own variables
./scripts/test_reporting_live.py         # dialect-specific SQL the suite cannot reach
./scripts/test_redaction_live.py
./scripts/test_console_live.py           # console, CSP, pagination
./scripts/test_providers_live.py         # credentials encrypted in PostgreSQL, routing
./scripts/test_surfaces_live.py          # responses, Anthropic messages, images
./scripts/test_quota_race_live.py        # admission under concurrency, real Valkey
./scripts/test_cache_accounting_live.py  # a real cache hit, and the ledger
./scripts/test_web_search_live.py        # per-search billing, the report and its CSV
./scripts/test_bill_to_live.py           # x-bill-to against a real OIDC token
./scripts/benchmark_live.py              # per-layer cost; see docs/performance.md
```

They sign in through the bundled Authelia (`scripts/live_session.py`): set
`PYSTINO_LIVE_ADMIN_PASSWORD` to the password behind
`AUTHELIA_ADMIN_PASSWORD_DIGEST`. **Run them.** More than half the serious bugs
in this project were only findable against a running stack: a counter seeded
at zero, a migration given the wrong environment, a 500 on `/v1/models` that
every unit test passed through.

Traps when running them:

- **Do not source `deploy/.env` before `docker compose`.** Compose prefers the
  shell environment over `--env-file`, and bash's quote removal mangles
  values that contain quotes or JSON. The *scripts* want it sourced; compose
  does not.
- **A whole suite exhausts the demo group's cost ceiling**, because the fake
  upstream bills a million tokens per request. The scripts report that as
  skipped. Flushing Valkey alone does not reset it: the counters rebuild from
  `usage_records`, so clear both, or raise the rule temporarily.
- **From a git worktree, `deploy/.env` does not exist** (it is gitignored).
  Symlink it, or scripts that shell out to `docker compose --env-file` do
  nothing.

## Non-obvious things that will bite you

- **The two prompt conventions are opposites.** OpenAI's `prompt_tokens`
  *includes* cached tokens; Anthropic's `input_tokens` *excludes* them. Each
  surface has its own named reader in `accounting/cost.py`. Do not "simplify"
  them into one tolerant parser.
- **But cache-write *spellings* are tolerated, deliberately.** There are four
  names for that one quantity (`_CACHE_WRITE_KEYS`), and reading only one bills
  those tokens at the input rate. Those differ only in spelling; the prompt
  conventions differ in meaning.
- **Three cost figures, one meaning each.** `cost` is what we charge, and what
  quotas and reports read; `computed_cost` is always our arithmetic;
  `upstream_cost` is always the counterparty's. `cost_source` says which one
  billed, and `own_prices_fallback` means a pass-through provider reported
  nothing. An unpriced model reserves nothing, so it has no cost ceiling at
  all; `unpriced_model_count` on the provider listing is the warning.
- **A plugin returns facts and never computes money.** Vendor quirks live in
  `gateway/plugins/`: header names, the unit a provider reports cost in
  (`usage.cost` is micro-EUR from Cortecs and credits from OpenRouter, and
  nothing in the payload says which), provider or router.
  `accounting/cost.py` is the only code that multiplies a count by a rate.
  `ReportedCost.authoritative` is what gates pass-through billing: reporting a
  figure is not claiming it is the charge.
- **SQLite is more forgiving than PostgreSQL** in ways that hide real bugs. It
  will `SELECT DISTINCT` over a JSON column; PostgreSQL has no equality
  operator for `json`. The unit suite runs on SQLite, so anything
  dialect-shaped needs a live script or a compile-against-the-dialect test.
- **Money is a string end to end.** `Numeric(24,12)` round-trips zero as
  `Decimal("0E-12")`; the `Money` type in `schemas.py` forces plain digits.
  Never parse an amount into a float, including in the browser.
- **Stored precision is twelve places; displayed precision is three.**
  `formatMoney` in `packages/ui` and `format_money_prose` in `gateway/types.py`
  default to milli-units, opt-out with `{ exact: true }`, and never round a real
  amount to zero (it reads `< €0.001`). Admins reach full precision with the
  **Exact figures** toggle, a `MoneyPrecisionContext`: a `formatMoney` call
  outside `<Money>` reads it with `useExactMoney`.
- **The look lives in one file.** `packages/ui/src/tokens.css` carries the
  palette (light and dark), the type and the geometry; components use tokens,
  never literals. Components are styled with Tailwind utilities, and the
  interaction layer (Dialog, Menu, Toast, Tooltip) is Base UI. Dark mode is one
  `.dark` block plus a class on `<html>`. Tailwind's content detection is
  rooted at the Vite project root, so `apps/console/src/index.css` declares
  `@source` for `packages/ui/src`: if a class "does nothing", check that line.
- **Provider-side web search is a surcharge.** `per_search` on the price row,
  `search_count` on the usage row. The count comes only from
  `usage.server_tool_use.web_search_requests`, never from counting blocks (an
  errored search produces a block and is not billed). Searches are recorded
  even when the model has no rate, so the gap is findable here rather than on
  an invoice.
- **There are two search counts; do not add them together.**
  `usage_records.search_count` is the counterparty's server-side search,
  billed per search. `usage_records.own_search_requests` is a call this gateway
  made to its own search backends (Linkup, Exa, Jina, DuckDuckGo), **counted, never
  priced**, and limited by the `OWN_SEARCH_REQUESTS` quota metric, which
  defaults to zero per request. A request ceiling bounds volume, not spend,
  and the quota form says so.
- **A search backend is a provider, and a search "model" is a tier.**
  `POST /v1/search` resolves a `ModelKind.SEARCH` model whose `upstream_model`
  is the vendor's depth or type, behind a provider whose plugin implements
  `SearchPlugin`. A search is counted before the call and never refunded. The
  query is redacted, which genuinely degrades it; the fix is the redaction
  scope, not an exemption. Exa's `costDollars` is logged, never stored.
- **Restoring a placeholder moves every offset after it.** Provider citations
  are character offsets into the answer, so `restore_with_edits` reports where
  it wrote and each surface protocol's `shift_citations` moves what it holds.
  A `Shift` is `(choice index, offset)`. Anthropic needs nothing moved. The
  streamed case needs the whole answer, so the rewriter keeps the provider's
  text per choice.
- **Redaction is most of the CPU, and it scales with prompt length**: about
  0.1 ms per prompt token, against a gateway cost that stays flat. Its detection
  cache is a per-process LRU, so more workers lower the hit rate.
- **The final ledger write happens after the response is sent** for the
  non-streamed routes (`metered.completed_after_response`). A failure there is
  logged, not raised, so the row stays `in_progress`. It is worse under
  saturation, because awaiting the settle was accidental backpressure.
  httpx's ASGI transport awaits background tasks, so `test_deferred_settlement.py`
  drives raw ASGI to prove the body goes out first.
- **`/api` reads a session cookie and nothing else.** Anything a
  bearer-authenticated client needs cannot live there. `GET /v1/me` is how a
  bearer caller learns who it is: `is_admin` comes from effective memberships,
  never the token's claim, and an API key never gets `is_admin: true`.
- **A request may choose which group pays, and a key may not.**
  `x-bill-to: <group name>` is honoured only for OIDC access tokens; a key
  sending it is refused, not ignored. The lookup walks the caller's own
  memberships, so a group they do not hold is indistinguishable from one that
  does not exist. `GET /v1/billing/groups` is what a client reads to offer the
  choice.
- **A directory owns the memberships it granted, and no others.**
  `memberships.source` is `oidc` or `manual`; a login's sync never touches
  manual ones. `identity_providers.group_sync` says how often the directory
  answers. `is_admin`, the default billing group and the sole-group rule read
  effective memberships, not the token.
- **A deployment may keep no ledger.** `GATEWAY_ACCOUNTING__ENABLED=false`
  writes no `usage_records`, never a row of zeros, and the report announces
  that metering is off. Quotas without a ledger are refused at startup, since
  counters rebuild from `usage_records`.
- **OIDC discovery is read once at startup**, so changing the issuer needs a
  restart, and **`iss` is part of a user's identity**: users are keyed on
  `(issuer, subject)`, so a new issuer re-provisions everyone.
- **Behind the proxy, uvicorn must trust the forwarded headers**
  (`FORWARDED_ALLOW_IPS`), or the app believes every request is http, and the
  only symptom is a post-logout URL a provider refuses. The session cookie is
  scoped to the origin the login happened on, so sign in and check on the same
  address.
- **Per-request round trips are pinned by a test.** `test_query_counts.py`
  bounds them: 3 selects to authenticate, 5 + 2 writes for a metered request.
  `selectinload` on a many-to-one relation costs a round trip that
  `joinedload` does not.
- **`InMemoryCounterStore` is atomic only because it never awaits.** It proves
  nothing about `MULTI`/`EXEC`, which is why `test_quota_race_live.py` exists.
- **An abandoned stream is billed from our price table.** A client that hangs
  up before the terminal SSE frame gets no usage and no reported cost from the
  provider, so tokens are estimated and cost falls back to our prices, even on
  a pass-through provider, and the reconciliation excludes those rows.

## Known open items

- **A stream settled at the moment the client disconnects can lose its
  write.** The `completed` branch of the streaming `finally` awaits the ledger
  write inside the request task, which uvicorn cancels on disconnect; the row
  stays `in_progress`. The disconnect branch already detaches its write
  (`spawn_finalisation`); the `completed` branch needs the same, with a session
  outside the request scope and its own tests.
- **The estimated-usage disclosure blames the provider for client
  disconnects.** `_disclosures` in `reporting.py` should split its sentence on
  `status`.
- **Reranking, image editing and variations, and per-size image pricing** are
  not implemented.
