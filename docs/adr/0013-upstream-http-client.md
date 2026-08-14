# 0013 — The upstream HTTP client (httpx), and a maintenance risk to watch

- Status: accepted
- Date: 2026-08-14

## Context

The upstream client does the most load-bearing job in the gateway: it holds long-lived
streaming connections to model providers. Its behaviour under timeouts and cancellation
determines whether accounting works.

## Decision

**`httpx`**, behind a thin internal interface (`OpenAICompatibleUpstream`, with
`build_http_client()` isolating construction), so replacing it is a day's work rather
than a rewrite.

### Timeout configuration — the trap

```python
httpx.Timeout(
    connect=10.0,
    read=None,  # explicitly unbounded
    write=30.0,
    pool=10.0,
)
```

httpx's default read timeout is **5 seconds**, and it applies to the gap between reads
*on a stream*, not to the request as a whole. A model that thinks for six seconds before
its first token, or pauses mid-generation, has its stream killed by the client library —
intermittently, under load, in a way that looks like a provider fault. `read` must be
`None`.

Connect, write and pool stay finite: without them an unreachable provider hangs a
request forever instead of failing fast.

### Two clients, not one

The upstream client has no read timeout. A control-plane call — OIDC discovery, JWKS —
must **never** inherit that, or a hung identity provider hangs a login indefinitely.
`init_app_state` builds two clients: `upstream_http` and `control_http` (10s).

### Streaming lifecycle

The upstream stream is opened **before** the `StreamingResponse` is returned, so an
upstream error can still become a real HTTP status. Once a `StreamingResponse` is handed
back the status line is committed, and the only way to report a failure is an error frame
inside the stream — which most clients handle badly.

The stream is held in an `AsyncExitStack` closed in the generator's `finally`, so an
abandoned client cannot leak a provider connection that keeps generating billable tokens
nobody will read.

## The maintenance risk, stated plainly

**httpx's last release is 0.28.1, dated 6 December 2024** — about 20 months before this
was written. Confirmed twice: from PyPI, and by the resolver, which selected 0.28.1 on
2026-08-14. Its PyPI classifiers still top out at Python 3.12. The `master` branch has
commits into February 2026, so it is not abandoned, but the release cadence is very low
for the single most load-bearing dependency here.

`aiohttp` by contrast is at 3.14.3 (23 July 2026, Apache-2.0/MIT).

**Decision: use httpx anyway**, because its streaming API is the right shape, it works
correctly on 3.13 in practice, and it is the well-trodden path — but keep it behind the
interface and treat a security advisory with no upstream release as the trigger to
migrate, not a reason to panic.

## Consequences

- Requests are forwarded **permissively**: `body.model_dump(exclude_unset=True)` sends
  exactly what the client sent, including parameters this gateway knows nothing about,
  and nothing it did not. A new provider parameter therefore needs no release from us,
  and we never invent a default that silently changes model behaviour. Tested.
- An `x-request-id` is sent upstream, so our ledger can be correlated with a provider's
  own logs when reconciling a disputed bill.
- Connection limits are configurable (200 total, 50 keepalive by default); at a few
  hundred concurrent streams these need reviewing against the provider's own limits.
- One retry behaviour deliberately **not** implemented: some providers reject unknown
  parameters such as `stream_options`, in which case the correct response is one retry
  without it. There is no way to test that here without such a provider, so it is left
  as known future work rather than untested code.
