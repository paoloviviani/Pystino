# Phase 2 — redaction and the console

Decided 2026-08-15. The chat frontend moved to Phase 3; Phase 2 is the Presidio
redaction engine plus a console covering billing, model pricing, user quotas and
usage reporting.

**Both halves of spend control are in scope, and they are different mechanisms.**
*Enforcement* caps what a user or group may spend and refuses requests past the cap;
*accounting* reports what was actually spent in a calendar month. Per-user and
per-group cost limits already work and are enforced today — what Phase 2 adds is
calendar-period budgets ([0025](adr/0025-quota-model-v2.md)), so a monthly cap
resets on the 1st and agrees with the monthly report by construction.

Architecture decisions behind this: [0023](adr/0023-admin-console.md) (the console),
[0024](adr/0024-billing-periods.md) (calendar periods), and the unchanged API
invariants in [0022](adr/0022-administration-surface.md).

## Sequence

The reporting API comes first because it is needed whatever the UI turns out to be,
and building it first means the console is assembled against a data layer that is
already proven rather than designed alongside it.

```
  Stage 1  Reporting API        ── backend only, unblocks everything
  Stage 2  Redaction engine     ── independent; closes the compliance gap
  Stage 3  packages/ui          ── design tokens and primitives
  Stage 4  Console SPA          ── consumes stages 1 and 3
```

Stages 1 and 2 are independent of each other and could run in parallel. Stage 2 sits
before the UI deliberately: until it ships, every prompt reaches the provider
unredacted, and that is a worse thing to be carrying than a missing screen.

---

## Stage 1 — Reporting API — **done**

Delivered in commits `cd584e1` (quota model v2) and `a2172bf` (reporting API, resets),
plus the counter-seeding fix below. 78 new tests; `scripts/test_reporting_live.py`
covers what SQLite cannot.

**Built:**

| | |
|---|---|
| `gateway/periods.py` | One implementation of "when does August start", shared by reporting and calendar quotas |
| Calendar quotas | `period` ∈ day/week/month/quarter/year, stacking with rolling windows |
| `api_key` scope | "this CI key gets €5 a month" |
| `quota_resets` | Resettable budgets — the feature Cortecs lacks — as a watermark in the counter key |
| `GET /api/admin/reports/usage[.csv]` | Named periods or explicit ranges, `group_by` over group/user/model/api_key/day/total |
| `GET /api/me/reports/usage[.csv]` | Same implementation, `user_id` pinned server-side |
| `POST /api/admin/limits/{id}/reset` | Reason required, audit trail at `GET .../resets` |
| `current_value` | Populated from the live counters |
| Disclosures | Estimated, unavailable, in-flight, `(erased user)`, `(no group)` |

**Where this diverged from the plan below, and why:**

* **No nullable `scope_id`, no shadowing matrix.** Withdrawn on the user's instruction:
  "scope id null gives an error, it's too easy to go over scope". A rule with no
  `scope_id` is now refused for every non-global scope. ADR 0025 revised accordingly.
* **`current_value` reads the counters, not the ledger** — the reverse of what item 5
  below proposed. Reading the ledger would show a number the enforcement path does not
  use, so a screen could read "€4 of €10" while the next request is refused. The
  displayed figure is now the deciding figure, watermark included; the two are asserted
  equal in `TestReportAndQuotaAgree`.
* **`group_by` takes one dimension, not combinations.** Cross-tabs are a UI concern and
  the console can issue two calls; the query builder stays one shape.
* **Pagination not done** (item 6). Deferred deliberately: it is the only item here the
  console cannot work without eventually, but no list is near a problematic size in this
  deployment and adding it now would be guessing at the console's paging model.

**Found by building it** — three bugs the design review had not caught:

1. `rebuild_into` dropped `period` and `reset_epoch`, so the cold-cache rebuild failed
   outright for calendar rules and was swallowed by its own error handler. A wiped
   cache handed every group a fresh monthly budget — the exact failure the rebuild
   exists to prevent.
2. Sessions use `expire_on_commit=False`, so an already-loaded `resets` collection was
   not refreshed by a later `selectinload`, making a reset invisible to enforcement in
   the same session.
