# 0007 — The SSE streaming architecture

- Status: accepted
- Date: 2026-08-14

## Context

The streaming response path is where this gateway is most likely to be subtly
wrong, because every failure mode is invisible in a happy-path test: token counts
quietly missing, a frame dropped, a client disconnect losing an answer that was
already paid for.

## Decisions

### Parse at event boundaries, never at chunk boundaries

`gateway/sse/parser.py` is an incremental parser. Feed it arbitrary byte chunks; get
back only complete events. Nothing downstream ever sees a network chunk.

The fiddly part is line terminators. SSE permits `\n`, `\r\n` and a bare `\r`, which
makes a buffer ending in `\r` genuinely ambiguous: the next chunk may start with
`\n` (one CRLF) or with anything else (a complete line). Resolving that early
invents a blank line and therefore a **spurious event boundary**, so a trailing `\r`
is held back until more bytes arrive or the stream ends.

Tested by feeding identical bytes at every possible split offset and one byte at a
time, and asserting identical output. That property — *output must not depend on how
the stream is chopped up* — is the whole point, because in production the chunking is
chosen by the network and is never the same twice.

**A bug this found:** the first implementation dropped an unterminated final line, so
a provider ending its stream without a trailing newline lost its last frame — which
is exactly the frame carrying token usage. Fixed and regression-tested.

### Stages are async-generator transformers, not per-event callbacks

A stage consumes a stream of events and yields a stream of events. The simpler
`event -> event` callback was rejected for one specific reason: a redaction stage
cannot decide whether to emit a chunk of text until it knows the text is not the
start of an entity continuing into the next frame. `Ma` / `rio Ros` / `si` across
three frames must be detectable, so a stage must be able to **hold bytes back and
release them later**, and to flush what it held when the stream ends. A callback
returning one event per event cannot buffer. This can — which is what makes Phase 2
an insertion rather than a redesign. See [0012](0012-redaction-interface.md).

### Force `include_usage`, and strip it if the client did not ask

`stream_options: {"include_usage": true}` is **merged** into every streaming upstream
request (merged, not replaced, so other options the client set survive). Without it,
streamed responses carry no token counts and accounting silently reports zero.

On the way back out, two distinct cases — conflating them corrupts the stream:

- a **usage-only** frame (usage present, `choices` empty) is dropped whole;
- a frame carrying **both** usage and content has only its `usage` key removed,
  because dropping it would swallow generated text.

Stage order matters: usage capture (`tap`) runs **before** usage stripping, so
accounting sees counts the client will never receive.

### Never trust an absent usage frame

If a stream produced text but no usage arrived, tokens are counted locally and the
row is stamped `usage_source='estimated'`. **Zero is never silently recorded.** This
is not defensive programming for its own sake — BerriAI/litellm#25389 documents
exactly this loss for vLLM backends and was closed as not planned. See
[0008](0008-accounting-model.md).

### Client disconnect

Starlette cancels the generator when the client goes away. The upstream stream is
held in an `AsyncExitStack`, so it is closed on every exit path.

Finalisation is the subtle part: inside a cancelled task, `await` re-raises
immediately, so awaiting the database write would abandon it. On the abnormal path,
finalisation is handed to a **detached task** with a strong reference held on
`app.state.background_tasks` (a task with no references can be garbage-collected
mid-flight, losing precisely the write being protected). Shutdown waits up to 5
seconds for those tasks. Tested by driving the endpoint directly and closing the
generator after one frame.

### Persist assistant output as it streams

Mobile clients get suspended mid-generation; a response that exists only in the
stream is lost. Partial text is flushed on a size/time threshold, each flush its own
short transaction. **One transaction for the life of a stream would pin a connection
for the whole generation and hold back autovacuum on the busiest table in the
schema.**

### httpx timeouts

`read=None` explicitly; `connect`, `write` and `pool` finite. httpx's default 5s read
timeout applies to the gap between reads *on a stream*, so a model that thinks for
six seconds before its first token has its stream killed by the client library —
intermittently, under load, looking like a provider fault.

## Consequences

- Keepalive comment frames (`: ping`) are preserved rather than dropped; clients and
  intermediaries rely on them.
- Model names are rewritten in every frame so clients never see the upstream's name.
- A synthesised frame emitted by a buffering stage copies `id`, `model` and `created`
  from the last real frame, because strict clients check them, and never carries
  `usage`.
