# 0009 — Quota rules, rolling windows, and the mid-stream overrun policy

- Status: accepted
- Date: 2026-08-14

## Context

Rules must apply at global, group and user scope, over requests, tokens or cost, on
rolling windows. The brief asked for an explicit decision on what happens when a
request would exceed a limit mid-stream.

## The rule model

One `limit_rules` row is one (scope, scope_id, metric, window) with a limit value.
Rules are **additive: every matching rule must pass.** There is intentionally no
priority or override mechanism — "most specific wins" reliably surprises people when
a user-scoped rule silently *raises* a group ceiling.

`tokens` means `total_tokens` (prompt + completion). A `CHECK` constraint enforces
that global rules carry no `scope_id` and scoped rules do.

## Rolling windows: bucketed approximation

A true rolling window needs every event's timestamp, which is O(events in window) per
request. Instead the window is approximated by fixed-width buckets, and the buckets
overlapping the window are summed with one batched read.

Bucket width is derived from the window so a window never needs more than
`max_buckets_per_window` (default 60) keys. A 60s window gets 1s precision; a 30-day
window gets 12h buckets, so a monthly budget is accurate to within half a day of
traffic.

**The error is bounded and its direction is deliberate.** The oldest bucket is
included whole even though the window covers only part of it, so a total is
*over*-counted by at most one bucket's traffic. Over-counting refuses slightly early;
under-counting would let spend escape. For a quota, erring toward refusal is correct.

`Retry-After` is computed from the oldest bucket actually carrying traffic — that
bucket leaves the window `window_seconds` after it ends — and is never zero, because
`Retry-After: 0` invites a retry storm.

## Reserve-then-settle

**The part that is easy to leave out, and the reason the whole mechanism exists.**

Without a reservation, ten requests arriving in the same millisecond each read the
same under-limit total and each decide they are allowed, so a group with room for one
request runs ten. The reservation makes each request's *worst case* immediately
visible to its siblings; settling replaces the estimate with the truth.

- **Before** the upstream call: reserve `prompt_tokens + max_output_tokens` and the
  corresponding worst-case cost. Reservations round **up**, so an estimate is never
  accidentally generous.
- **After**: apply the difference between actual and reserved to the same bucket grid.
  The net effect on the window is exactly the actual usage.
- On any error path: release. An abandoned reservation keeps consuming a user's budget
  until its buckets age out.

Tested with ten concurrent attempts against a 1000-token ceiling at 400 tokens each:
between 1 and 3 are admitted, never all ten.

**Amended 2026-08-16 — the bound was not guaranteed, and now is.** The same
test admitted *five* on a loaded machine. `check_and_reserve` read the window
total, decided, and then incremented, with `await` points in between, so
concurrent callers could all read the same pre-reservation figure before any of
their increments landed. Reservations narrowed the window a great deal —
without them all ten passed — but did not close it, and it was *wider* against
a real counter store than the in-memory one, because the read is a network
round trip.

**Admission now reserves first and judges afterwards.** One store operation
applies the deltas and returns the resulting totals
(`CounterStore.apply_and_total`); the engine subtracts its own contribution back
out and compares *that* against the limit. Every caller therefore sees exactly
what the window held before it arrived, whatever order the loop or the network
puts them in. On Valkey it is a `MULTI`/`EXEC` — a plain pipeline is only
batching, and another client's `INCRBY` can still land between the writes and
the read.

The policy is unchanged, only the moment of the increment: a request is refused
when the window had *already* reached the limit, and an admitted request may
still overshoot by its own usage. A caller that turns out to be refused undoes
its own delta, so refusals leave nothing on the counter — otherwise fifty
refusals would bury the window and refuse the next legitimate request too.

Ten concurrent attempts at 400 against a 1000 ceiling now admit exactly three,
deterministically, and fifty attempts still admit exactly three and leave 1200
on the counter.

**Except on the database fallback.** That store counts settled ledger rows and
has nothing to increment, so a reservation there is invisible to other
in-flight requests. Overshoot while running degraded keeps the wider bound
already documented — `concurrent_requests × default_max_output_tokens` — and no
amount of atomicity at that layer would change it.

**A bug the tests found:** deltas whose estimate was zero were originally skipped, so
`settle` had nothing to correct and the *actual* usage for that metric was never
counted at all — which would have silently under-counted cost for every unpriced
model. Zero-unit deltas are now recorded (the store still skips zero-valued writes,
so it costs nothing on the wire).

## The overrun policy — the decision the brief asked for

**A request is refused when the window total has already reached the limit
(`current >= limit`). A request that is admitted always runs to completion.**

We never abort a stream mid-generation. The tokens are already spent upstream, so
truncating burns the money *and* destroys the answer, which is the worst of both.

Consequences of that choice:

- Overshoot is bounded by **one request's actual usage**, and the *next* request is
  refused.
- Because reservations are applied and read back atomically, that bound holds
  under concurrency too — see the amendment above for how, and for the one
  exception while running on the database fallback.

An optional stricter mode was considered and **not implemented**: clamping
`max_tokens` so the worst case fits the remaining budget, making the limit genuinely
hard at the cost of occasionally truncating a long answer near the budget edge. The
settings needed for it exist (`default_max_output_tokens`); implement it if a group
ever demands a hard ceiling.

## Failure behaviour

| Situation | Behaviour |
|---|---|
| Valkey unreachable | Fall back to exact PostgreSQL aggregates. Slower, correct. |
| Both stores unreachable | `fail_open=false` (default) → 503. Protect the budget. |
| Counter *write* fails | Logged, request proceeds. Counters are a cache; the ledger write is a separate transactional path. |

## Consequences

- Counters only exist for metrics some rule watches. A rule added later starts from an
  empty cache — which is why `DatabaseCounterStore.rebuild_into()` exists and why the
  database remains the source of truth.
- Rules are loaded per request with one indexed query. Safely cacheable with a short
  TTL if it ever shows up in a profile; correctness first.
- 429 responses use OpenAI's error envelope with `type: insufficient_quota`, so
  OpenAI-SDK clients report something readable instead of an opaque transport error.
