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
models (which can differ behind a router),
token counts, status, redaction provenance (`redaction_scope`,
`redaction_rule_id`) and three cost figures.

Statuses include `ok`, `client_disconnected` (the caller hung up before the
terminal frame — tokens are counted locally and stamped `estimated`),
`quota_exceeded` (never admitted) and `blocked` (refused by redaction policy —
zero tokens, zero cost, but counted, because "this deployment refused 400
prompts last month" is a number a data-protection review asks for).

## Running without a ledger

A deployment that wants routing, keys, model access control and redaction — and
does not care what anything cost — can turn the ledger off:

```bash
GATEWAY_ACCOUNTING__ENABLED=false
GATEWAY_QUOTA__ENABLED=false     # required; see below
```

Three things to know.

**Off means no row, not a row of zeros.** Nothing is written, because a row
saying a request cost nothing cannot be told apart from one where the
arithmetic failed — and keeping those distinguishable is what
`usage_source`, `cost_source` and `own_prices_fallback` exist for. `finalise`
returns before the price arithmetic as well, since every figure it computed
would be discarded.

**The report says so.** An empty usage report is otherwise indistinguishable
from an idle week, so `_disclosures` leads with a line stating that metering is
off, that the report covers nothing, and that requests are still served and
access still enforced.

**Quotas without a ledger are refused at startup**, and the error names the
other flag. Counters are a cache rebuilt from `usage_records` — which is why
flushing Valkey alone does not reset a limit — so with no ledger every counter
comes back at zero and *every ceiling silently passes*. A quota that refuses
nobody is worse than no quota, because somebody configured it and believes in
it. The asymmetry is deliberate: metering without quotas is fine, quotas
without metering is incoherent.

(Knowledge ingestion runs in the chat, metered through the embedding
endpoint.) The consequence this section is here for: ingestion bills through
the same path, so an unmetered deployment records no indexing spend either — and
a large ingestion run can cost more than the chat traffic it serves.

## Three cost figures, one meaning each

| Column | Meaning |
|---|---|
| `cost` | What **we charge**. Quotas and reports read this. |
| `computed_cost` | Always **our arithmetic** — tokens × our price table. |
| `upstream_cost` | Always **the counterparty's** figure, when one is reported. |

`cost_source` says which figure billed: our prices, or the counterparty's
reported cost (billing mode is a per-provider choice). Both figures are recorded in both
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
meaning, these differ only in spelling.

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

The quota engine is reserve-then-settle over stacked,
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
  not a gap.
- **Billing periods are calendar-based** (monthly, etc.) and are *not* rolling
  windows; a rolling window and a billing period answer different questions and
  the model keeps them apart.
- **Valkey holds the counters; it is never authoritative.** With Valkey gone,
  quotas fall back to the database and evaluate correctly, just more slowly.
  The counters rebuild from the ledger — which is also why flushing Valkey
  alone does **not** reset spent quota: clear both `usage_records` and the
  counters, or let the gateway restart and rebuild.
- An admin reset (`POST /api/admin/limits/{id}/reset`) zeroes a quota's
  *consumption* and requires a reason; billing history is never touched.

### Rebuilds replace; they never add

Every path that fills a counter from the ledger (the start-up rebuild, seeding
a new or re-activated rule, and reconcile) **sets** the counter to the
ledger's figure. A calendar counter is overwritten with the period's own TTL;
a rolling window has the buckets it reads deleted and the total written into the
newest one. Because it is an overwrite, running it twice, or on a counter that
is already right, changes nothing. (An earlier version added the ledger onto
the counter, so a restart counted everything since the rule was created twice.)

**Cold is read from the counters, not from a marker.** At start-up each active
rule's current window is read; a window whose counter reads zero is rebuilt from
the ledger and a window with a value is left alone. One worker is elected with a
60-second `q:rebuild-lock` so N workers starting together run one ledger query,
but nothing depends on the election. There is no permanent "seeded" marker any
more: it was a second fact to keep true alongside the counters and it was not.
An idle deployment costs one query per rule and writes nothing.

**What a replace can lose.** The ledger is read, then the counter written; they
are not one transaction and nothing is locked, so admission keeps working. An
increment landing between the two, and a reservation in flight at that instant
(counted by the counter, absent from the ledger until it settles), are
overwritten. The window is one ledger query long, the loss is bounded by the
traffic in flight over it, and it errs *low*, never high, so it cannot refuse a
request that should have been admitted. The next reconcile or the window rolling
over repairs it.

### Quota health and reconcile

`GET /api/admin/quota/health` (admin only, read-only) lists every rule's
current window: the counter value, the ledger total, the **difference**
(counter − ledger; positive means over-counted, which refuses early), the TTL of
the counter key, the number of rows still `in_progress` after 30 minutes
(excluded from the ledger total, and the first suspect for a difference), and
whether the rebuild lock is held. A counter the store cannot read is reported as
`null`, never as zero. The same figures are the **Quota health** panel on the
Quotas page of the console. Without a browser session, `docker compose exec
gateway pystino quota health` prints the same figures as JSON; it is what
cerea-deploy's `tools/diagnose` attaches to a report.

`POST /api/admin/quota/reconcile` (the panel's **Reconcile** button) recomputes
every *active* rule's current window from the ledger with the replace semantics
above and returns each window's before and after. It is recorded in the audit
trail as `quota.reconcile` with the counts of windows and of windows corrected,
and no figures. It is not run on a timer: drift should be rare now, and a
periodic overwrite would trade a rare, visible, repairable difference for a
steady small loss of in-flight traffic. Run it when the health panel shows a
difference.

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
