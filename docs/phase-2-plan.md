# Phase 2 — redaction and the console

Decided 2026-08-15. The chat frontend moved to Phase 3; Phase 2 is the Presidio
redaction engine plus a console covering billing, model pricing, user quotas and
usage reporting.

**Both halves of spend control are in scope, and they are different mechanisms.**
*Enforcement* caps what a user or group may spend and refuses requests past the cap;
*accounting* reports what was actually spent in a calendar month. Per-user and
per-group cost limits already work and are enforced today — what Phase 2 adds is
calendar-period budgets ([0025](adr/0025-calendar-period-quotas.md)), so a monthly cap
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

## Stage 1 — Reporting API

Everything here is stack-independent. Verified gaps in the current API:

| Gap | Why it blocks the console |
|---|---|
| `LimitRuleResponse.current_value` is declared but never populated | A quota screen cannot show "€7.20 of €10" |
| Usage aggregates by **group only** | No per-user, per-model or per-key breakdown |
| No time series | No charts, no trend, no "spend by day" |
| No pagination anywhere | `/api/admin/users` returns everything |
| No date ranges — only `window_seconds` | Cannot report on January ([0024](adr/0024-billing-periods.md)) |
| No CSV export | Finance cannot allocate against grants |

### Work

1. **Calendar periods.** `[from, to)` ranges and named periods (`2026-01`, `2026-Q1`)
   resolved in `GATEWAY_BILLING_TIMEZONE` (default `Europe/Rome`), converted to UTC
   for the query. Test the March and October DST boundaries explicitly. **This one
   function is then consumed by calendar quotas too** — that shared implementation is
   what makes budgets and reports agree ([0025](adr/0025-calendar-period-quotas.md)).
2. **Calendar-period quotas.** `limit_rules.period` alongside `window_seconds`, one or
   the other per rule. Period-keyed counters (`q:user:{id}:cost:p2026-08`) rather than
   buckets: exact, self-resetting at the boundary, one GET to read. Settlement applies
   to the period the *reservation* used, or a stream crossing midnight on the 1st
   corrupts both months.
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
return the same number**; that a stream crossing a period boundary settles into the
period it started in; aggregation correctness against hand-computed fixtures;
that estimated rows are counted *and* disclosed; that a per-user report sums to the
group report for the same period; CSV escaping of a group named `Research, AI`.

---

## Stage 2 — Redaction

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

## Stage 3 — `packages/ui`

Shared React component library and design tokens, consumed by the console now and the
chat app in Phase 3.

Keep it to **tokens and primitives**: colour, spacing, typography, buttons, tables,
forms, empty states. Resist inventing chat-shaped components for an application that
does not exist yet — designing in a vacuum for an imagined second consumer is how
component libraries become the thing everyone works around.

---

## Stage 4 — Console SPA

React SPA, built into the gateway image, served same-origin so the existing session
cookie authenticates it. See [0023](adr/0023-admin-console.md).

### Routes

| Route | Who | Content |
|---|---|---|
| `/` | any authenticated user | own spend, own groups' spend, API keys, default billing group |
| `/admin/models` | admin | catalogue: create, edit, activate/deactivate |
| `/admin/pricing` | admin | price history per model, append a new price, schedule a future one |
| `/admin/quotas` | admin | limit rules with current consumption; budgets shown as "€31.40 of €50 this month, resets in 12 days" |
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

## Explicitly out of scope

Budgets with alerts (needs a notification path that does not exist), invoice
generation (needs period close-off and numbering), and upstream cost reconciliation
(needs provider usage APIs). Chargeback reporting and export only, per the decision
in [0024](adr/0024-billing-periods.md).

Still Phase 3 or later: the chat frontend, RAG, MCP, the code sandbox, the desktop
app, and the `opencode` device flow.