3. **A rule created mid-period started its counter at zero** (caught only against the
   live stack, where the ledger already held €4.95). "€5 this month" created on the
   20th would have had the wrong ceiling for eleven days. `QuotaEngine.seed_rule` now
   primes the counter from the ledger at creation.

The original plan follows, unchanged.

---

Everything here is stack-independent. Verified gaps in the current API:

| Gap | Why it blocks the console |
|---|---|
| `LimitRuleResponse.current_value` is declared but never populated | A quota screen cannot show "€7.20 of €10" |
| Usage aggregates by **group only** | No per-user, per-model or per-key breakdown |
| No time series | No charts, no trend, no "spend by day" |
| ~~No pagination anywhere~~ | Closed after the plan — [0029](adr/0029-pagination.md) |
| No date ranges — only `window_seconds` | Cannot report on January ([0024](adr/0024-billing-periods.md)) |
| No CSV export | Finance cannot allocate against grants |

### Work

1. **Calendar periods.** `[from, to)` ranges and named periods (`2026-01`, `2026-Q1`)
   resolved in `GATEWAY_BILLING_TIMEZONE` (default `Europe/Rome`), converted to UTC
   for the query. Test the March and October DST boundaries explicitly. **This one
   function is then consumed by calendar quotas too** — that shared implementation is
   what makes budgets and reports agree ([0025](adr/0025-quota-model-v2.md)).
2. **Quota model v2** ([0025](adr/0025-quota-model-v2.md)). Four parts, in this order:
   - `period` alongside `window_seconds`, one or the other per rule, so "€1 every 6
     hours" and "€5 per calendar week" stack. Period-keyed counters
     (`q:user:{id}:cost:p2026-08`) rather than buckets: exact, self-resetting at the
     boundary, one GET to read.
   - `api_key` scope, and nullable `scope_id` meaning "each entity individually", so
     "every user gets €20 per month" covers people who join next month. Without it the
     default state for a new user is unlimited.
   - Explicit rules shadow the `scope_id IS NULL` default **of the same shape only**.
     This is the subtle part and needs its own test matrix, not examples.
   - `quota_resets` — a reset is a watermark in the database, never a counter
     mutation, or the cold-cache rebuild resurrects the spend it cleared. The
     watermark goes in the counter key so invalidation is free.

   Settlement applies to the period the *reservation* used, or a stream crossing
   midnight on the 1st corrupts both months.
3. **Aggregation endpoint.** `GET /api/admin/reports/usage` with `group_by` over any
   of group, user, model, api_key, day — and combinations, since "spend by model
   within group, by month" is the actual question finance asks.
4. **Self-service equivalents.** `GET /api/me/reports/usage`, same shape, scoped to
   the caller and the groups they belong to. This is what makes "everyone sees their
   own spend" real rather than admin-only.
5. **`current_value` on limit rules**, so quotas can be displayed against consumption.
   Reads the ledger, not the counter cache — the cache is an approximation for
   admission control, and a screen showing a budget should show the exact number.
6. **Pagination** on every list endpoint, with a stable sort.
7. **CSV export** on the reporting endpoints.
8. **Disclosures**: estimated-versus-measured split on every total, and an explicit
   "(erased user)" bucket so a per-user report still sums to its group total.

### Tests worth writing before the code

Period boundaries across DST; **that a calendar quota and a report for the same period
return the same number**; that a reset survives a counter-cache wipe and rebuild; that
a reset changes enforcement and leaves the billing report untouched; the default-vs-
explicit shadowing matrix (default only, explicit only, both, different metric,
different window, different scope); that a stream crossing a period boundary settles into the
period it started in; aggregation correctness against hand-computed fixtures;
that estimated rows are counted *and* disclosed; that a per-user report sums to the
group report for the same period; CSV escaping of a group named `Research, AI`.

---

## Stage 2 — Redaction — **done**

Built: `services/redaction` (Presidio behind the contract), `HttpDetectionRedactor`
in the gateway, an entry-point plugin registry, and
`deploy/compose/docker-compose.redaction.yml`. Verified end to end against the
running stack with `scripts/test_redaction_live.py`: the upstream receives
placeholders and never the PII, the same entity keeps its placeholder across turns,
and the caller gets the real values back — including through a stream sliced into
7-byte frames.

