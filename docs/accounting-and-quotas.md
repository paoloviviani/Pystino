# Accounting and quotas

A wrong answer here is a wrong invoice, not a stack trace — which is why this
logic has the strictest tests in the repository and why its conventions are
written out at length. Every request that passes through a `/v1` route goes
through the same pipeline in `routers/_metered.py`:

**resolve → reserve → record → settle**

1. **resolve** — authenticate the caller (API key or OIDC bearer), load user,
   billing group and model (a handful of SELECTs; the whole metered request is
   pinned at 5 SELECTs and 2 writes by `test_query_counts.py`).
2. **reserve** — ask the quota engine whether this request may proceed *before*
   the upstream call, and tentatively debit its estimate.
3. **record** — write the in-progress ledger row *before* the upstream call, so
   a crash mid-request is visible.
4. **settle** — when the response (or its terminal SSE frame) arrives, count
   the tokens, compute the cost, and finalize the row.

That ordering is the whole design. Each piece exists because the naive version
fails in a specific, boring, expensive way.

## The ledger

Every admitted request writes one row to `usage_records` — before the upstream
call, not after. A request the quota refuses (a `429` with `code:
quota_exceeded`) writes none: it never reached a provider and cost nothing. The row carries the caller, the billing group (see
[Billing groups](billing-groups.md) for what decides it), the requested and served
models (which can differ behind a router),
token counts, status, redaction provenance (`redaction_scope`,
`redaction_rule_id`) and three cost figures.

