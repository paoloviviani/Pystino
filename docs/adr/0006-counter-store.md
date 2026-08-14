# 0006 — Valkey, not Redis, for quota counters

- Status: accepted
- Date: 2026-08-14

## Context

Quota enforcement needs a fast shared counter store so that N gateway workers see
each other's traffic. The assumption was Redis. Redis's licensing moved twice since
that assumption was formed, and so did what distributions actually ship.

Verified against redis.io/legal/licenses:

| Version | Called | Licence |
|---|---|---|
| ≤ 7.2 | Redis | BSD-3-Clause |
| 7.4–7.8 | Redis Community Edition | RSALv2 **or** SSPLv1 — neither OSI |
| ≥ 8.0 (May 2025) | Redis Open Source | RSALv2 **or** SSPLv1 **or** AGPLv3 |

So Redis 8+ *does* offer an OSI licence and is not barred by
[0001](0001-licensing.md). Two things still weigh against it:

- **Valkey** — the Linux Foundation fork of Redis 7.2.4, BSD-3-Clause, wire-protocol
  compatible — is now the *packaged default* in Fedora 42, Ubuntu 26.04 LTS, Debian
  13 backports and Arch. Installing "redis" from a current distribution increasingly
  gets you Valkey.
- AGPL-3.0 is **not** upstream-compatible with EUPL for incorporation or static
  linking (dynamic linking and separate processes are fine). We only speak to it over
  a socket, so it is legally fine — but choosing BSD-3 removes the analysis, and
  removes an open-core vendor from the dependency tree.

## Decision

**Valkey.** BSD-3-Clause, Linux Foundation governance. `valkey/valkey:8-alpine` in
compose.

**No code change:** it is wire-compatible, so the client library stays `redis-py`
(MIT — fine inbound) and the URL scheme stays `redis://`. The client choice is
independent of the server choice; `valkey-py` is a drop-in alternative if matching
the server's provenance ever matters more than the client's maturity.

## The more important decision: Valkey is never authoritative

**PostgreSQL is the ledger. Valkey holds counters as a rebuildable cache.**

Consequences of taking that seriously:

- `DatabaseCounterStore` computes exact quota totals straight from `usage_records`.
  It is both the **fallback** when Valkey is unreachable and the **test oracle** for
  the cache's semantics.
- `rebuild_into()` repopulates counters from the ledger after cache loss.
- If Valkey is down at startup the gateway still comes up, degraded, using the
  database fallback — it does not refuse to boot.
- If *both* stores fail, `fail_open` decides: default **closed** (refuse the
  request), because protecting the budget matters more than availability for a
  research foundation. Configurable.

One documented limitation of the fallback: it sees only *settled* usage, since
in-progress rows carry zero tokens. Concurrent in-flight requests are therefore
invisible to it in a way they are not to Valkey, so overshoot while running on the
fallback is bounded by `concurrent_requests * default_max_output_tokens` rather than
by one request. Acceptable for a degraded path; noted so it is not a surprise.

- Counters store **integers only**. `INCRBYFLOAT` uses long-double arithmetic and is
  not exact, and a monetary quota that drifts is one you cannot defend in an audit.
  Cost is scaled to integer nano-units (`COST_SCALE = 1e9`) at the boundary.

## Verified against a running stack (2026-08-14)

The fallback claim was not left as an assertion. With the full compose stack up,
Valkey was stopped mid-workload:

- `/readyz` reported `valkey: error: ConnectionError` while `status` stayed `ok` —
  degraded, not down, which is the intended distinction.
- A group already over its limit was **still refused (429)** with the correct total,
  computed from `usage_records` by the database fallback.
- A *different* group with budget remaining was **still served (200)**, so the
  fallback is genuinely evaluating rules rather than blanket-refusing.
- Restarting Valkey returned `/readyz` to `ok` with no gateway restart.

Valkey's own behaviour was confirmed too: a cost bucket held `1350000000` — integer
nano-units for €1.35, the exact settled total rather than an inflated reservation —
under key `q:group:<uuid>:cost:g1440:<bucket>` with TTL 89279s. Bucket width 1440s
is 86400/60 as designed, and the TTL is `window + 2 * granularity`.

### The serious bug this found: an empty cache is not a failed read

Restarting Valkey exposed a money leak that the fallback design does **not** cover, and
that no unit test would have caught because it is a gap in the reasoning rather than in
the code.

A *failed* read falls back to the database. An **empty** read does not — it answers
confidently with zero. So after Valkey was restarted without its counters, the cache
reported €0.90 for a group whose ledger said **€11.70 against a €10 ceiling**, and the
gateway served it. Restart Valkey, and every group silently gets a fresh budget.

`DatabaseCounterStore.rebuild_into()` had existed since the first draft for exactly this
— and nothing ever called it. Writing the mechanism is not the same as wiring it up.

Fixed by `QuotaEngine.rebuild_if_cache_is_cold()`, called from `init_app_state`. The
mechanism turns on a marker key whose presence *in the cache* is a proxy for "the cache
still holds our counters", because the two are lost together:

| Situation | Marker | Behaviour |
|---|---|---|
| Fresh or wiped cache | absent | exactly one worker rebuilds from the ledger |
| Gateway restarted, cache intact | present | no rebuild — counters are already right |
| Buckets aged out naturally | present | no rebuild — that traffic is outside every window |

`claim_once` (SET NX) elects a single worker, so N workers starting together produce one
rebuild rather than N additive ones — which would otherwise report N times the real spend
and refuse everyone.

Verified on the running stack: `flushall`, restart, and the log said *"counter cache was
cold and has been rebuilt from the ledger (2 rule scopes)"*; counters came back as exactly
10.35 and 11.70, and both over-budget groups were then refused with 429. Restarting again
with a warm cache changed nothing, confirming no double-counting.

Startup never fails on this: a gateway that will not boot is worse than one whose counters
are briefly optimistic.

**One further problem this exposed.** Every fallback logged a full stack trace, and the
store is touched three times per request (read, reserve, settle) while the outage
recurs on *every* request. Under load that turns a degraded dependency into a second
incident — the log volume — and buries the one line an operator needs. Fixed: the
first occurrence of each distinct failure logs in full, subsequent ones are
one-liners until a 60s interval elapses. Measured on the running stack, steady-state
logging during an outage fell from ~30 lines per request to 3, with every failure
still reported. Throttling is per worker process, so an N-worker deployment sees N
initial tracebacks.

## Consequences

- Persistence is configured in compose (`--save 60 1`) even though correctness does
  not need it: it avoids a cold start recomputing every window from the ledger.
- Anyone who "simplifies" this by making Valkey authoritative for spend has
  introduced a data-loss path for money. The database fallback existing is what makes
  that mistake obvious.
- With no Valkey configured at all, the gateway uses a process-local
  `InMemoryCounterStore` and logs a warning. That is correct only for a single worker
  and is intended for tests and single-process deployments.