Decided during the work ([0026](adr/0026-pluggable-detection.md)): **detection is a
plugin, not a Presidio integration.** Three entry points — serve the HTTP contract,
extend this service's recogniser registry, or ship a package advertising
`llmp.redactors`. Placeholder derivation, substitution, restoration and fail-closed
behaviour stay in the gateway so no detector can get them wrong.

The Italian NER model is a build argument, off by default, because its weights are
CC BY-NC-SA 3.0 while everything else is MIT.

Three things only a running container revealed:

1. Presidio **drops** a recogniser whose language is not a registry language, so
   every Italian identifier was silently absent from the English-only image — the
   exact capability the licence decision was resting on. Fixed by re-registering
   the pattern recognisers under each loaded language.
2. `/healthz` was advertising entities from a hardcoded list while the registry
   held something else. It now asks the analyzer.
3. Presidio's default phone regions exclude Italy, so an Italian number came back
   as a `PERSON`.

Still open: latency under load is unmeasured, and the plan's `tail_size` question
answered itself — restoration only ever matches placeholders the gateway issued, so
the buffer is sized from their exact lengths rather than estimated.

The original plan follows, unchanged.

---

## Stage 2 — Redaction (as planned)

The interface, the deterministic placeholder scheme and the cross-frame buffering
already exist and are tested. `build_redactor("http")` raises `NotImplementedError`
on purpose; this fills it in.

1. `services/redaction` — FastAPI wrapping Presidio analyzer, `POST /detect`,
   returning spans only. Image from `ghcr.io/data-privacy-stack/presidio-analyzer`.
2. `HttpDetectionRedactor` in the gateway.
3. Italian and English. Presidio ships `IT_FISCAL_CODE`, `IT_VAT_CODE`,
   `IT_DRIVER_LICENSE`, `IT_IDENTITY_CARD` and passport recognisers with `it`
   language support, so Italian PII is largely covered without custom recognisers —
   it needs `it_core_news_lg` alongside `en_core_web_lg`.
4. Compose overlay and an end-to-end script in the style of `test_oidc_flow.py`.

**Two things to plan for rather than discover:**

- **Re-detection is O(n²) across a conversation.** On turn 40 the client resends all
  40 messages, and a naive implementation runs spaCy over the whole history every
  turn. Cache on a hash of the normalised message text. The placeholder derivation is
  deterministic and stateless, so a cache cannot change the result — it is purely an
  optimisation, which is what makes it safe.
- **`tail_size` must exceed the longest matchable entity**, or a match straddling an
  SSE frame boundary is missed. Presidio's entity set determines the number; it is
  not a guess.

Latency is the risk. spaCy is hundreds of milliseconds and every request pays it. The
out-of-process design protects the event loop but does not make it fast.

---

## Stage 3 — `packages/ui` — **done (minimal)**

Tokens (`tokens.css`) plus eight primitives: Button, Card, Table, Stat, Badge,
Notice, Spinner, Select, and a `Money` formatter. Nothing else — the warning
below about designing in a vacuum was taken literally, and a component arrives
when a real screen needs it.

**Styling is CSS custom properties + CSS Modules, not a utility framework.** The
deciding factor was coupling: components styled with Tailwind classes are inert
without Tailwind's generator, so every consumer — including the Phase 3 chat app,
which does not exist yet — would have to run and configure it. With CSS Modules a
component brings its own scoped styles and the consumer imports it. Plain CSS is
also readable in five years by someone who is not a frontend specialist, which
matters more here than terseness.

The palette is deliberately quiet — warm off-white, warm grey text, one muted
steel-blue accent — and there is **no dark theme**, on instruction. Adding one
later is a block of token overrides and nothing else.

---

## Stage 3 — `packages/ui` (as planned)

Shared React component library and design tokens, consumed by the console now and the
chat app in Phase 3.

Keep it to **tokens and primitives**: colour, spacing, typography, buttons, tables,
forms, empty states. Resist inventing chat-shaped components for an application that
does not exist yet — designing in a vacuum for an imagined second consumer is how
component libraries become the thing everyone works around.

---

## Stage 4 — Console SPA — **done**

