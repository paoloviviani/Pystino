# 0025 — The quota model: stacked, multi-granularity, resettable

- Status: accepted (Phase 2, not yet implemented)
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

### 3. Scopes, including "each"

`api_key` joins global, group and user. `usage_records.api_key_id` already exists, so
this is an enum and a lookup, not a schema change.

More importantly, `scope_id` becomes nullable for the non-global scopes, with a
distinct meaning:

| `scope` | `scope_id` | Means |
|---|---|---|
| `global` | NULL | everyone, combined, one pool |
| `user` | a user id | that user |
| `user` | **NULL** | **each user, individually** |
| `group` / `api_key` | id or NULL | likewise |

The `NULL` form is the "shared limit applied individually" idea, and it is what makes
quotas safe by default: "every user gets €20 per month" covers people who join next
month without anyone remembering to write a rule for them. Without it, the default
state for a new user is *unlimited*.

### 4. An explicit rule shadows a default of the same shape

This needs care, because [0009](0009-quota-model.md) deliberately rejected precedence:
"most specific wins reliably surprises people when a user-scoped rule silently raises a
group ceiling."

That refusal stands. What is added is much narrower:

> A rule for a **specific** entity shadows the **`scope_id IS NULL` default** of the
> same `(scope, metric, window)`. Rules of *different* scopes, metrics or windows
> continue to stack conjunctively, with no precedence between them.

Why the narrow version is necessary: a default of €20 per user and a conjunctive-only
model make it impossible to *grant* anyone more than €20 — the default keeps capping
them. Shadowing lets "every user gets €20, except alice who gets €100" be expressed at
all.

Why it does not reintroduce the thing 0009 rejected: shadowing operates only between a
default and an explicit rule **of identical shape**, on the same counter. It never lets
a user rule loosen a *group* ceiling, or an hourly rule loosen a weekly one. Those
still stack, and the tightest still wins.

The console must show this plainly — an entity's effective rules, and which default
each one shadows — because a rule that silently does nothing is worse than no rule.

### 5. Resets are watermarks, not counter mutations

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

- Migration: `limit_rules.window_seconds` becomes nullable, `period` is added, the
  scope CHECK relaxes to allow NULL `scope_id` on non-global scopes, `api_key` joins
  the scope enum, and `quota_resets` is created. Existing rules are all specific,
  rolling, and unaffected.
- `ScopeRef` currently *raises* when a non-global scope has no id; that validation
  inverts for defaults and must be reworked deliberately rather than deleted.
- Rule evaluation gains a resolution step — expand defaults to the entities in play,
  drop those shadowed by explicit rules — which is the first place a subtle bug will
  live. It deserves a test matrix of its own, not just examples.
- Error messages should name the breached rule *and* the time until it resets, as
  Cortecs does. For calendar rules that time is exact rather than the bucket-derived
  estimate used for rolling windows.
- Deliberately still absent: soft limits, alerts and graduated tiers. A quota refuses;
  informing someone before they hit it needs a notification path that does not exist.

## Not adopted from Cortecs

UTC resets, for the timezone reason above; and a fixed daily/weekly/monthly menu, since
arbitrary rolling windows are already supported and "every 6 hours" is a real ask.
