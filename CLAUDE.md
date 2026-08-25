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
docs/adr/        34 ADRs. Read the index; they are the design record.
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
- **But cache-write *spellings* are tolerated, deliberately.** There are four
  names for that one quantity — `_CACHE_WRITE_KEYS` in `accounting/cost.py` —
  and reading only one bills those tokens at the input rate. The distinction
  from the rule above is the point: those differ in **meaning**, these differ
  only in **spelling**. See
  [docs/cache-accounting-findings.md](docs/cache-accounting-findings.md).
- **Three cost figures, one meaning each.** `cost` is what we charge and is what
  quotas and reports read; `computed_cost` is always our arithmetic; and
  `upstream_cost` is always the counterparty's. `cost_source` says which one
  billed, and `own_prices_fallback` means a pass-through provider reported
  nothing — never silent, because that would be billing from a price table
  nobody maintains. An unpriced model reserves nothing, so it has no cost
  ceiling at all; `unpriced_model_count` on the provider listing is the warning.
- **A provider's own reported cost is its plugin's to read.** `usage.cost` is
  micro-EUR from Cortecs and credits from OpenRouter, and nothing in the payload
  says which — so the unit lives in the plugin, not in a column an operator fills
  in. A plugin that does not read a cost reports none, which is the safe default.
  `ReportedCost.authoritative` is separate and is what gates pass-through
  billing: reporting a figure is not claiming it is the charge.
- **SQLite is more forgiving than PostgreSQL** in ways that hide real bugs. It
  will `SELECT DISTINCT` over a JSON column; PostgreSQL has no equality
  operator for `json` at all. The unit suite runs on SQLite, so anything
  dialect-shaped needs a live script or a compile-against-the-dialect test.
- **The look is Bauhaus, and it lives in one file.** `packages/ui/src/tokens.css`
  carries the palette, the geometry and the type, and components reference tokens
  and never literals — the whole restyle in [ADR 0034](docs/adr/0034-bauhaus-design-language.md)
  was almost entirely that file. Two consequences that will bite: **there is no
  green**, so `ok` is the blue and a *classification* badge is black while a
  *state* badge is blue (they collided when green was dropped); and **cadmium
  yellow cannot be both type and fill**, so `--colour-warn` is a dark
  yellow-brown for text and `--colour-yellow` is the fill. Jost* is vendored
  under the SIL OFL — the licence file sits beside it in `packages/ui/src/fonts/`.
- **Money is a string end to end.** `Numeric(24,12)` round-trips zero as
  `Decimal("0E-12")`; the `Money` annotated type in `schemas.py` forces plain
  digits. Never parse an amount into a float, including in the browser.
- **Stored precision is twelve places; *displayed* precision is three.** The
  ledger's precision is real and is kept; putting it on a screen is not. Both
  formatters default to milli-units — `formatMoney` in `packages/ui` and
  `format_money_prose` in `gateway/types.py`, for figures embedded in a
  disclosure the console renders verbatim. Three things about that default:
  it is **opt-out** (`{ exact: true }`), because when it was opt-in four of the
  five screens leaked twelve decimals by saying nothing; it is *at most* three,
  so €12.50 is not written €12.500; and a real amount **never rounds to zero** —
  it reads `< €0.001`, because "€0.00" for genuine spend makes the ledger look
  broken. Admins reach full precision with the **Exact figures** toggle in the
  identity menu, which is a `MoneyPrecisionContext` — a `formatMoney` call made
  outside `<Money>` has to read it by hand (`useExactMoney`).
- **Redaction is ~90% of the CPU, and it scales with prompt length** — about
  0.1ms per prompt token, against a gateway cost that stays flat at 24-31ms.
  Capacity planning is redaction planning; see
  [docs/performance.md](docs/performance.md). Its detection cache is a
  *per-process* LRU, so adding workers lowers the hit rate.
- **Vendor quirks belong in a plugin, not in the accounting.** `gateway/plugins/`
  owns which header names a counterparty uses, what unit it reports cost in, and
  whether it is a provider or a router. The rule that keeps this safe is that a
  **plugin returns facts and never computes money** — there is deliberately
  nothing it can return that would let it price a request (ADR 0032).
