# What the gateway costs, measured

- Measured 2026-08-22 against the running compose stack with
  `scripts/benchmark_live.py`, which reproduces every figure here, and
  **re-measured on a 5-core box on 2026-09-08** — see "Re-measured on 5 cores"
  below for what changed (single-request latency: nothing) and what it revealed
  (the throughput knee, at concurrency 8).
- **The box: 2 cores, 3 GB, everything co-resident** — gateway, Postgres,
  Valkey, Presidio, Keycloak, the fake upstream, *and* the load generator. Treat
  throughput as a floor and CPU-per-request as the transferable number.
- Related: [redaction scoping](redaction.md), which turns out to be a
  performance feature as much as a policy one.
- Sources for the comparison table: LiteLLM's own
  [benchmarks page](https://docs.litellm.ai/docs/benchmarks), Tetrate's
  [Envoy AI Gateway benchmarks](https://tetrate.io/learn/ai/ai-gateway-benchmarks),
  and DeepInspect's
  [reading the 2026 numbers](https://www.deepinspect.ai/blog/ai-gateway-latency-benchmarks),
  whose warning about mock upstreams applies to every figure here including ours.

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
| authenticate (`/v1/models`) | 5 selects | **3** (4 since the listing also carries the agents a caller may reach; the test pins 4) |
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

## Re-measured on 5 cores, 2026-09-08

The figures above are from the 2-core/3 GB box. Repeated on
a host with **5 cores, 14 GB**, still with everything co-resident
including the load generator, and with the gateway running its usual **2 uvicorn
workers**. Two conclusions, and the second is the useful one.

**Single-request latency barely moved**, which is what the CPU-per-request model
predicts: one request uses one core, so more cores buy nothing for it.

| | 2 cores (2026-08-22) | 5 cores (2026-09-08) |
|---|---|---|
| upstream called directly | 1.6ms | 2.0ms |
| `/healthz` | 1.2ms | 2.9ms |
| `/readyz` | 2.5ms | 3.9ms |
| `/v1/models` | 5.3ms | 8.7ms |
| `/v1/chat/completions` | 19.3ms | 26.3ms (p95 42.6) |

Slower per request on the bigger box, not faster. The measurement is p50 at
concurrency 1 over a loopback network, so it is dominated by per-request CPU
work and scheduler noise, not by core count — and this host is busier, with
other development containers on it. **This is why the doc leads with CPU per
request:** 20.3ms of CPU here against 12.2ms there is the same shape of answer,
and it is the number that extrapolates.

**Throughput has a knee, and it is early.** A sweep from 1 to 48 concurrent
requests, unique prompt per request:

| concurrency | p50 | p95 | req/s |
|---|---|---|---|
| 1 | 27ms | 41ms | 35 |
| 2 | 26ms | 37ms | 73 |
| 4 | 45ms | 56ms | 96 |
| **8** | **61ms** | **86ms** | **130** |
| 12 | 88ms | 122ms | 128 |
| 16 | 123ms | 233ms | 120 |
| 24 | 190ms | 575ms | 103 |
| 32 | 250ms | 855ms | 98 |
| 48 | 358ms | 1123ms | 104 |

Peak throughput is at **concurrency 8: 130 req/s at p95 86ms**. Past it the
work done stops growing and only the queue does — at 48 the p95 is **13x** the
p95 at the knee for *less* throughput. That is the argument for shedding load at
the edge rather than queueing it here, made in numbers rather than in principle.

With redaction switched on the shape is identical and the knee moves out one
step, to concurrency 12 at 130 req/s — the prompts in this sweep are a few
tokens each, so detection has almost nothing to do. The next section is where
redaction actually costs.

### Moving the final write off the critical path (2026-09-08)

The `UPDATE usage_records`
and the counter settle used to be awaited between the upstream answering and
the response going out; they now ride on the response as a background task.
Same host, same 2 workers, redaction on:

| | before | after (median of 3) |
|---|---|---|
| `/v1/chat/completions` p50 | 25.4ms | **21.1ms** |
| p95 | 37.9ms | **25.4ms** |
| CPU per request | 22.2ms | 20.5ms |

CPU unchanged, latency down: the signature of work moved rather than removed.

Under saturation it is neutral to worse — peak throughput unchanged inside a
wide spread (110–127 req/s across three runs of the same code, against 130
before), and p95 at concurrency 12 consistently worse (192–245ms against
135ms). Awaiting the settle was accidental backpressure; deferring overlaps the
write with the next request instead of removing it. Below the knee that overlap
is free, and below the knee is where this doc already argues a deployment
should stay.

**A cheaper-looking idea that was measured and dropped:** switching response
serialisation from the standard library to orjson. Six to fourteen times
faster in relative terms and worth 0.009ms on a chat completion — 0.04% of the
request. FastAPI 0.141 already serialises
response-model routes straight to bytes, and its `ORJSONResponse` is
deprecated for that reason.

### How this compares, and to what

Worth writing down because "is 24ms of overhead good" has no answer without a
peer group, and the peer group is not the one the marketing pages imply.

| | added latency | measured on |
|---|---|---|
| Envoy AI Gateway | 1–3ms | proxy only; no metering, no ledger |
| Kong (nginx + Lua) | 2–5ms | same class |
| Portkey, bare proxy | <1ms claimed | — |
| Portkey **with guardrails on** | 20–40ms, community-reported | the comparable class |
| LiteLLM proxy | **12ms** median, p95 29ms, at 1,036 RPS | 2 instances × 4 CPU / 8 GB, fake upstream |
| LiteLLM proxy | 2ms median, at 1,170 RPS | 4 instances × 4 CPU / 8 GB |
| **this gateway** | **~24ms** (26.3 total − 2.0 upstream) | 2 workers, 5 shared cores, everything co-resident |

Read honestly, three things follow.

**The sub-3ms figures are a different class of product.** A proxy that routes
and forwards does not write a ledger row before the upstream call, reserve
against five quota scopes, settle at actuals, or run named-entity recognition.
Those are not overheads that better engineering removes; they are the thing
being built. The right comparison for us is the "guardrails enabled" band, and
~24ms sits inside it.

**Against LiteLLM we are about 2x slower per request, on half the cores.** That
is the comparison that should sting a little, and it is not explained away by
the ledger: their published run used a fake upstream and PostgreSQL, as ours
does. Two differences are real and worth keeping in view — they measure
`x-litellm-overhead-duration-ms`, their own processing as they instrument it,
against our wall-clock p50 minus the upstream; and their advice is
"workers = CPU count" where we ship two. See below for what that is actually
worth here.

**Per core we are 2.5–5x behind their throughput.** Theirs is ~129 req/s per
core (1,036 RPS over 8 cores); ours is ~26 measured (130 over 5) or 49 by the
CPU model. Some of that gap is the two ledger writes and the reserve/settle
pair, which are deliberate. How much is *not* deliberate is unmeasured, and
that is the honest state of it.

### Does the worker count explain it? Measured: mostly no

LiteLLM's guidance is workers = CPU count. The gateway ships two. Four were
measured on this 5-core box, same everything else, redaction on:

| concurrency | 2 workers | 4 workers |
|---|---|---|
| 4 | 98 req/s, p95 58ms | 74 req/s, p95 97ms |
| 8 | 124 req/s, p95 89ms | 86 req/s, p95 158ms |
| 12 | **130 req/s**, p95 135ms | 127 req/s, p95 189ms |
| 16 | 112 req/s, p95 256ms | **139 req/s**, p95 250ms |
| 24 | 100 req/s, p95 602ms | 137 req/s, p95 349ms |
| 32 | 83 req/s, p95 830ms | 115 req/s, p95 529ms |

Peak throughput moved 130 → 139 req/s: **7%, for double the processes.** What
four workers actually bought is a gentler collapse — at concurrency 32,
115 req/s against 83, with p95 529ms against 830ms — and it cost latency below
the knee, where four processes contend for cores that Postgres, Valkey and
Presidio are also using.

So the box is **core-limited, not worker-limited**, and "workers = CPU count"
does not transfer to a deployment where the CPUs are shared with the
dependencies — above all with the detection service, which is the hungriest
thing on the host. Two workers stays the default. On a host where the gateway
runs alone, raise it; the measurement to repeat is this table.

### Redaction against prompt length, re-measured

Unique text per request, so nothing hits the per-process detection cache:

| prompt | p50 | total CPU | of which the gateway |
|---|---|---|---|
| ~35 tokens | 74ms | 61ms | 32ms |
| ~284 tokens | 122ms | 108ms | 35ms |
| ~1,136 tokens | 268ms | 229ms | 44ms |
| ~4,544 tokens | 596ms | 625ms | 64ms |

**0.116ms of latency per prompt token**, against 0.1ms measured on the 2-core
box — the same number within noise, and for the same reason single-request
latency did not improve: detection is one CPU-bound pass in one Presidio
process. The gateway's own share stays between 32ms and 64ms across a 130x
range of prompt sizes; everything else is the detector.

A 4,500-token prompt costs **0.6 seconds** and **0.625 CPU-seconds**. At that
size one core serves 1.6 requests per second. That is the capacity fact behind
[redaction scoping](redaction.md): scoping redaction is not a policy nicety, it is how a deployment with long prompts stays
affordable.

### How these were taken

```bash
set -a; . deploy/.env; set +a
export FAKE_UPSTREAM_URL=http://127.0.0.1:8081
uv run python scripts/benchmark_live.py ladder
uv run python scripts/benchmark_live.py load 1,2,4,8,12,16,24,32,48
uv run python scripts/benchmark_live.py redaction
```

The `load` mode takes an explicit sweep now, because the knee is the actionable
number and the old fixed `(1, 2, 8, 32)` steps could not see it. It also sends a
distinct prompt per request: with one repeated prompt every request after the
first hits the detection cache, which flatters the numbers **only when redaction
happens to be enabled** — the worst kind of artefact for a figure somebody plans
capacity from.

The direct-upstream row needs the fake upstream reachable from the host, which
the smoke overlay now publishes on **127.0.0.1:8081** — loopback only. Ground
rule 4 forbids publishing anything but the proxy's TLS port on a *routable*
address, and `scripts/test_public_tls_live.py` still enforces exactly that.

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
