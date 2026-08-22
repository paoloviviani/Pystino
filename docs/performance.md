# What the gateway costs, measured

- Measured 2026-08-22 against the running compose stack with
  `scripts/benchmark_live.py`, which reproduces every figure here.
- **The box: 2 cores, 3 GB, everything co-resident** — gateway, Postgres,
  Valkey, Presidio, Keycloak, the fake upstream, *and* the load generator. Treat
  throughput as a floor and CPU-per-request as the transferable number.
- Related: [docs/redaction-scoping-plan.md](redaction-scoping-plan.md), which
  turns out to be a performance feature as much as a policy one.

## Method, and why it is CPU rather than throughput

Two questions need two measurements:

**Added latency** is taken at concurrency 1, so nothing queues. A saturating
test on a small box measures contention, not overhead.

**CPU per request** is read from each container's cgroup `usage_usec`, which is
cumulative and immune to contention. That is what extrapolates:

```
cores = target req/s * CPU seconds per request / target utilisation
```

A throughput number from a development box does not extrapolate, because most of
what it measures is the box.

## Added latency, and where it goes

Each row adds one layer to the row above, so the deltas attribute the cost.

| | p50 | gateway CPU |
|---|---|---|
| upstream called directly (the floor) | 1.6ms | — |
| `/healthz` — ASGI, routing, middleware | 1.2ms | 0.4ms |
| `/readyz` — one database round trip | 2.5ms | ~1.3ms |
| `/v1/models` — API-key auth, access filter, catalogue | 5.3ms | 3.7ms |
| `/v1/chat/completions` — redaction, quota, metering, upstream | 19.3ms | 12.2ms |

**The web framework is not the cost.** FastAPI, uvicorn, routing and middleware
together are 0.4ms of CPU. Everything above that is our own work: database round
trips, redaction, quota admission, and two ledger writes.

That matters for the recurring "should we put Envoy or APISIX in front of this"
question. Envoy would replace the 0.4ms, not the 12ms. Moving the accounting
into a data plane means reimplementing the money arithmetic in Lua or WASM,
where none of the 775 tests run — a bad trade for a system whose entire value is
that the invoices are right. Envoy in front for TLS, HTTP/2, edge rate limiting
and **load shedding** is worth it on its own merits; it will not make the
gateway faster.

## Round trips per request

Exact and deterministic, unlike the CPU figures, and pinned by
`apps/gateway/tests/test_query_counts.py` so a regression fails a test rather
than a dashboard.

| | before | after |
|---|---|---|
| authenticate (`/v1/models`) | 5 selects | **3** |
| metered request | 8 selects + 2 writes | **5** + 2 |

The two saved on *every* request came from `resolve_api_key`, which used
`selectinload` for the API key's user and billing group. That issues one SELECT
per relation; a `joinedload` fetches all three in one, because both are
many-to-one and cannot multiply rows. Memberships stay `selectinload` — they are
to-many, and joining them would make the result set quadratic in a user's group
count. The model's provider got the same treatment.

Chosen over an in-process auth cache deliberately: it gives most of the same win
with **no staleness window**. A cached principal means a revoked key keeps
working for the length of the TTL, and with more than one worker process there is
no cheap way to invalidate across them. If a cache is ever needed, the honest
version is a shared one in Valkey with explicit invalidation, not a per-process
TTL.

The two writes are by design — the in-progress row exists before the upstream
call so a crash mid-request is visible — and are not a saving to go after.

## Redaction dominates everything

Detection is CPU-bound named-entity recognition, and it scales with prompt
length. Measured with **unique text every request**, which defeats the detection
cache and is therefore the worst case:

| prompt | p50 | total CPU | gateway | detection |
|---|---|---|---|---|
| ~35 tokens | 35ms | 27ms | 14ms | ~13ms |
| ~284 tokens | 74ms | 69ms | 24ms | ~45ms |
| ~1,136 tokens | 198ms | 158ms | 24ms | ~135ms |
| ~4,544 tokens | 480ms | 483ms | 31ms | ~452ms |

Roughly **0.1ms of CPU per prompt token**, and the gateway's own cost stays
flat at 24–31ms. At realistic prompt sizes detection is 85–95% of the total.

Three consequences worth stating plainly:

1. **Capacity planning is redaction planning.** Everything else is noise.
2. **The detection cache is doing real work, and it is per-process.** A chat
   client resends the whole history every turn, so by turn 40 most of the prompt
   is a cache hit — which is why the cached path costs 13ms where the uncached
   path at the same length costs 135ms. But the cache is an in-process LRU, so
   **adding worker processes lowers the hit rate**. At scale the right answer is
   a shared cache in Valkey, keyed the same way.
3. **Scoping redaction is a performance lever, not only a policy one.** Not
   running NER where policy does not require it is the largest single
   optimisation available, and it is already specified.

## Capacity

CPU per request, and what one core buys at 70% utilisation:

| workload | CPU/req | req/s per core | on 8 cores |
|---|---|---|---|
| short prompt, detection cached | ~16ms | ~44 | ~350 |
| ~1,000-token prompt, uncached | ~158ms | ~4.4 | ~35 |
| ~4,500-token agent prompt | ~483ms | ~1.4 | ~12 |

Sanity-checked against measured throughput on this box, where the knee is at
concurrency 2–8:

| concurrency | p50 | req/s |
|---|---|---|
| 1 | 19ms | 48 |
| 2 | 20ms | 87 |
| 8 | 85ms | 83 |
| 32 | 354ms | **60** |

Throughput **falls** from 87 to 60 as concurrency goes from 2 to 32, while p95
goes to 1.2s. That is saturation collapse, and the answer is to shed load at the
edge rather than queue it here.

### Does it reach a few thousand users?

For interactive chat, comfortably. 3,000 registered users with 10% active in the
peak hour, at ~20 requests/hour each, is ~1.7 req/s average and perhaps 8 req/s
peak. With 1,000-token prompts that is `8 × 0.158 ≈ 1.3` cores of detection plus
a fraction of a core of gateway. A single modest VM, with Presidio scaled
separately.

Agentic traffic is a different regime. Ten to fifty model calls per task, with
prompts carrying whole files, puts you at 1–2 req/s per core. Hundreds of
concurrent agent users means a cluster, and the shared detection cache and
redaction scoping both stop being optional.

The architectural property that makes any of this work is already true and
already verified: the gateway is stateless and quota admission is atomic in
Valkey — `scripts/test_quota_race_live.py` drives 40 concurrent clients and gets
exactly the arithmetic number admitted. Replicas are therefore safe.

## What binds before CPU does

1. **`max_connections = 200`** on the upstream client. A 500-token answer at
   30 tokens/second holds a connection for ~17s, so 50 req/s means ~850
   concurrent streams. This is the first wall.
2. **`usage_records` growth.** The only component that does not scale
   horizontally for free: two writes per request, and reports aggregate across
   the table. Wants monthly partitioning, rollups for reporting, and the
   retention policy that is needed for data-protection reasons anyway.
3. **Database pool**: 10 + 20 overflow per worker.
4. **Valkey is a single point of failure.** Throughput headroom is large; losing
   it degrades quotas to the wider database-fallback bound, which is correct
   behaviour worth alerting on rather than discovering.
5. **Memory**: Presidio is 750 MB, the gateway 240 MB for two workers. Presidio
   is the memory driver, not the gateway.

Keycloak does **not** appear on this list: OIDC login happens once per session
and `/v1` uses API keys, so it does not scale with request rate at all.

## Reproducing

```bash
set -a; . deploy/.env; set +a
uv run python scripts/benchmark_live.py ladder      # per-layer cost
uv run python scripts/benchmark_live.py load        # throughput vs concurrency
uv run python scripts/benchmark_live.py redaction   # cost vs prompt length
uv run python scripts/benchmark_live.py all
```

It creates an **unpriced** model on the fake upstream and its own key, so a
benchmark cannot distort billing, and removes both plus its ledger rows
afterwards. It touches no real provider and costs no money.

## What is not measured here

- **TLS**, which is absent from this deployment and is not free.
- **A real provider's latency.** The fake upstream answers in 1.6ms. A real one
  taking 100ms–10s does not change CPU per request but multiplies the number of
  concurrent in-flight streams for the same rate, moving the constraint from CPU
  to connections and memory.
- **Long-answer streaming.** The committed fake upstream emits a fixed seven
  frames, which cannot separate fixed cost from per-frame cost. A throwaway
  upstream with a variable frame count gave `≈15ms fixed + 55µs per frame` —
  about 43ms of gateway CPU for a 500-token answer — but that figure is not
  reproducible from this repository and should be re-measured before being
  relied on.
- **Sustained load.** Everything here is minutes, not hours; no soak, no memory
  growth curve, no connection-pool exhaustion under duration.