The statuses are `in_progress` (written before the upstream call; a row that
stays there was never settled), `completed`, `client_disconnected` (the caller
hung up before the terminal frame — tokens are counted locally and stamped
`estimated`), `upstream_error`, and `blocked` (refused by redaction policy —
zero tokens, zero cost, but counted, because "this deployment refused 400
prompts last month" is a number a data-protection review asks for).

**Requested by way of an alias.** `model_id` and `model_name` are always the
model that served the request, even when the caller sent a [model alias](model-aliases.md);
`requested_model` keeps the name they sent, and is null for an ordinary request.
Billing follows the target — prices, quotas and reports read the served model —
and the alias is kept beside it so a report can say "via alias" and a retarget
cannot rewrite the row. It is a copy of a string, not a foreign key, so an
alias that is deleted or retargeted leaves the ledger readable.

**Fallback attempts.** An alias with several targets can try more than one for a
request. Each attempt is its own row under the same `request_id`, numbered in
`route_attempt`: a failed attempt is `upstream_error` at zero cost against the
target that failed, and the attempt that served is billed to the target that
served it. A requests limit counts every attempt. See
[routing policies](model-aliases.md#routing-several-targets-per-alias).

## The request log

The reports add rows up; the request log lists them. It shows one line per
request, newest first: time, person, billing group, the model asked for and the
model that served it (the alias and its target when an alias was used), tokens in,
out and cached, cost, latency, status and source (`api_key` when a gateway API
key authenticated the request, `token` for a sign-in token or session). A request
that fell back to another target of an alias has one line per attempt, with the
attempt number.

**It never carries content.** The listing reads named metadata columns only.
`assistant_text` and `error_message` are not selected, so neither the reply nor a
provider error that echoed the prompt can reach the API, the console or the CSV.

| Route | Who | Notes |
|---|---|---|
| `GET /api/admin/requests` | administrators | Every request. Filters: `period` or `start` and `end` (as for the reports, default the current month), `user_id`, `group_id`, `model`, `status`. |
| `GET /api/admin/requests.csv` | administrators | The same filtered set as CSV. |
| `GET /api/me/requests` | any signed-in person | Only the caller's own requests. Same filters except `user_id`, which is ignored: the user is fixed to the caller. |
| `GET /api/me/requests.csv` | any signed-in person | The caller's own, as CSV. |

Both listings use the standard pagination envelope (`items`, `total`, `limit`,
`offset`; 50 rows by default, 200 at most). `model` matches either the alias the
caller asked for or the model that served the request. `status` is one of
`in_progress`, `blocked`, `completed`, `client_disconnected`, `upstream_error`.
Unlike the reports, the log includes `in_progress` rows, so a request that never
settled is visible.

The CSV writes cost as a plain decimal string, never a float or scientific
notation, and prefixes a text cell that starts with `=`, `+`, `-` or `@` with an
apostrophe so a spreadsheet does not run it as a formula. An export holds at most
100,000 rows; a larger set is refused with `export_too_large` rather than cut, so
narrow the period or add a filter. In the console, administrators find it under
**Requests**, and everyone has **Your requests** in the sidebar.

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
  *consumption*; a reason is optional and recorded in the audit log when given.
  Billing history is never touched.

### Group rules: the total, or each member

A group rule caps **who** its budget belongs to, chosen when the rule is
created (`applies_to`; immutable like the scope and window, because either
change would abandon the consumption already counted):

- **`aggregate`** (the default, and every rule that predates the choice): one
  budget for the group's total. Every request *billed to* the group counts,
  by all members together — including an API key's spend, which counts for
  the key's owner as a member.
- **`each_member`**: one budget per member of the group. A request billed to
  group G by user U counts against U's own share and nothing else: U's usage
  billed to other groups does not count, and other members' usage does not
  count. Spend through an API key counts against the key's owner. A member
  is a holder of a membership in the group; a request that has no user at
  all (no path reserves one today) would skip per-member rules, because
  there is nobody to attribute the spend to.

Where both kinds exist on one group, **both are enforced** — a request must
pass every rule that matches it, as everywhere. Neither kind raises the
other: a per-member rule does not lift the group ceiling, and the group
ceiling does not absorb a member's overrun.

A worked example, on one group with a €500 aggregate rule and a €50
per-member rule, both monthly:

| Situation | Aggregate (€500/month) | Per member (€50/month) |
|---|---|---|
| Ada has spent €30 this month, Bill €20 | group total €50 — fine | each share fine |
| Ada reaches €50 | still fine (group at €50) | **Ada refused**, Bill unaffected |
| The group together reaches €500 | **everyone refused** | irrelevant once the aggregate refuses first |
| Ada bills €40 of her spend to a *different* group | does not count here | does not count against her share here |

A member can see their own share on the usage page (`/v1/pystino/usage`,
`/api/me/limits`): a per-member rule appears as *their* limit with *their*
own consumption, marked `"applies_to": "each_member"`. It never shows
another member's spend, and a 429 for a per-member rule says the member's
share ran out — a 429 for an aggregate rule says the group's total did.

Operationally, a per-member rule is one counter key per member: the health
view and **Reconcile** expand to one row per member (each member's budget is
enforced independently, so drift is per member too), and seeding a new rule
primes the members who already spent in the window. The rules list carries
no single consumption figure for a per-member rule — the per-member figures
live in the health table.

### Everyone's own ceiling: a global rule per user

A **global** rule has the same choice (`applies_to`, immutable, and only
offered at global scope):

- **`aggregate`** (the default): one budget for everything the gateway serves,
  all users together.
- **`each_user`**: one budget **per person**, counted over everything that
  person spends: through every group they bill to, personal usage, and API
  keys they own (a key's spend is its owner's). Nobody needs a rule of their
  own and nobody needs configuring: a user created tomorrow is capped from
  their first request. "Each person may spend at most €20 per month" is one
  rule.

A per-user rule counts under the person's ordinary **user counter**,
`user:<id>`, the same one an explicit `scope=user` rule for that person reads.
The two mean the same quantity (that person's total spend), so for the same
metric, window and reset they are one counter: one reserve, one settle and one
refund per request however many such rules apply, and seeding or reconciling
either repairs both. Counters stay apart where it matters, because a quota
**reset** is part of the counter key: resetting the global rule restarts every
person's count for it without touching an explicit user rule's counter, and
the other way round.

Interactions, all of them the ordinary *all rules must pass*:

- With a **group aggregate** or **per-member** rule: independent; any of them
  can refuse. A person can be inside their group share and over their personal
  ceiling (they also spend through another group), or the reverse.
- With a **global aggregate** rule: also independent. The aggregate caps
  everyone together, the per-user rule caps each person; whichever is reached
  first refuses.
- With an explicit **`scope=user`** rule for one person: both apply and the
  lower ceiling binds. A `scope=user` rule **cannot raise** that person above
  the global per-user ceiling, because no rule overrides another. Giving one
  person a *higher* limit than everyone else is not supported; see the open
  question below.
- A request with **no user** (no path reserves one today) skips per-user
  rules, as it skips per-member ones.

A refusal reads `global per user limit of 20 cost per month exceeded (current
20)`: the figure is the person's own total, never anyone else's. A person sees
the rule in `/api/me/limits` and `/v1/pystino/usage` as their own limit with
their own consumption (`"scope": "global"`, `"applies_to": "each_user"`), and
quota notifications (ADR 0052) for it go to the person whose budget it is: one
user's crossing never mails another user who asked to hear about the same rule.

Operationally the rule is one counter per user, and the cost of that is paid
in the maintenance paths, not on the request path (its query budget is
unchanged, which `test_query_counts.py` pins). Seeding a new rule, the cold
rebuild, **Reconcile** and the health view reach every user, and each does it
with one grouped ledger query per rule rather than one per person (seeding and
the cold rebuild first ask which users spent in the window). Health and
Reconcile list a row per user, including users with no usage, because drift
can be a counter that should be zero. The admin rules list carries no single
consumption figure for a per-user rule.

**Open question for the owner.** Because nothing overrides, an operator who
wants a per-person *exception that raises* the ceiling (say, one heavy user at
€100 where everyone else has €20) cannot express it: the explicit `scope=user`
rule only ever adds a tighter bound. The ways to get there would be an
exclusion list on the per-user rule, or an override rule kind with defined
priority; both are new semantics and deliberately not invented here.

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
the deploy kit's `tools/diagnose` attaches to a report.

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

One known imperfection, recorded rather than hidden: the reconciliation
summary counts only the reconciled rows (a total covering 39 requests can sit
next to a drift row about 34).

The estimated-usage disclosure names its cause. A request whose provider did
not report usage, and one whose client disconnected before the provider's final
usage report, are counted and explained separately
(`estimated_disconnected_requests` on each report row is the second kind), so
the report never blames the provider for a client that hung up.
