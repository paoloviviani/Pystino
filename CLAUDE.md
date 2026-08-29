# Working on this repository

A self-hosted LLM platform, built and run by its author at personal expense.
Read this before changing anything; it is the context that is not recoverable
from the code.

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
4. **The dev stack goes on a routable address only with the proxy overlay.**
   The rule used to be "never", and the reason was a list of four things, not a
   principle: Keycloak runs `start-dev` with `admin`/`admin`, there is no TLS,
   the session cookie is not `Secure`, and the seeded passwords are in the repo.
   The base compose files bind `127.0.0.1` because all four are true of them.
   `docker-compose.proxy.yml` closes all four — Caddy terminates TLS, and three
   required, defaultless variables replace the admin password, every seeded
   user's password and the OIDC client secret on every `up`
   ([ADR 0035](docs/adr/0035-public-tls-exposure.md)); it is still not a
   production deployment, and that ADR says exactly why. Without it, reach the
   stack over the SSH tunnel or `docker-compose.overlay.yml`, which publishes on
   a private WireGuard-mesh address — see the README. **Nothing else may be
   published.** `scripts/test_public_tls_live.py` is what checks that, by
   requiring every other port to be refused on this host's routable address.

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
apps/gateway     the gateway: /v1 proxy surfaces, /api management, console hosting
apps/console     React admin SPA, served by the gateway at /console
apps/chat-api    the chat backend: conversations, and the loop that produces them.
                 Imports nothing from gateway — it is a /v1 client (ADR 0040)
apps/web         the chat SPA, served by chat-api at /chat
packages/ui      design tokens and primitives, shared with the Phase 3 chat app
packages/shared-py  detection contract and the deterministic placeholder scheme
services/redaction  Presidio behind a swappable contract, out of process
deploy/compose   the stack: base + smoke + keycloak + redaction + chat + proxy overlays
deploy/caddy     the TLS reverse proxy's one config file, for both configurations
scripts/         live checks against a running stack (see below)
docs/adr/        37 ADRs. Read the index; they are the design record.
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
- **The look is Material Design 3, and it lives in one file.**
  `packages/ui/src/tokens.css` carries the palette, the geometry and the type,
  and components reference tokens and never literals — the restyle in
  [ADR 0042](docs/adr/0042-material-design-3.md) was that file plus two
  components, and the Bauhaus pass before it ([ADR 0034](docs/adr/0034-bauhaus-design-language.md),
  superseded) was the same. Three things worth knowing: **the tonal ground is
  inverted from MD3's own** — the page is tinted and a card is white, because
  this console is tables of figures and figures read best on white; **green is
  back**, so `ok` is green, `accent` is the purple, and the black-badge
  workaround ADR 0034 needed is gone with the constraint that caused it; and
  **the decorative half of Material You is deliberately absent** — no blur
  shapes, no glass-morphism, no glow or scale on hover. Roboto is vendored under
  the Apache License 2.0, licence beside it in `packages/ui/src/fonts/`.
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
- **Four things bite anything served behind the TLS proxy** (all of them found
  building it, all recorded in [ADR 0035](docs/adr/0035-public-tls-exposure.md)).
  **SNI may not carry an IP address**, so an address-only deployment offers no
  certificate at all until `default_sni` names one — every handshake fails with a
  TLS "internal error" and nothing above debug in the log. **uvicorn trusts
  forwarded headers from `127.0.0.1` only**, and the proxy arrives from the
  compose network, so without `FORWARDED_ALLOW_IPS` the app believes every
  request is http; the only place that shows is the post-logout URL built from
  `request.base_url`, which Keycloak then refuses with a 400 after a login that
  worked. **Keycloak's management interface inherits `KC_HTTP_RELATIVE_PATH`**,
  so moving Keycloak under `/kc` moves `/health/ready` with it and the healthcheck
  fails against a perfectly healthy container — `KC_HTTP_MANAGEMENT_RELATIVE_PATH`
  pins it. And **`iss` is part of a user's identity**: users are keyed on
  `(issuer, subject)`, so changing `KC_HOSTNAME` re-provisions everyone as new
  rows with no memberships at their next login.