React 19 + Vite 8 + TypeScript 5.9, served by the gateway at `/console`. The
slice proves everything novel about serving HTML from the gateway; the remaining
screens are repetition against an API that is already built.

**Done:** the Node build stage and `ARG INCLUDE_CONSOLE` (325 MB with the console,
323 MB without, from one Dockerfile); `GATEWAY_CONSOLE_ENABLED`; CSP, cache
headers and the SPA fallback; session-cookie auth with a redirect to
`/auth/login` on 401; and all six routes:

| Route | What it does |
|---|---|
| `/` | Own spend for a named period, by model/day/group/key, CSV, API keys |
| `/admin/reports` | Chargeback: period, six breakdowns, group and model filters, CSV, and what share of the figure is measured rather than inferred |
| `/admin/quotas` | Rules with live consumption as a meter, over-budget and nearly-spent states, create, delete, reset-with-reason, reset history |
| `/admin/models` | Catalogue with prices and grants, activate/deactivate, per-group access, provider discovery and selective import |
| `/admin/pricing` | Append-only price history per model, with scheduled future prices marked |
| `/admin/users` | Users, groups, keys, last login, enable/disable |

**Found while building it:**

* The SPA fallback registered before the asset mount swallowed every asset
  request, so the page loaded with no scripts and no server-side error. Order is
  now explicit and tested.
* `StaticFiles` strips its own mount prefix, so a path check for `assets/` never
  matched and hashed assets were being served with revalidate headers.
* TanStack Query's default retry sent a 400 three more times before showing the
  reader the message. Now no 4xx is retried — the request will be just as wrong
  next time.

**Two things the screenshots caught that the tests did not**, both now fixed and
both presentation problems the API cannot solve on its own:

* A rolling window rendered as `per 86400s`. The API reports raw seconds because
  that is what it stores; putting seconds in front of a person is the console's
  mistake, not the API's.
* A rule at 200% of its cap still showed a green **Active** badge — true, and
  useless. What that row means is that requests are being refused right now, so
  it now reads **Over budget**, with **Nearly spent** from 80%.

The users screen also shows each account's `issuer`. Identity is
`(issuer, subject)`, so two rows can share a name and an email and be different
accounts — which is exactly what the demo stack contains, left over from before
the Keycloak hostname fix. Worth knowing operationally: **changing the OIDC
issuer URL re-provisions everyone**, orphaning their keys and default billing
group.

**Pagination was still not done here.** It was closed afterwards — see the
section below and [0029](adr/0029-pagination.md).

TypeScript is pinned to **5.9.3**, not the 7.0.2 that `latest` now resolves to:
7.0 is the native-compiler rewrite and was five weeks old at the time. One line
to change when the ecosystem has caught up.

---

## Stage 4 — Console SPA (as planned)

React SPA, built into the gateway image, served same-origin so the existing session
cookie authenticates it. See [0023](adr/0023-admin-console.md).

### Routes

| Route | Who | Content |
|---|---|---|
| `/` | any authenticated user | own spend, own groups' spend, API keys, default billing group |
| `/admin/models` | admin | catalogue: create, edit, activate/deactivate; **discover** what the provider offers that we do not carry, and adopt with one click (API already built) |
| `/admin/pricing` | admin | price history per model, append a new price, schedule a future one |
| `/admin/quotas` | admin | rules with current consumption; budgets as "€31.40 of €50 this month, resets in 12 days"; which default each explicit rule shadows; a reset button with a required reason |
| `/admin/reports` | admin | spend by group/user/model/period, CSV export |
| `/admin/users` | admin | users, groups, keys, activity |

### Build and packaging

- `ARG INCLUDE_CONSOLE` gates the Node stage and the asset copy, producing `gateway`
  (headless) and `gateway-console` from one Dockerfile. The HTTP API is identical.
- `GATEWAY_CONSOLE_ENABLED` gates the static mount at runtime, so an operator can
  disable it without rebuilding and an assetless image cannot half-serve a UI.
- New to the gateway because it now serves HTML: a Content Security Policy, asset
  cache headers, and a SPA fallback route.

### Knock-on for Phase 3

The console is the account-management surface for the whole platform. The chat app
should **link** to it for key minting and spend rather than reimplementing them. Two
implementations of "mint an API key" is one too many, and the second is where the
security bug will be.

