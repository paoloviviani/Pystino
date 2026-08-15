# 0024 — Billing periods are calendar-based, and are not quota windows

- Status: accepted
- Date: 2026-08-15

## Context

Phase 2 adds chargeback reporting: what each group and user spent, exportable so it
can be allocated against grants and cost centres.

The gateway already aggregates usage — `/api/admin/usage?window_seconds=86400` — and
it would be easy to build reporting on that. It would also be wrong.

## The distinction

**Quota windows are rolling.** "€10 per 86 400 seconds" means the last 24 hours,
continuously moving, and they are deliberately approximated with buckets
([0009](0009-quota-model.md)) because their job is to decide whether to admit the
next request. An answer that is accurate to within one bucket is fine for that.

**Billing periods are calendar boundaries.** January. Q1. A grant year. They must be
exact, they must not move, and two people running the same report for the same month
must get the same number.

A rolling 30-day window is never a month. A chargeback report built on one will never
reconcile with anyone's accounting, and the discrepancy will be small enough to be
argued about rather than obviously wrong.

## Decision

**Reporting queries `usage_records` directly over calendar boundaries. It does not
reuse the quota engine, its buckets, or its counter stores.**

- Periods are expressed as an explicit `[from, to)` half-open range, or a named
  period (`2026-01`, `2026-Q1`) resolved into one.
- Boundaries are computed in a **configured timezone**, `GATEWAY_BILLING_TIMEZONE`,
  defaulting to `Europe/Rome` rather than UTC. A foundation's January starts at
  midnight local, and a UTC boundary silently misallocates an hour of spend at each
  end of every month.
- Those local boundaries convert to UTC for the query, so the index on
  `(group_id, created_at)` is still used.
- **DST is why this is not a one-liner.** Europe/Rome crosses DST twice a year, so a
  month is not always the same number of hours and naive arithmetic drifts. Use
  timezone-aware date arithmetic, and test the March and October boundaries
  specifically.

## Two disclosures every report must make

- **Estimated versus measured spend.** `usage_source` already distinguishes
  `upstream_exact` from `estimated` ([0008](0008-accounting-model.md)). A report that
  silently blends them presents an inferred number as a measured one. Every total
  carries its estimated portion.
- **Erased users.** GDPR erasure sets `user_id` to NULL and leaves `group_id` intact
  ([0005](0005-persistence.md)), so group totals survive erasure but per-user
  breakdowns lose the row. Those rows are reported under an explicit
  "(erased user)" bucket rather than dropped, so a per-user report still sums to the
  group total. A report whose parts do not add up to its whole is worse than one with
  an awkward row in it.

## Consequences

- Reporting is a read path over the ledger, so it has no effect on quota enforcement
  and cannot be broken by counter-cache problems.
- Long periods over a large `usage_records` table will eventually want rollup tables.
  Not now: the composite indexes on `(group_id, created_at)`, `(user_id, created_at)`
  and `(api_key_id, created_at)` cover the queries, and a research foundation's
  volumes are far from needing pre-aggregation. Revisit when a monthly report takes
  more than a second.
- `usage_records.created_at` is the timestamp reporting uses — when the request
  started. For a stream that begins at 23:59 and ends at 00:01, the whole cost lands
  in the earlier day. That is a choice, and the alternative (`finalised_at`) is
  equally defensible; what matters is that it is consistent and written down.
- Period close-off, invoice numbering and provider reconciliation are explicitly out
  of scope. Chargeback reporting only.