- **An abandoned stream is billed from our price table, and the report blames
  the provider for it.** Found reconciling this deployment against Cortecs'
  dashboard on 2026-08-25. Their console said 34 requests / 244.3K tokens /
  €0.02; the ledger said 39 / 295,873 / €0.025510. Nothing was lost — the 34
  `upstream_exact` rows matched Cortecs exactly, to the token and to
  €0.019410. The other five were all `status=client_disconnected`,
  `upstream_status=200`, streamed: Cortecs served them for 2–15 seconds and the
  client hung up before the terminal SSE frame, so **no usage arrived and no
  reported cost did either**. Three consequences, none of them visible on the
  screen: tokens are counted locally (`usage_source=estimated`), cost falls back
  to our prices (`cost_source=own_prices_fallback`) on a provider configured to
  pass through, and `_reconciliation` — which requires `upstream_cost IS NOT
  NULL` on both sides, correctly — excludes them, so the drift row reads "34
  requests, ours equals theirs" beside a total covering 39. The one sentence
  that does mention them is wrong about why: `_disclosures` says *"the provider
  did not report usage"* when the row itself records that the client left. That
  wording is what sends an operator to the provider's dashboard to look for
  requests that were never missing.
- **The demo user's cap is EUR 1/hour and the fake upstream bills 1M tokens per
  request.** Running several live scripts back to back exhausts it legitimately;
  they report that as skipped. Flushing Valkey alone does not reset it — the
  counters rebuild from `usage_records`, so clear both.

## Verifying a change

```bash
uv run ruff check . && uv run mypy apps/gateway/src services
uv run pytest -q                       # 952 gateway + chat-api tests, SQLite
pnpm -r test                           # packages/ui + console + web
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
./scripts/test_bearer_tokens_live.py # OIDC access tokens on /v1, real Keycloak
./scripts/test_chat_live.py         # login, a streamed turn, and the ledger row
./scripts/test_public_tls_live.py   # only with the proxy overlay: TLS, the
                                    # rotated credentials, and that nothing else
                                    # is on a routable address
```

With the proxy overlay the live scripts need the deployment's own variables and
Caddy's CA, because they verify the certificate rather than skipping
verification:

```bash
docker compose ... exec proxy cat \
  /data/caddy/pki/authorities/local/root.crt > deploy/tls/caddy-root.crt
set -a; . deploy/.env; set +a       # PUBLIC_HOST, HTTPS_PORT, the seed password
./scripts/test_oidc_flow.py
```

Sourcing `deploy/.env` is also what points them at the https origin: signing in
only works there, because the gateway sends exactly one `redirect_uri` and the
realm has exactly that one registered.

**Run the live scripts.** More than half the serious bugs in this project's
history were only findable against the running stack: a counter seeded at zero,
a migration given the wrong environment, a 500 on `/v1/models` that every unit
test passed through.

The `130.192.84.52` host has 3 GB of RAM and 2 cores. The compose stack plus a
`pnpm test` will swap there, and the symptom is tests that fail having done
nothing wrong — check `free -g` before believing a frontend failure.
`130.192.84.103` has 14 GB and 5 cores and does not have this problem.

## Deployment

Development host `130.192.84.52`, console over an SSH tunnel — the README's
"Reaching the console from another machine" section has the command and why
both ports must match. A second VM, `130.192.84.103`, runs the same stack behind
the proxy overlay at <https://130.192.84.103:8443/console>; note that its
firewall permits **8443 and 22 and nothing else**, which is why that deployment
serves one origin with Keycloak under `/kc` rather than two ports, and why the
Let's Encrypt configuration cannot be used there until 80 and 443 are opened.
Git remote is GitHub (`paoloviviani/Pistin`, since 2026-08-29; GitLab
`viviani/ai-stack` was the original home); the tokens are in `.gitlab-token` and
`.gh-token`, which may be sourced but should not be read. Both are gitignored by
name and glob.

## Where the project is

