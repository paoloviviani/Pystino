# Accounting and quotas

A wrong answer here is a wrong invoice, not a stack trace — which is why this
logic has the strictest tests in the repository and why its conventions are
written out at length. Every request that passes through a `/v1` route goes
through the same pipeline in `routers/_metered.py`:

**resolve → reserve → record → settle**

1. **resolve** — authenticate the caller (API key or OIDC bearer), load user,
   billing group and model (3 SELECTs, pinned by a test).
2. **reserve** — ask the quota engine whether this request may proceed *before*
   the upstream call, and tentatively debit its estimate.
3. **record** — write the in-progress ledger row *before* the upstream call, so
   a crash mid-request is visible.
4. **settle** — when the response (or its terminal SSE frame) arrives, count
   the tokens, compute the cost, and finalize the row.

That ordering is the whole design. Each piece exists because the naive version
fails in a specific, boring, expensive way.

## The ledger

Every request writes one row to `usage_records` — before the upstream call, not
after. The row carries the caller, the billing group, the requested and served
models (which can differ behind a router — [ADR 0028](adr/0028-embeddings-and-served-model.md)),
token counts, status, redaction provenance (`redaction_scope`,
`redaction_rule_id`) and three cost figures.

Statuses include `ok`, `client_disconnected` (the caller hung up before the
terminal frame — tokens are counted locally and stamped `estimated`),
`quota_exceeded` (never admitted) and `blocked` (refused by redaction policy —
zero tokens, zero cost, but counted, because "this deployment refused 400
prompts last month" is a number a data-protection review asks for).

## Three cost figures, one meaning each

| Column | Meaning |
|---|---|
| `cost` | What **we charge**. Quotas and reports read this. |
| `computed_cost` | Always **our arithmetic** — tokens × our price table. |
| `upstream_cost` | Always **the counterparty's** figure, when one is reported. |

`cost_source` says which figure billed: our prices, or the counterparty's
reported cost (billing mode is a per-provider choice,
[ADR 0032](adr/0032-provider-plugins.md)). Both figures are recorded in both
modes, so a divergence is always reconstructable.

`own_prices_fallback` is the value that must never appear silently: a
pass-through provider reported nothing, so the row was charged from our price
table instead. It is stamped, not hidden. A client that hangs up mid-stream
always lands here — the provider served the tokens but sent no usage, so the
tokens are counted locally and billed from our prices.

**An unpriced model reserves nothing, so it has no cost ceiling at all.**
`unpriced_model_count` on the provider listing is the warning; treat it as an
open invoice waiting to happen.

### Two prompt conventions, and they are opposites

OpenAI's `prompt_tokens` **includes** cached tokens; Anthropic's
`input_tokens` **excludes** them. Each surface therefore has its own named
reader in `accounting/cost.py`. They must not be merged into one tolerant
parser: a tolerant parser silently double-counts (or under-counts) one of the
two vendors, and neither error is visible on a single request.

What *is* tolerated across names is **spelling**: there are four names for the
cache-write quantity (`_CACHE_WRITE_KEYS`), because vendors name the same
quantity differently. Reading only one would bill those tokens at the input
rate. The distinction from the rule above is the point — those differ in
meaning, these differ only in spelling. The measurements behind this are in
[Cache accounting findings](cache-accounting-findings.md).

## Money is a string, end to end

- The column type is `Numeric(24,12)` through a `Money` TypeDecorator; in
  Python and JSON, money is a string. `Numeric` round-trips zero as
  `Decimal("0E-12")` — a float anywhere in the path turns that into `0.0` and
  the ledger starts lying in the twelfth decimal.
- **Never parse an amount into a float**, including in the browser.
- Stored precision is twelve places and is real — it is kept. *Displayed*
  precision is at most three, defaulting to milli-units: `formatMoney` in
  `packages/ui`, `format_money_prose` in `gateway/types.py`. The default is
  **opt-out** (`{ exact: true }`), because when it was opt-in four of the five
  screens leaked twelve decimals by saying nothing. `€12.50` is not written
  `€12.500`, and a real amount **never renders as zero** — it reads
  `< €0.001`, because `€0.00` for genuine spend makes the ledger look broken.
- Admins reach full precision with the **Exact figures** toggle in the console
  identity menu (a `MoneyPrecisionContext`); a bare `formatMoney` call made
  outside `<Money>` has to read it by hand.
- All prices and quotas share one currency (`GATEWAY_BILLING_CURRENCY`). A
  model priced in anything else is **refused rather than converted**: a silent
  exchange rate produces invoices that look right and are wrong.

## Quotas

The quota engine ([ADR 0009](adr/0009-quota-model.md),
[ADR 0025](adr/0025-quota-model-v2.md)) is reserve-then-settle over stacked,
multi-granularity rolling windows: global, group, user and API-key rules can
all apply to one request, and **all of them must pass** — a permissive rule
must not raise a ceiling someone else set.

- **Why reserve:** without a reservation, N concurrent requests each read the
  same under-limit total and all proceed; the budget is exceeded by however
  much N was. The reservation is a tentative debit that settles into the real
  figure afterwards. Admission under concurrency is verified against real
  Valkey by `scripts/test_quota_race_live.py`, which drives 40 concurrent
  clients and gets exactly the arithmetic number admitted.
- **Overrun policy:** the request that crosses the limit is *admitted* (it
  reserved before anyone knew); the **next** one gets a 429. There is no
  mid-stream cutoff via `max_tokens` clamping — that is a deliberate choice,
  not a gap ([ADR 0009](adr/0009-quota-model.md)).
- **Billing periods are calendar-based** (monthly, etc.) and are *not* rolling
  windows; a rolling window and a billing period answer different questions and
  the model keeps them apart ([ADR 0024](adr/0024-billing-periods.md)).
- **Valkey holds the counters; it is never authoritative.** With Valkey gone,
  quotas fall back to the database and evaluate correctly, just more slowly.
  The counters rebuild from the ledger — which is also why flushing Valkey
  alone does **not** reset spent quota: clear both `usage_records` and the
  counters, or let the gateway restart and rebuild
  ([ADR 0006](adr/0006-counter-store.md)).
- An admin reset (`POST /api/admin/limits/{id}/reset`) zeroes a quota's
  *consumption* and requires a reason; billing history is never touched.

## Reconciliation

The reports can compare our ledger against a provider's own dashboard, row by
row. A row reconciles only when both sides have a reported figure
(`upstream_cost IS NOT NULL` on both) — which correctly excludes
`client_disconnected` rows, since the counterparty reported nothing for them.

Two known imperfections, recorded rather than hidden: the reconciliation
summary counts only the reconciled rows (a total covering 39 requests can sit
next to a drift row about 34), and the estimated-usage disclosure currently
attributes every unreported case to "the provider did not report usage" when
the row itself records that the client disconnected. The fix is small and
tracked; until then, read `status` before believing the sentence.
