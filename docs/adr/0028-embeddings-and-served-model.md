# 0028 — Embeddings, and recording what actually served a request

- Date: 2026-08-15
- Status: accepted
- Extends [0008](0008-accounting-model.md) (accounting) and
  [0027](0027-inference-providers.md) (providers).

## Context

Cortecs is the reference provider for the first deployment. Reading their API
documentation surfaced three things this gateway gets wrong or does not cover.

**1. A router can serve a different model than the one asked for.** Cortecs
supports Model Fallback: when the primary model is unavailable the request is
retried down a fallback chain, and their documentation is explicit that *"you
only pay for the successful request and for the model that ultimately served the
response."* Their response carries `provider` and `model` naming what actually
ran. This gateway prices from the **requested** model's price rows, rewrites the
response's `model` back to our client-facing name, and records neither field — so
a substitution is billed at the wrong rate and leaves no trace. The setting is
off by default, which makes the discrepancy silent rather than absent.

**2. We send a parameter the provider does not document.** Every streaming
request has `stream_options: {"include_usage": true}` injected. That field is
absent from the Cortecs request schema, their `stream` field already promises
*"the last chunk will contain the usage information"*, and their compatibility
page warns that *"adding unsupported or unnecessary parameters can cause requests
to fail or limit the providers able to process them."* We are taking a documented
risk to ask for something we would get anyway.

**3. Only chat completions are proxied.** Embeddings are the immediate gap —
Phase 3's RAG service needs them, and they cost money, so they must be metered
like anything else.

## Decision

### Record the model and provider that served each request

Two nullable columns on `usage_records`: `upstream_model` and
`upstream_provider`, captured from the response — the same funnel handles a
streamed chunk and a whole non-streamed body, so one place covers both.

Reporting gains a disclosure when any request in a period was served by a model
other than the one asked for. Recording it does **not** fix the price: the
substitute model may not be in our catalogue at all, and inventing a price for it
would be worse than saying the figure is uncertain. Making the discrepancy
visible is the honest first step; automatic re-pricing needs a policy decision
about a model we never agreed to buy.

`rename_model_stage` still rewrites the client-facing `model`, because clients
compare the echoed name with what they sent. The original is now preserved in the
ledger rather than only overwritten.

### `stream_options` becomes a per-provider flag

`providers.forward_stream_options`, default **true** — correct for a generic
OpenAI-compatible endpoint, where asking is the only way to get usage. Turned off
for a provider that sends usage unconditionally and dislikes unknown fields,
which is exactly Cortecs. A provider-level switch rather than a global one
because the right answer differs per endpoint, and this gateway now talks to
several.

### Embeddings are a first-class metered route

`POST /v1/embeddings`, sharing everything that matters with chat completions:
the same model catalogue and access union, the same provider routing, the same
reserve-then-settle quota path, the same ledger and the same price rows.
Differences, all forced by the shape of the operation:

- **No streaming**, so no SSE pipeline and no buffering.
- **`completion_tokens` is always 0** — Cortecs says so explicitly. Cost is the
  input price alone, and an embedding model with an output price is not an error,
  it is simply never charged.
- **The reservation estimates input only.** `default_max_output_tokens` is a
  completion-length guess and has no meaning here; using it would reserve
  thousands of tokens that can never be spent and refuse requests under budget.

`models.kind` (`chat` | `embedding`) distinguishes them, imported from the
Cortecs catalogue's `output_modalities`. Asking `/v1/chat/completions` for an
embedding model is refused **here**, with a message naming the mistake, rather
than forwarded to fail upstream with a provider-specific error.

### Embedding inputs are redacted, and the vector is of the redacted text

The consequence has to be stated plainly: with redaction on, an embedding
represents the text *after* substitution. `"Mario Rossi"` is embedded as
`<PERSON_K3QF7RZM2A>`, and the vector differs from one of the original string.

Redacting anyway, because the alternative is a hole: an embeddings route that
forwards raw text would let anyone bypass redaction entirely by embedding the
document instead of asking about it.

What makes this workable rather than merely safe is that placeholders are
**deterministic** ([0012](0012-redaction-interface.md)): the same entity yields
the same placeholder in the indexed document and in the query that later
retrieves it, so a redacted corpus stays internally consistent and retrieval
still works. It is not equivalent to embedding the original — a placeholder
carries no semantic similarity to the name it replaced — and a deployment that
needs true semantic fidelity over PII should run redaction in `noop` for the
provider serving its index, deliberately.

There is no restoration on the response path. The response is an array of
numbers; there is nothing to restore.

## Consequences

- Migration 0004 adds three columns and one enum. All are nullable or defaulted;
  existing rows read as "not recorded", which is true of them.
- A model imported before `kind` existed is `chat`, which is what every model in
  the catalogue today is.
- The embeddings route does not implement `encoding_format: base64`, `dimensions`
  or batching limits of its own — the request is forwarded and the provider
  decides. Only the fields the gateway must act on are validated, as on the chat
  route.
- Still absent, and named so nobody assumes otherwise: image generation, the
  Responses API, and Anthropic's Messages format. Each is a separate route with
  its own usage shape, and none is needed yet.