- **Per-request round trips are pinned by a test.** `test_query_counts.py`
  bounds them at 3 selects to authenticate and 5 + 2 writes for a metered
  request. `selectinload` on a many-to-one relation costs a round trip that
  `joinedload` does not; that is how the budget was set.
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
uv run pytest -q                       # 809 gateway tests, SQLite
pnpm -r test                           # 21 packages/ui + 100 console
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
./scripts/test_cache_accounting_live.py  # a real cache hit, and the ledger
./scripts/benchmark_live.py         # per-layer cost; see docs/performance.md
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

Two pieces of work with their reasoning written down rather than left to be
rediscovered — one being built, one not started:

- **[ADR 0032](docs/adr/0032-provider-plugins.md)** *(accepted; being built)* — providers and
  routers are different kinds, distinguished by whether the serving endpoint is
  implied by the model or chosen per request. Vendor knowledge moves into
  plugins, pricing with it, and the load-bearing rule is that **a plugin returns
  facts and never computes money** — `accounting/cost.py` stays the only code
  that multiplies a count by a rate. Cortecs is the router reference
  implementation. Note the measurement recorded there: Cortecs charges its
  listed price whichever sub-provider serves, so per-endpoint pricing buys
  attribution and drift detection rather than different rates. Billing has two
  configurable modes — our prices, or the counterparty's reported figure — and
  **both figures are recorded in both modes**, so a divergence is always
  reconstructable and a fallback is never silent.

  Built so far: the plugin protocol and registry (`gateway/plugins/`, in-tree
  plus the `llmp.providers` entry point), the generic, Anthropic and Cortecs
  plugins, `providers.plugin` / `providers.kind`, `upstream_provider` finally
  populated for routers, both billing modes with `computed_cost` / `cost_source`
  / `upstream_cost_details`, the console's provider-type selector, and the three
  reactive columns removed — `auth_scheme`, `forward_stream_options` and
  `upstream_cost_unit` are now `auth_headers` / `prepare_payload` and the
  plugin's own knowledge of its counterparty's unit. **Note what that costs:** a
  per-row knob became per-plugin behaviour, so two providers of the same type
  that need different answers now need two plugins. Migration 0010 translates
  `auth_scheme = x_api_key` into `plugin = anthropic` and prints a line naming
  every row whose behaviour changes. Still to come: `catalogue()` replacing
  `scripts/import_cortecs_pricing.py`.


- **[docs/redaction-scoping-plan.md](docs/redaction-scoping-plan.md)** —
  visibility is **done** (`GET /api/admin/redaction`, the Redaction screen), and
  so is **choosing the engine** ([ADR 0033](docs/adr/0033-redaction-engine-selection.md)):
  the registry describes every installed engine, `redaction_config` is an
  append-only row that overrides `GATEWAY_REDACTION__ENGINE`, and
  `RedactionResolver` polls it every 10s so a change reaches the other worker
  without a restart and without a query on the request path. Switching to an
  engine that redacts nothing needs a written reason, kept permanently. Note
  `_engine_redacts` asks the registry rather than comparing against `"noop"` — an
  installable engine could redact nothing under any name.
  Still to do: redaction is process-global when it needs to be scopeable per
  model, provider, user or group, and the engine is not configurable from the
  console. The precedence rule to copy is the quota engine's, inverted — quotas
  are *all rules must pass*, redaction is *any applicable scope requiring it
  wins* — so adding a scope can only tighten. The doc carries the table shape,
  where it plugs into `_metered`, and why the first version has no exemptions.

Known open items, none of them blocking:

- **Pagination**: done. **Concurrency**: done and verified. Both were the last
  outstanding items from Phase 2.
- Cortecs **accepts** `stream_options` and reports usage with or without it, so
  `CortecsRouterPlugin.prepare_payload` adds nothing (checked against the live
  API). Still unverified is whether sending it narrows the routing pool — Cortecs
  does not name the serving provider in its stream frames, so
  `scripts/check_cortecs_stream_options.py` reports that part as unknown.
- Deferred by the user: per-provider default body params (`eu_native`,
  `allow_zero_data_retention`), image editing and variations, per-size image
  pricing, reranking.
- Phase 3 is the chat frontend, RAG, MCP, the code sandbox, the desktop app and
  the `opencode` device flow. Nothing has been started.
