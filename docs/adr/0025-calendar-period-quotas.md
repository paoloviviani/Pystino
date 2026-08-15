# 0025 — Calendar-period quotas, sharing one boundary implementation with reporting

- Status: accepted (Phase 2)
- Date: 2026-08-15
- Extends [0009](0009-quota-model.md); pairs with [0024](0024-billing-periods.md).

## Context

Both are required: **enforcement** (cap what a user or group may spend) and
**accounting** (report what was actually spent each month). [0024](0024-billing-periods.md)
established that reporting uses calendar boundaries. Quotas, as shipped in Phase 1,
only understand rolling windows.

That leaves a trap. A "monthly" budget expressed with the existing machinery is
`window_seconds: 2592000` — a rolling 30 days. It never resets on the 1st, it decays
continuously rather than stepping, and its window edge is approximated to within one
bucket (12 hours at that width, by design — see [0009](0009-quota-model.md)).

So a monthly quota and a monthly report would disagree. The quota might say €47
consumed while the report says €51 for August, and **both would be correct**. That is
the worst kind of wrong: not obviously broken, just permanently arguable.

## Decision

`limit_rules` gains an optional **`period`** (`day`, `week`, `month`, `quarter`,
`year`) as an alternative to `window_seconds`. A rule carries exactly one of the two,
enforced by a CHECK constraint.

| Kind | Field | Semantics | Good for |
|---|---|---|---|
| rolling | `window_seconds` | last N seconds, continuously moving, bucket-approximated | rate limiting, burst control: "1000 requests per hour" |
| calendar | `period` | the current calendar period in the billing timezone, resets at the boundary | budgets: "€50 per user per month, resetting on the 1st" |

Both remain first-class. Rolling windows are the right tool for admission control
that should not care about the calendar; calendar periods are the right tool for
anything a human will reconcile against an accounting period.

### The mechanism that makes them agree

**One implementation of "when does August start", used by reporting and by quotas.**
Not two implementations kept in sync — one function, in the billing timezone from
[0024](0024-billing-periods.md), consumed by both. Agreement is then structural
rather than something to verify.

### Calendar counters are simpler than rolling ones

The bucket machinery exists to approximate a sliding window cheaply. A calendar period
needs none of it:

```
q:{scope}:{metric}:p2026-08        one key per period label
```

`INCRBY` to accumulate, a single `GET` to read, and the key changes name at the
boundary so the reset is automatic rather than something that has to be scheduled.
TTL runs to the end of the period plus slack. The value is **exact**, not
approximated, which is the other half of why it can be reconciled against a report.

Rebuild-from-ledger ([0006](0006-counter-store.md)) works identically: recompute the
period's total from `usage_records` between the same boundaries and write it to the
period key.

## The boundary hazard, named up front

A stream beginning at 23:59:50 on 31 August settles at 00:00:10 on 1 September. If
settlement applies to "the current period", August stays over-reserved by the
estimate and September under-counts by the actual — and both months are wrong in a
way that only shows up once a month.

**Settlement applies to the period the reservation used.** A `Reservation` already
carries the bucket grid it wrote to ([0009](0009-quota-model.md)); it carries the
period label for the same reason. This is the same class of bug as bucket drift, with
the same fix.

## Consequences

- A migration: `window_seconds` becomes nullable, `period` is added, and a CHECK
  enforces exactly one. Existing rules are all rolling and are unaffected.
- The quota engine evaluates a mix of both kinds in one pass; nothing about the
  reserve-then-settle flow or the overrun policy changes.
- The console can show a budget the way an operator thinks about it — "€31.40 of €50
  this month, resets in 12 days" — rather than "€31.40 in the last 30 days", which is
  true and unhelpful.
- Timezone matters here as much as it does for reporting. A month boundary computed
  in UTC resets an hour early or late depending on DST, and a budget that resets at
  01:00 on the 1st is a support question.
- Rolling windows are not deprecated. Removing them would leave no way to express
  "1000 requests per hour", which is a different and equally real need.
