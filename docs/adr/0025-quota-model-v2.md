# 0025 — The quota model: stacked, multi-granularity, resettable

- Status: accepted, implemented (Stage 1 of Phase 2)
- Date: 2026-08-15, expanded the same day from an earlier draft that covered only
  calendar-period quotas. Nothing is built against it yet, so it is revised in place
  rather than superseded.
- Extends [0009](0009-quota-model.md); pairs with [0024](0024-billing-periods.md).

## Context

Enforcement and accounting are different mechanisms and both are required. Accounting
is settled: calendar months in a configured timezone ([0024](0024-billing-periods.md)).
This ADR is about enforcement.

The requirement is quotas at **several granularities at once** — "€1 every 6 hours
*and* €5 per week" — stacked, configurable, and **resettable**: an operator must be
able to put a specific quota's consumption back to zero at any time.

[Cortecs' budgets](https://docs.cortecs.ai/features/budgets) were the reference. Their
model: scopes of project, user and API key; daily, weekly and monthly windows resetting
at **UTC**; conjunctive enforcement; a hard block naming the breached budget and the
time until it resets. Two of their ideas are worth taking, one of their limits is worth
fixing, and one of their choices is worth improving on.

## Decision

### 1. Stacking is already the model, and stays

Rules are **conjunctive**: every applicable rule must pass. "€1 per 6 hours" and "€5
per week" are two rules, both evaluated, and either can refuse a request. This is
unchanged from [0009](0009-quota-model.md) and needs no new machinery — arbitrary
`window_seconds` already expresses any rolling granularity.

### 2. Windows: rolling *or* calendar, per rule

A rule carries exactly one of:

| Kind | Field | Semantics | For |
|---|---|---|---|
| rolling | `window_seconds` | last N seconds, continuously moving, bucket-approximated | "€1 every 6 hours", burst control |
| calendar | `period` | current `day`/`week`/`month`/`quarter`/`year`, resets at the boundary | budgets that must reconcile with a report |

Calendar boundaries use **the same function as reporting**, in
`GATEWAY_BILLING_TIMEZONE` (default `Europe/Rome`). Not UTC — this is where we improve
on Cortecs. A budget that resets at 01:00 or 02:00 on the 1st, depending on the season,
is a support question, and a monthly budget that disagrees with the monthly report is
worse.

Calendar counters need no buckets: one key per period label
(`q:user:{id}:cost:p2026-08`), `INCRBY` to accumulate, one `GET` to read, and the key
changes name at the boundary so the reset needs no scheduling. Exact rather than
approximated, which is what lets a budget reconcile with a report.

### 3. Scopes: global, group, user, api_key — all explicit

`api_key` joins global, group and user. `usage_records.api_key_id` already exists, so
this is an enum and a lookup, not a schema change. "This CI key gets €5 per month" is
a real need and was previously inexpressible.

**`scope_id` stays mandatory for every non-global scope.** A rule names exactly what it
governs, or it is a global rule.

An earlier draft proposed `scope_id IS NULL` meaning "each entity individually",
copying Cortecs' "shared limit applied individually to every user". It was rejected:
the failure mode is a rule that looks narrow and is not, and over-scoping a spending
cap is discovered by someone being unable to work.

The cost, stated plainly: **a new user has no per-user limit until someone writes one.**
They are not unbounded — a global rule caps total spend, and a group rule caps their
group — but the per-user ceiling has to be created deliberately. Explicit over
implicit, chosen knowingly.

A pleasant consequence is that there is no precedence rule at all. Every rule is
specific, every applicable rule applies, the tightest wins, and
[0009](0009-quota-model.md)'s refusal of "most specific wins" survives untouched. The
resolution step that would have expanded defaults and computed shadowing — the place
the first subtle bug was going to live — simply does not exist.

### 4. Resets are watermarks, not counter mutations

The feature Cortecs lacks, and the one with a trap in it.

The obvious implementation — set the counter to zero — is **wrong here**, and provably
so: counters are a rebuildable cache, and the gateway already recomputes them from the
ledger when it finds the cache cold ([0006](0006-counter-store.md)). Zero the counter,
restart Valkey, and the rebuild faithfully restores the spend. The reset silently
evaporates. This is the same class of bug as the cold-cache leak already found and
fixed once.

So a reset is a **fact stored in the database**:

```
quota_resets(id, rule_id, effective_at, created_by, reason, created_at)
```

Resets take effect **immediately**; `effective_at` records when the reset happened, not
a schedule. Scheduling was considered and left out — a watermark supports a future
instant trivially, but nothing needs it yet, and an external scheduled call to the
endpoint covers it if that changes.

and consumption for a rule becomes

```
usage in [ max(window_start, latest_reset.effective_at), now )
```

Properties that fall out of this:

- **It survives cache loss.** Rebuild-from-ledger honours the watermark because the
  watermark is in the same database as the ledger.
- **It is auditable.** Zeroing someone's spending cap is a financially meaningful act;
  who did it, when, and why are recorded. A reset with no attribution is not a feature.
- **It works identically for rolling and calendar rules.** One expression, both kinds.
- **It cannot corrupt billing.** This is the important one. `usage_records` is never
  touched, so *a quota reset does not change what the monthly report says was spent*.
  Enforcement and accounting stay separated exactly as [0024](0024-billing-periods.md)
  requires. Implementing reset by deleting or adjusting usage rows would silently
  falsify the chargeback report — which is precisely the failure this design forecloses.

Cache invalidation comes free: **include the reset watermark in the counter key**, the
same trick the period label uses.

```
q:user:{id}:cost:p2026-08:r1755300000
```

A reset changes the key name, so the old value is orphaned and expires on its own TTL.
No deletion, no race, no partial state.

## Consequences

- Migration: `limit_rules.window_seconds` becomes nullable, `period` is added with a
  CHECK enforcing exactly one of the two, `api_key` joins the scope enum, and
  `quota_resets` is created. The scope CHECK is unchanged — `scope_id` stays mandatory
  for non-global scopes. Existing rules are all rolling and unaffected.
- `ScopeRef`'s existing validation (raise when a non-global scope has no id) is correct
  as written and stays.
- Rule evaluation gains no resolution step: every rule names its target.
- Error messages should name the breached rule *and* the time until it resets, as
  Cortecs does. For calendar rules that time is exact rather than the bucket-derived
  estimate used for rolling windows.
- Deliberately still absent: soft limits, alerts and graduated tiers. A quota refuses;
  informing someone before they hit it needs a notification path that does not exist.
- A rule created part-way through a period must have its counter **seeded from the
  ledger** (`QuotaEngine.seed_rule`). This was missed in the design and found against a
  live stack: the counter for a new rule starts empty, and an empty counter answers zero
  with confidence, so "EUR 5 this month" created on the 20th would enforce the wrong
  ceiling for eleven days. The cold-cache rebuild does not cover it — the cache is not
  cold, only the rule is.
- `current_value` on the admin API reads the **counters**, not the ledger. Reading the
  ledger would display a number the enforcement path never consults, so an operator
  could see room in a budget that the next request is refused for. The displayed figure
  is the deciding figure, reset watermark included.

## Not adopted from Cortecs

UTC resets, for the timezone reason above; and a fixed daily/weekly/monthly menu, since
arbitrary rolling windows are already supported and "every 6 hours" is a real ask.
