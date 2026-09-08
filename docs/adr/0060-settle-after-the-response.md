# 0060 — The final ledger write happens after the answer is sent

- Date: 2026-09-08
- Status: **accepted, built** for the non-streamed routes. Streaming is
  deliberately untouched.
- Requested as "can you fix the low-hanging fruits? like 1 to 3?", after a
  ranked list of latency opportunities. This was items 2 and 3; **item 1 was
  measured and abandoned** — see "The one that was not worth doing".
- Touches the ordering [ADR 0009](0009-quota-model.md) and the recorder's own
  module docstring describe, and neither's reasoning changes: the row is still
  inserted before the upstream call, and the reservation is still taken before
  any money is spent.

## What moved

`Metered.completed()` — one `UPDATE usage_records` and one counter settle — was
awaited inside the request handler, between the upstream answering and the
response going out. The caller waited for two round trips it has no use for.

It now rides on the response as a Starlette `BackgroundTask`, which runs after
the last byte is sent. Six routes: chat completions, responses, messages,
embeddings, images, and both OCR branches.

## Measured, on the 5-core host, redaction on, 2 workers

| | before | after (median of 3 runs) |
|---|---|---|
| `/v1/chat/completions` p50 | 25.4ms | **21.1ms** |
| p95 | 37.9ms | **25.4ms** |
| CPU per request | 22.2ms | 20.5ms |

**−16% on p50 and −33% on p95**, with CPU per request unchanged inside noise —
which is the signature of moving work off the critical path rather than doing
less of it. Considerably more than the "0.5 to 1.5ms" this was estimated at
before measuring; two round trips cost more than their network time, because
each also waits on a session and a commit.

## And it is not a free win

Under saturation it is neutral to worse, and the honest numbers are:

| | before | after (3 runs) |
|---|---|---|
| peak throughput | 130 req/s | 110, 127, 123 |
| p95 at concurrency 12 | 135ms | 194, 192, 245 |

Peak throughput is **unchanged within the run-to-run spread**, which on this
box is wide enough (110–127 across three runs of identical code) that no claim
either way is supportable. The p95 at concurrency 12 is consistently worse
across all three runs, and that part looks real.

The explanation is that awaiting the settle was **accidental backpressure**.
Deferring does not reduce the work; it overlaps it with the next request, so
under saturation more requests are admitted while their writes pile up behind
them. Below the knee — where a deployment should be operating, and which
`docs/performance.md` already argues for shedding load to stay inside — the
overlap is free and the latency win is real.

Deliberately not mitigated. Capping in-flight background tasks, or awaiting
when the pool is busy, would add a second admission control for the regime the
gateway already says to keep out of. If that regime turns out to be normal for
some deployment, the measurement to repeat is the table above.

## Why this is safe rather than merely faster

**The row already exists.** It is inserted before the upstream call, so a
request inside this window is visible as `in_progress` — which reporting counts
separately and discloses as "still in flight and excluded" — rather than
missing.

**Nothing downstream read the return value.** Every caller ignored the
`QuotaAmounts` that `completed()` hands back; the reservation settles against
the counter store, not through the route.

**A failure is logged, not raised.** By the time the task runs the caller has a
200 and the upstream has served them. Raising would report a failure for a
request that succeeded — and bill them anyway, because the row exists. What it
costs is that a failed settle leaves the row `in_progress` instead of returning
a 500. That is the better of two bad answers, and it is visible in both the
ledger and the log.

**The test suite stayed deterministic**, which was the thing most likely to go
wrong. `httpx`'s ASGI transport awaits the background task before the request
returns, so hundreds of tests that assert on a finalised row immediately after
a call still see one. That is luck in the sense that it was not designed for,
and it is also why `test_deferred_settlement.py` drives the app as **raw ASGI**
for the one test that matters: through the client, deferred and awaited are
indistinguishable, and a refactor could quietly put the write back on the
critical path with every other test still green.

## Streaming is not included

Its finalisation runs in the body iterator's `finally`, and the open bug there
— a client that disconnects at the terminal frame cancels the task mid-write,
leaving the row `in_progress` — needs `spawn_finalisation` and its own tests.
That is a correctness fix with a different shape, still listed in CLAUDE.md,
and a response class cannot do it.

## The one that was not worth doing

Item 1 of the list was "responses are serialised with the standard library's
`json` while orjson is already a dependency". It was measured before being
built, and abandoned:

| payload | stdlib | orjson | ratio |
|---|---|---|---|
| one SSE frame (151 B) | 0.0044ms | 0.0003ms | 12.6x |
| chat completion (2.3 KB) | 0.0098ms | 0.0007ms | 14.2x |
| 200-model catalogue (30 KB) | 0.275ms | 0.042ms | 6.6x |

Six to fourteen times faster, and **irrelevant**: the chat completion saves
0.009ms of a 21ms request, or 0.04%. Only the large management listing is worth
anything at all — 0.23ms of an 8.7ms request — and FastAPI 0.141 already
serialises those straight to bytes through Pydantic, because they declare
response models. `fastapi.responses.ORJSONResponse` is deprecated in that
version for exactly this reason.

Recorded because the ranked list that produced it put this **first**, and it
was worth roughly nothing. The estimate was wrong by two orders of magnitude,
which is the argument for measuring the cheap thing before building it rather
than after.