The gateway and its console are the substance and are done through Phase 2:
gateway, redaction, providers, quotas, reporting, five `/v1` surfaces, the admin
console. `docs/phase-2-plan.md` records what was planned and what was added
afterwards, including the bugs each addition surfaced. The chat (Phase 3) runs
but is early — M1's foundation only — and is not the current deliverable.

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
  **What is redacted is now an admin decision**
  ([ADR 0037](docs/adr/0037-redaction-policy.md)): a per-entity policy — four
  modes on two axes (what the model sees, what the reader gets back), a
  threshold per type, an allow-list — stored as JSON on that same append-only
  row and picked up by the same poll. Two things easy to get wrong: the policy
  is applied **before** overlap resolution, or a discarded `URL` span takes the
  `PERSON` it overlapped with it; and a reason is required only when a change
  protects *less*. The default protects everything the engine finds except
  `URL`, `DATE_TIME`, `LOCATION` and `NRP` — which is what fixed *"Riassumi le
  notizie del giorno da ilpost.it"* reaching the upstream as `<PERSON_…> le
  notizie del giorno da <URL_…>`, the English model calling the Italian verb a
  person at 0.85 and the news site a URL.
  Still to do: **scoping** — redaction is deployment-wide when it needs to be
  scopeable per model, provider, user or group — and **showing an operator which
  spans were replaced**, which is the feature that would have caught that bug in
  an afternoon. Read §4 and §5 of the plan first.
  The precedence rule to copy is the quota engine's, inverted — quotas
  are *all rules must pass*, redaction is *any applicable scope requiring it
  wins* — so adding a scope can only tighten. The doc carries the table shape,
  where it plugs into `_metered`, and why the first version has no exemptions.

Known open items, none of them blocking:

- **A stream settled at the moment the client disconnects loses the write.**
  Found on 2026-08-28 building the chat app, on the live stack. `body_iterator`
  in `routers/chat.py` sets `completed = True` and then, in its `finally`,
  awaits `metered.completed(...)` — a database write — *inside the request
  task*. A client that closes the connection the instant it reads the terminal
  `data: [DONE]` frame causes uvicorn to cancel that task mid-write: the
  connection is torn down with `CancelledError`, and the row stays
  `status=in_progress`, zero tokens, zero cost, for a request the provider
  served in full. Two rows in this deployment's ledger are exactly that.
  The disconnect branch of the same `finally` was already made
  cancellation-proof, with a comment explaining why — `spawn_finalisation`
  detaches the write into a task precisely because "we are very likely inside a
  cancelled task". The reasoning was never applied to the branch beside it.
  The chat service now drains the stream rather than breaking at `[DONE]`, which
  removes the common case, but any browser closing a tab at the wrong
  millisecond still reaches it. The fix is to route the `completed` branch
  through the same detached mechanism — and it needs a session that does not
  belong to the request scope, which is why it is not a two-line change and gets
  its own work with tests, per ground rule 3.

- **The estimated-usage disclosure attributes every case to the provider.**
  `_disclosures` in `reporting.py` cannot tell "the provider reported no usage"
  from "the client disconnected mid-stream", and says the former for both. The
  row knows: `status` is `client_disconnected`. Splitting the sentence by status
  — and saying that such requests are charged from our prices while the
  counterparty charged nothing — is the fix. See the trap above for the
  measurement it came from.

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
- **Phase 3 is under way.** `docs/phase-3-plan.md` records the design decisions —
  a separate service coupled to the gateway only through `/v1` and OIDC — and,
  as importantly, what was rejected and why. **M0 is done**: the gateway accepts
  OIDC access tokens on `/v1` when `GATEWAY_OIDC__ACCESS_TOKEN_AUDIENCE` names
  an audience ([ADR 0040](docs/adr/0040-bearer-tokens-on-v1.md)), which is also
  what the `opencode` device flow needs. **M1's foundation is done**: `chat-api`
  and the chat SPA, signing in against the same realm, streaming a turn through
  `/v1` as the person typing it. Regenerate and edit-and-resend are built: a
  regenerate is a contentless turn (`content: null` writes no user row) after
  the caller truncated from the reply, and an edit is truncate-from-the-prompt
  plus a fresh send — both because the transcript is one causal sequence, so
  `DELETE /chat/api/conversations/{id}/messages/{mid}` deletes a message and
  everything after it. No branch picker: forks would need server-side branch
  storage first. Still to come in M1: search, and titles from a model. Then
  assistants, RAG, MCP, voice, the code sandbox, the desktop app.

  Two things about the chat that are easy to get wrong. **It lives under
  `/chat`, in every deployment shape** — behind the proxy the gateway owns the
  root of the origin, so `/api` and `/auth` there are *its* management API and
  *its* callback; a chat that answered on those paths works alone and collides
  the moment it is proxied. And **the request id is minted by chat-api and sent
  to the gateway**, not read back: the gateway adopts an inbound
  `x-request-id` and never returns the one it used, so reading it back gives a
  null column — and that column is the only thing tying a transcript to what it
  cost.
