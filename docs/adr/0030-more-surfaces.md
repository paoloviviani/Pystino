# 0030 — Responses, Anthropic Messages and image generation

- Date: 2026-08-16
- Status: accepted
- Extends [0028](0028-embeddings-and-served-model.md), which added the second
  metered route and set the pattern this one generalises.

## Context

Three surfaces were deferred at the end of [0028](0028-embeddings-and-served-model.md)
with "not now": image generation, OpenAI's Responses API, and Anthropic's
Messages API. The reference provider serves all three from the same base URL
with the same credential, so none of them is a second gateway — each is a
request shape, a usage shape and a streaming protocol.

The reason to do them together rather than one at a time is that the second
route already showed the cost of doing them apart. `embeddings.py` was a copy of
`chat.py` with the streaming removed, and the copies had already diverged: one
settled a failed request and the other released it. Three more copies would
have meant five places where the reserve-then-settle ordering could drift, and
that ordering is the only thing between a caller and an unmetered request.

## Decision

### 1. One metering path, five surfaces

`routers/_metered.py` owns resolve → check access → reserve → open the row →
call → settle. A route supplies the protocol-specific parts and calls those in
order; it does not decide when the reservation happens.

`protocols.py` owns the other half: per surface, where usage lives, which model
served the request, and which fields hold assistant text. Accounting and
redaction both read it, so there is one table indexed by surface rather than
two that can disagree.

Chat and embeddings were moved onto both. That is most of the diff, and it is
the part that makes the three new routes small.

### 2. Anthropic's prompt convention is inverted, and this is the headline

OpenAI's `prompt_tokens` **includes** the cached tokens. Anthropic's
`input_tokens` **excludes** them — the prompt is

```
input_tokens + cache_creation_input_tokens + cache_read_input_tokens
```

Reading Anthropic's usage with the OpenAI reader subtracts a cache read from a
figure that never contained it. On a request with more cache reads than
uncached tokens it bills nothing at all for the prompt.

So each surface names its reader explicitly — `from_usage`,
`from_responses_usage`, `from_anthropic_usage` — instead of one tolerant
function sniffing keys. A test pins the difference by computing both and
asserting they disagree, so the two conventions cannot quietly converge.

**Cache writes are now billed.** `cache_write_per_mtok` had existed on the
price table since Phase 1 and `compute_cost` never read it. Anthropic reports
cache creation as a distinct slice charged above the input rate, which made the
gap real. The prompt is now three disjoint slices — uncached, read, written —
each at its own rate, and a slice with no rate configured falls back to the
input rate rather than to zero. Charging nothing for tokens the provider
charged us for is the more expensive mistake and stays invisible until the
invoice arrives.

### 3. Usage that arrives in two frames is merged, not overwritten

Anthropic streams the input count on `message_start` and a cumulative output
count on `message_delta`; the final delta repeats the input only when server
tools ran. "Last frame wins" is correct for OpenAI's single terminal usage
frame and records a prompt of zero here. The reader declares
`accumulates_usage` and the recorder merges key by key.

### 4. Images are billed per picture, because that is how they are sold

Most image models are priced per generated image by size and report no token
usage at all; the newer OpenAI ones are token-priced and do. Both are served.
`model_prices.per_image` sits alongside the token rates rather than replacing
them — a model can be metered both ways, and one metered neither way records
zero cost with a null price so the gap shows in reporting.

Two consequences worth stating:

- **`image_size` is recorded on every row** even though nothing prices by size
  yet. Per-size pricing is a real thing and modelling it properly is a bigger
  change than this; recording the size is the difference between introducing it
  later and having a ledger that cannot say what was bought.
- **The `revised_prompt` is not counted as output.** It is the only text in an
  image response and it is tempting, but it is the provider echoing our own
  prompt back, not text it charged for. Counting it estimated tokens for a
  quantity nobody bills and labelled an exactly-known charge "estimated" —
  precisely backwards, since the picture count is the exact figure. It is still
  restored through redaction; it is just never metered.

The report grew an `images` column and a disclosure, because a row showing real
money against zero tokens reads as a bug otherwise.

### 5. Server-side conversation state is refused

`previous_response_id` and `store` on the Responses API let the provider hold
the conversation. That puts prompt text outside this gateway's redaction and
outside its ledger: a stored prefix is billed to us on every follow-up and we
would have no record of what it contained. Refused with a 400 that says so,
rather than forwarded.

### 6. Credentials are presented per provider, not per route

Anthropic's own API authenticates with `x-api-key` and rejects a bearer token.
The reference provider uses bearer for every route including `/v1/messages`. So
`providers.auth_scheme` is a property of the provider, defaulting to bearer,
and `anthropic-version: 2023-06-01` is sent unconditionally on the messages
route — required by Anthropic, ignored by anything proxying the shape.

### 7. `api_surface` on every usage row

Chat, Responses and Messages serve the *same* models, so the model no longer
identifies the endpoint. "Can we retire the Anthropic surface" is a question
only this column answers.

## Consequences

Adding a sixth surface now means a protocol class and a thin route, not another
copy of the pipeline.

Two behaviours changed for existing routes. Every failure path now **settles**
rather than releasing — settling charges what was actually used and releasing
hardcodes zero, and the two differ the moment a provider fails after producing
something. And the chat route's kind check moved into the shared resolver, so
its error message now names all three routes that would have worked.

`ModelKind` gained `image`, which widens a check-constrained enum; migration
0005 rewrites the constraint and backfills `api_surface` from each model's kind
— exactly right for historical rows, since before this there were two surfaces
and each had one kind.

**The catalogue importer will not guess an image model from its name.**
"image" appears in the id of many models that *accept* images and generate
text, and mislabelling one of those takes it off the chat route entirely. Only
an explicit `image` in `output_modalities` counts; anything else is labelled by
hand.

## Not done

- **Image editing and variations** (`/v1/images/edits`, `/v1/images/variations`).
  They take multipart uploads, which the redaction layer has no story for.
- **Partial-image streaming.** A partial image is not billable output; the route
  would gain a streaming pipeline for no accounting benefit.
- **Per-size image pricing.** The data to introduce it is being recorded now.
- **Anthropic's Batches, Files and token-counting endpoints.** Only Messages is
  served.
- **Whether Cortecs rejects `stream_options`** — still open from
  [0028](0028-embeddings-and-served-model.md), and still unanswerable without a
  key. `scripts/check_cortecs_stream_options.py` answers it in four requests
  once someone has one, including the case that has no error attached: the
  parameter being accepted but narrowing the routing pool.
