# 0004 — Python + FastAPI for the gateway, and how to keep it fast

- Status: accepted
- Date: 2026-08-14

## Context

The starting assumption was Python 3.12 + FastAPI, with the explicit question:
*is the SSE-rewriting path a mistake at a few hundred concurrent users?*

The rationale for Python is sound — Presidio is Python, and one developer has to
maintain this — but "we chose Python and it fell over under streaming load" is a
real failure mode worth arguing about before writing code rather than after.

## The performance question, answered

At 300 concurrent streams generating ~60 tokens/second each, the gateway handles
roughly **18,000–30,000 SSE events per second**, each a 200–400 byte JSON object
that must be reassembled, inspected, possibly rewritten, re-encoded and forwarded.

With `orjson` and `uvloop`, per-event work is tens of microseconds — order **one
core** — and it scales horizontally across worker processes because all state lives
in PostgreSQL and Valkey. Python is not the constraint here.

What *will* hurt, and what the design does about it:

1. **Blocking work on the event loop.** This is the actual risk, and it is not
   throughput. Presidio means spaCy: CPU-bound, synchronous, hundreds of
   milliseconds. Run in-process it would stall *every concurrent stream on that
   worker*, not just the request being screened. Hence the hard rule in
   [0012](0012-redaction-interface.md): redaction is an out-of-process HTTP call,
   never a library import. `ruff`'s `ASYNC` rules are enabled to catch blocking
   calls in `async def`.
2. **Parsing every frame unconditionally.** `SSEEvent.json()` memoises, so the
   several stages that inspect one event parse it once.
3. **The GIL.** Scale with worker *processes*, not threads. The gateway is stateless,
   so this is free.

## Decision

- **Python 3.13** + FastAPI. Not 3.14: free-threading is officially supported there
  as of late 2026 and the single-thread penalty is down to 5–10%, but "officially
  supported" is not "operate this in production for a foundation".
  - The version shipped is the version tested. Local development and CI here ran on
    **3.13.15**, and the Dockerfile uses `python:3.13-slim`.
- `uvicorn[standard]` (uvloop + httptools). **Granian** (Rust ASGI) is a credible
  later swap — benchmarks show 20–50% on CPU-bound paths — but the same benchmarks
  show the gains collapsing once real database work dominates, which is our case.
  Recorded as a known lever, not adopted.
- `orjson` everywhere on the hot path.
- Multiple worker processes (`gateway serve --workers N`).

## Versions verified at install time (2026-08-14)

| Package | Resolved |
|---|---|
| fastapi | 0.141.1 |
| starlette | 1.6.0 |
| pydantic | 2.13.4 |
| sqlalchemy | 2.0.52 |
| alembic | 1.19.1 |
| uvicorn | 0.52.3 |
| httpx | 0.28.1 |
| joserfc | 1.7.4 |
| orjson | 3.11.9 |

## Consequences

- The redaction service being a separate deployable is not an architectural
  preference; it is load-bearing for latency. Do not "simplify" it into the gateway.
- One FastAPI-specific trap already hit: `POST /v1/chat/completions` returns either
  a `JSONResponse` or a `StreamingResponse`, and FastAPI tries to build a Pydantic
  response model from that union. It needs `response_model=None`. The response body
  is OpenAI's schema, passed through rather than re-validated.
- If a profile ever shows the event loop saturated, the order to investigate is:
  (1) something blocking, (2) worker count, (3) `orjson` coverage, (4) Granian.
  Rewriting in another language is far down that list.