---

## Added after the plan — inference providers

Not in the original plan; asked for once the console existed. See
[0027](adr/0027-inference-providers.md).

Providers became records rather than environment variables: a `providers` table,
`models.provider_id` (not nullable), one HTTP client per provider built on demand,
and `POST /api/admin/providers/{id}/test` to check an endpoint before anything
depends on it. Migration 0003 turns the existing `GATEWAY_UPSTREAM__*` into a
`default` provider and points every existing model at it, so an upgrade changes
nothing observable.

API keys are encrypted at rest with Fernet under `GATEWAY_SECRET_KEY`
(comma-separated, so rotation is a rolling restart) and are **write-only** over
the API — a request returns only a hint like `sk-…4f2a`.

Model access became the union of group grants and new per-user grants, in one
predicate (`gateway/access.py`) that `/v1/models`, `/v1/models/{name}` and the
chat path all share. No denials: an explicit deny overriding a group grant makes
"why can this person not use that model" a question requiring a search.

**Found while building it:** the migrate job did not have `GATEWAY_UPSTREAM__*`,
so the backfilled provider pointed at the default Cortecs URL and dropped the
API key — the upgrade succeeded and quietly configured the wrong endpoint. The
compose files now give the migrate job the same upstream configuration as the
gateway, and the migration warns when it cannot encrypt the key it found.

## Added after the plan — embeddings, and what actually served a request

[ADR 0028](adr/0028-embeddings-and-served-model.md). Reading the reference
provider's documentation closely raised three things the plan had not.

`POST /v1/embeddings` is now served, with the same access check, redaction and
metering as a completion. Embedding inputs are redacted and the vector is of the
redacted text — deterministic placeholders keep a corpus self-consistent, so two
documents mentioning the same person still embed alike. Charging is input-only:
an embedding has no completion tokens. `models.kind` separates chat from
embedding models and each route refuses the other kind by name, pointing at the
one that would have worked.

Usage records now keep `upstream_model` and `upstream_provider` — what the
provider said served the request, which is not always what we asked for when a
provider fails over. The `model_substituted` flag is decided at write time
rather than by comparing the two columns later: `model_name` is our
client-facing name and `upstream_model` is the provider's, so those always
differ and comparing them would call every request a substitution. The report's
disclosures surface the count.

`stream_options.include_usage` became per-provider (`forward_stream_options`).
The reference provider returns usage on streamed responses unconditionally and
does not document the parameter, so asking for it is at best redundant.

**Found while building it:** a request that failed upstream before generating
anything was still billed for its estimated prompt. `resolve_counts` now returns
zero for that case, on both routes.

## Added after the plan — pagination and server-side search

[ADR 0029](adr/0029-pagination.md). Every management listing returns
`{items, total, limit, offset}` now, and takes `limit`/`offset` with a ceiling
of 200. Out-of-range is a 400 rather than a clamp, because silently returning a
different window is how a client comes to treat a truncated list as complete.

Aggregations are excluded on purpose: `/api/admin/reports/usage` and its CSV
return every row they summed. A truncated total is a wrong number that looks
like a right one, and the report exists to be reconciled against an invoice.

Filtering moved to the server along with it — `q` on users, models and groups,
plus `is_active` and `provider_id`. This was the actual motivation. The users
screen had a search box that filtered in the browser over whatever had been
returned, which reads exactly like a real search until the directory outgrows
one response, and then searches the first page and reports nothing found.

**Found while building it:** `PATCH /api/admin/users/{id}` answered by re-reading
the listing and picking its row out of it, which breaks the moment the listing
is a page. Two other lookups were quadratic in disguise — rendering a page of
models read *every* group and personal grant, and a page of users read every API
key — now both restricted to the ids on the page.

## Explicitly out of scope

Budgets with alerts (needs a notification path that does not exist), invoice
generation (needs period close-off and numbering), and upstream cost reconciliation
(needs provider usage APIs). Chargeback reporting and export only, per the decision
in [0024](adr/0024-billing-periods.md).

Still Phase 3 or later: the chat frontend, RAG, MCP, the code sandbox, the desktop
app, and the `opencode` device flow.
