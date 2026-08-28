# AGENTS.md

A self-hosted LLM platform for LINKS Foundation: an OpenAI-compatible gateway with
accounting, quotas and redaction, plus a chat app and admin console. Licence is
**EUPL-1.2** for all first-party code.

`CLAUDE.md` is the repo's own deep-context file — ground rules, recorded bugs, and the
traps that only show up on the live stack. Read it before changing anything;
`docs/adr/README.md` indexes every design decision. This file is the fast layer.

## Ground rules

- **Licence is a hard requirement, not a preference.** Ask before adopting any
  dependency with a non-OSI licence, a CLA, or an open-core model (ADR 0001).
- **Accounting and quota logic get tests specifically.** A wrong answer there is a
  wrong invoice, not a stack trace.
- **`.gitlab-token` / `.gh-token` at the repo root are credentials: never read,
  cat or print them.** Source one (`set -a; . .gh-token; set +a`) and use a
  throwaway credential helper — never argv, config or logs. They define
  `GITLAB_TOKEN` (gitlab.linksfoundation.com, the original home) and `GH_TOKEN`
  (github.com/paoloviviani/Pistin, current origin).
- **The stack is loopback-only by default.** Nothing may be published on a routable
  address except through `docker-compose.proxy.yml` (Caddy TLS + rotated credentials);
  `scripts/test_public_tls_live.py` enforces that by requiring every other port to be
  refused on the host's routable address.

## Layout: two workspaces, one deliberate exception

- Python: `uv` workspace — `apps/gateway`, `apps/chat-api`, `packages/shared-py`.
  `services/redaction` is **not** a member on purpose (spaCy must never enter the
  gateway's lockfile) — but its tests still run via the root `pyproject.toml` pytest
  config, from the repo root.
- JS/TS: `pnpm` workspace — `packages/ui` (design tokens + primitives),
  `apps/console`, `apps/web` (Vite + React, vitest).
- `apps/chat-api` is a `/v1` client of the gateway (ADR 0040) and imports nothing
  from it.
- Gateway internals that carry the weight: `routers/_metered.py`
  (resolve → reserve → record → settle), `accounting/cost.py` (the only code that
  multiplies a count by a rate), `quota/engine.py`, `access.py`, `plugins/`
  (vendor quirks live there, per ADR 0032).
- Alembic migrations: `apps/gateway/migrations/versions/NNNN_*.py` — excluded from
  ruff and mypy. Against a real DB:
  `uv run alembic -c apps/gateway/alembic.ini upgrade head`.

## Verify a change

```bash
uv run ruff check . && uv run mypy apps/gateway/src services   # mypy is --strict
uv run pytest -q                                               # ~950 tests, SQLite, no services
pnpm -r test && pnpm -r typecheck                              # packages/ui + console + web
```

- Tests need no PostgreSQL, Valkey or network (SQLite + fake upstream transport).
  Run pytest from the repo root; testpaths/pythonpath live in `pyproject.toml`.
  Single file: `uv run pytest apps/gateway/tests/test_cost.py -q`.
- SQLite is more forgiving than PostgreSQL (e.g. `SELECT DISTINCT` over JSON). Anything
  dialect-shaped needs `./scripts/test_reporting_live.py` against the real stack.
- `apps/gateway/tests/test_query_counts.py` pins per-request DB round trips
  (3 selects to authenticate, 5 + 2 writes per metered request). Prefer `joinedload`
  over `selectinload` on many-to-one relations — the budget assumes it.
- Pre-commit hooks run ruff + ruff-format + mypy, but the mypy hook only covers
  `apps/gateway/src` and `packages/shared-py/src` — run mypy on `services` by hand.
- The dev VM is small (2 cores). If a frontend test fails having done nothing wrong,
  check `free -g` first — `pnpm test` can swap there.

## Changes touching the request path, money, or SQL: run the live scripts

```bash
docker compose --env-file deploy/.env \
  -f deploy/compose/docker-compose.yml -f deploy/compose/docker-compose.smoke.yml \
  -f deploy/compose/docker-compose.keycloak.yml \
  -f deploy/compose/docker-compose.redaction.yml up -d --build
```

Then the matching `./scripts/test_*.py` (oidc, reporting, redaction, console,
providers, surfaces, quota_race, cache_accounting, bearer_tokens, chat). More than
half the serious bugs in this project's history were only findable against the
running stack.

- Compose is overlay-based; the base file binds `127.0.0.1` on purpose.
- **Source `deploy/.env` before the live scripts** — they follow
  `PUBLIC_HOST`/`OVERLAY_ADDR` when set (needed under the TLS proxy, where
  certificates are verified, not skipped).
- The demo user's cap is EUR 1/hour and the fake upstream bills ~1M tokens per
  request: several live scripts back to back legitimately exhaust it, reported as
  *skipped*, not failed. Flushing Valkey alone does **not** reset it — clear both
  `usage_records` and the Valkey counters (they rebuild from the ledger).

## Domain invariants that are easy to break

- **Money is a string end to end** (`Money` type in `gateway/schemas.py`;
  `Numeric(24,12)` round-trips zero as `Decimal("0E-12")`). Never parse an amount
  into a float, including in the browser. Stored precision is 12 places; displayed
  precision is at most 3, defaulting to milli-units, and a real amount never renders
  as zero (`< €0.001`).
- **The two prompt conventions are opposites**: OpenAI's `prompt_tokens` *includes*
  cached tokens; Anthropic's `input_tokens` *excludes* them. Each surface has its own
  named reader in `accounting/cost.py` — do not merge them into one tolerant parser.
  (Cache-*write* key spellings *are* deliberately tolerated across four names —
  `_CACHE_WRITE_KEYS`; that is spelling, not meaning.)
- **Three cost figures, one meaning each**: `cost` = what we charge (what quotas and
  reports read), `computed_cost` = always our arithmetic, `upstream_cost` = always the
  counterparty's. A fallback to our own prices is stamped `own_prices_fallback` and is
  never silent.
- **A plugin returns facts and never computes money** (ADR 0032). Vendor quirks —
  header names, cost units, router-vs-provider — live in `gateway/plugins/`.
- **Redaction is ~90% of the CPU** and scales with prompt length; its detection cache
  is a per-process LRU, so adding workers lowers the hit rate (docs/performance.md).

## Conventions that differ from defaults

- Comments and commit messages carry **reasoning**, not diff summaries: say why,
  especially where the obvious approach was rejected; name the failure a decision
  prevents; record bugs found while building.
- Changing `KEYCLOAK_PORT` / `OVERLAY_ADDR` / `KC_HOSTNAME` needs the **gateway
  restarted too** — it reads OIDC discovery once at startup. Keycloak's advertised
  hostname must match however you reach it, and `iss` is part of user identity:
  a hostname change re-provisions every user as a new row with no memberships.
- The look is Material Design 3 and lives in one file: `packages/ui/src/tokens.css`.
  Components reference tokens, never literals — and the decorative half of MD3 is
  deliberately absent (no blur, glass, glow, or scale-on-hover).
