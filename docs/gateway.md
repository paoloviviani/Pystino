# Gateway

OpenAI-compatible API gateway with per-user and per-group accounting, quotas,
per-group model availability and a pluggable redaction layer.

## Surfaces

| Path | Auth | Purpose |
|---|---|---|
| `POST /v1/chat/completions` | key or bearer | Proxy to the configured upstream, streaming and not |
| `POST /v1/responses` | key or bearer | OpenAI's Responses API. Server-side conversation state (`previous_response_id`, `store`) is refused |
| `POST /v1/messages` | key or bearer | Anthropic's Messages API, over the same chat models |
| `POST /v1/embeddings` | key or bearer | Embeddings, metered and redacted like a completion |
| `POST /v1/rerank` | key or bearer | [Reranking](#reranking-v1rerank): score documents against a query (Cohere and Jina's shape), metered per input token, query and documents redacted like embedding input |
| `POST /v1/decisions`, `POST /v1/systemone` | key or bearer | [Decision models](decision-models.md): typed questions about a text, answered with calibrated probabilities. Metered per token, redacted, the request and the answer passed through in the format they arrived in |
| `POST /v1/images/generations` | key or bearer | Image generation, billed per picture or per token depending on the model |
| `POST /v1/ocr` | key or bearer | Document extraction, metered per page. Two backends: an upstream OCR model, or this deployment's own extractor |
| `POST /v1/audio/transcriptions`, `POST /v1/audio/translations` | key or bearer | [Speech to text](#speech-to-text-v1audiotranscriptions), OpenAI's multipart shape, metered per second of audio and priced per minute. The audio is not screened by redaction |
| `POST /v1/search` | key or bearer | Web search against a configured backend (Linkup, Exa, Jina, DuckDuckGo), metered per call and billed per request at the backend's price ([Web search: a price per request](accounting-and-quotas.md#web-search-a-price-per-request)). The backend is the billing group's search policy, or the one the body names in an optional `backend` (a search model the caller is granted; an ungranted name is a 404, never a fallback to the policy). `POST /v1/search/{backend}` is the verbatim passthrough |
| `GET /v1/models` | key, bearer or none | Models this caller may use — billing-group grants (or the group named by `x-bill-to`), personal grants, and public models; and the caller's own [bring-your-own-key](byok.md) models as `my/<name>` with `"byok": true`. Unauthenticated: the full deployment catalogue, brochure fields only |
| `GET /v1/pystino/usage` | key or bearer | This caller's own spend, for a client that wants to show it without a console session |
| `GET /v1/pystino/usage/tags` | key or bearer | This caller's own usage by the label it sent in the `x-pystino-tag` request header (printable ASCII, at most 128 characters; an invalid label is ignored, never an error). Totals per tag (`?tag=a&tag=b`, optionally between `since` and `until`), for a client that wants a session's or a turn's cost. Only the caller's own requests are read, gateway usage only (bring-your-own-key and in-flight requests are left out) |
| `GET /v1/billing/groups` | key or bearer | Which groups this caller may bill, and which one paid for this request |
| `GET /v1/me` | key or bearer | Who is calling, their groups and `is_admin` (from effective memberships; never `true` for a key) |
| `GET /v1/me/identities` | access token only | Every identity that names this person, including merged ones, for the chat |
| `POST /v1/session/announce` | access token of the chat client | The chat's sign-in door: runs linking, provisioning and the admin rules, and answers who this is |
| `GET /auth/login`, `/auth/callback/{provider}` | — | OIDC authorization-code login (PKCE); the provider is `default` for the one set in `.env` |
| `GET /auth/methods`, `/auth/session`, `POST /auth/logout` | —, cookie, cookie | Which sign-in methods exist; whether a session cookie is still good; sign out, at the provider too |
| `/api/me/*` | session cookie | Identity, billing group, API keys, own usage and reports |
| `/api/admin/*` | session cookie + `is_admin` | Models, prices, group access, quotas, users, providers, redaction rules, reports, identity events, the [administrator audit log](console.md#the-administrator-audit-log) |
| `/scim/v2/{provider}/*` | the provider's SCIM token | SCIM 2.0 pushes from an external directory ([User sync](oidc-generic-provider.md#user-sync)) |
| `GET /opencode/install.sh` | — | The opencode setup script ([Coding agents](coding-agents.md#point-opencode-at-the-gateway-with-a-script)) |
| `GET /healthz`, `/readyz` | — | Liveness (no dependencies) and readiness (one DB round trip) |

Every metered `/v1` route shares one metering path — `routers/_metered.py` — so
resolve → reserve → record → settle cannot drift between surfaces.
Ten routers import it today: chat, responses, messages, embeddings, rerank,
images, ocr, transcription, search and decisions.
`_metered.begin` takes `fx` and `session_factory` rather than a `Request`, so
code with no request can bill through the same path: a second copy of the money
code would make that spend invisible to every report. Knowledge-base ingestion
is the chat's, not the gateway's: Cerea calls `/v1/embeddings` and `/v1/ocr`
like any client, each metered to the signed-in person's own token.


**`/v1` and `/api` differ, and that is the whole asymmetry.** One dependency
(`deps.get_principal`) authenticates every `/v1` route, and it takes either
credential: a JWT in the `Authorization` header is verified against the issuer
that minted it, anything else is looked up as an API key — so
"accepts a key" and "accepts a bearer" are the same list, not two. `/api`
accepts neither; it reads a session cookie and nothing else. That is why
`/v1/billing/groups` and `/v1/pystino/usage` are on `/v1` at all: a chat client
holding a bearer token cannot reach a management route, so anything it needs
has to live where it can be reached.

The one credential-shaped distinction that survives is `x-bill-to`:
a bearer caller may name the group to bill, an *issued* key may not — it
already carries its answer, and quietly overriding it is how somebody finds
out from an invoice. `GET /v1/models` is the other exception, in the opposite
direction: it answers unauthenticated too, brochure-level fields only, so a
client can build a catalogue before anyone has signed in.

Authenticated, the model list shows *exactly what a request would accept,
nothing more*: the same scope the request path admits on (`resolve_model`) —
the billing group's grants (the group named by `x-bill-to`, else the
caller's default), the caller's personal grants, and public models. It used
to union every group the person belonged to, which advertised a model every
request then refused with a 404 to anyone who belonged to two groups and
billed the other one. Anonymous, it is the whole catalogue: the listing is
public, a card's detail is not (ADR 0081). The whole rule — what decides the
billing group, what it decides in turn, and how a machine follows a person's
default — is on [Billing groups](billing-groups.md).

`GET /v1/models` reports each model's `kind`, `context_window`,
`max_input_tokens`, `max_output_tokens`, `input_modalities`,
`output_modalities` and `supported_features`, so a client
can pick a model that does tool calling or reads images without taking a 400 to
find out. `supported_features` includes `tool_images` for a vision model that
can be shown images returned by a tool (see the console's *Images in tool results*). These are non-standard fields, which OpenAI clients ignore. `max_input_tokens` is the one limit the gateway itself enforces:
a prompt estimated above it is a 400 `prompt_too_long` before any
reservation is made, and it is unset for most models — no catalogue reports
a provider's real input cap, so an operator records it by hand and null
means "no local limit", never zero.

### A model's name and its display name

**A model's name and its display name answer different questions.** `id` is
what a request sends and what stored references key on — a conversation's
model, a custom model's base, the grants and prices behind it — so it is set
once and never changed; `display_name` is the label a client's picker shows,
set on the model's page and changeable at any time. Discovery suggests an id
from the provider's own naming and imports do not overwrite one you have
typed; the display name is yours alone and is served as `display_name` on
`GET /v1/models`. Renaming a model's `id` is deliberately not offered: usage
rows denormalise it, so a change would split the history rather than move it.

Every management listing answers with `{items, total, limit, offset}` and takes
`?limit=&offset=` (ceiling 200; out of range is a 400, not a clamp). Users,
models and groups also take `?q=` for case-insensitive substring search.
Reports are aggregations, not listings, and return every row they summed.

There is **no HTML admin panel built by hand**: `/docs` is the operator API
console — Swagger, generated from the same schemas the endpoints validate
against. Sign in at `/auth/login` first so the session cookie travels with the
requests. The React console at `/console` is the human surface.

## Two authentication schemes, on purpose

`/v1` is for programs and uses revocable API keys that carry a billing group;
`/api` is for humans and uses OIDC sessions. There is no password sign-in:
people sign in through an OpenID Connect provider, the bundled Authelia or
your own.

- **API keys** are `gwk_...` secrets, shown once, stored as SHA-256 hashes —
  a slow KDF would buy nothing on 256 bits of entropy, but revocation must be
  instant.
- **OIDC** works against any provider. The gateway ships no identity
  provider of its own; the deployments in `deploy/` and the deploy kit can run
  Authelia beside it, and any other issuer is the same `GATEWAY_OIDC__*`
  configuration from the gateway's side. Discovery is read once at
  startup, so changing any `GATEWAY_OIDC__*` value needs a restart. Users are
  keyed on `(issuer, subject)`: changing the issuer re-provisions every user as
  a new row with no memberships at their next login.
- **OIDC access tokens on `/v1`** are accepted when
  `GATEWAY_OIDC__ACCESS_TOKEN_AUDIENCE` names an audience — what lets a
  first-party client stream as the person typing, and what a device-flow
  bootstrap needs.

## Providers and plugins

A provider record holds credentials (encrypted at rest) and a **plugin**
that carries its
vendor knowledge: which header names it wants, what unit it reports cost in,
whether it is a provider or a router.
In-tree plugins cover the generic OpenAI-compatible case (`generic`), OpenAI,
Anthropic (chat completions are served over its native Messages API, see below), Mistral, Nebius, Melious, OpenRouter, Cortecs and Tensorix, this
deployment's own extractor, and the search backends DuckDuckGo, Exa, Jina and
Linkup. More can install via the `llmp.providers` entry point.

Two consequences worth knowing:

- a per-row knob became per-plugin behaviour, so two providers of the same type
  that need different answers need two plugins;
- a plugin that does not read a cost reports none, which is the safe default —
  see [Accounting and quotas](accounting-and-quotas.md#three-cost-figures-one-meaning-each).

### The provider catalogue

Known providers (endpoint, plugin, region, data retention, dated price hints)
are described in YAML, shipped with the image and overridable from a mounted
directory. See [Provider catalogue](provider-catalogue.md).

### Importing models from a provider's catalogue

`GET /api/admin/models/discover` compares a provider's catalogue with what this
deployment serves and answers with rows to adopt, rows we still serve that the
provider has dropped, and the models the provider listed and left unpriced.
`POST /api/admin/models/import` adopts the rows an administrator names — never
"import everything"; the allowlist stays a human decision — and grants no
access: a newly imported model is invisible until a group is granted it.

Discovery also reads each entry's limits, priced or not: `context_size`,
`context_length`, `context_window`, `max_context` or `max_input_tokens` (first
one present) is the model's context window, and `max_output_tokens` (or
`max_completion_tokens`) its output limit. An import writes them onto the new
model's `context_window` and `max_output_tokens`, including a model imported
with a manual price. A catalogue never fills the operator's own
`max_input_tokens` cap, which stays empty.

A model the provider lists **unpriced** is refused by the import, and the
refusal stands: an unpriced model serves happily and records a cost of zero,
which is a quiet way to give away money. What the import does accept is a price
the operator supplies with the request — `manual_price` on the model item, the
same amounts and money-as-string shape the model page's price form posts
(`input_per_mtok` and `output_per_mtok` per million tokens, `per_page` for an
OCR model, `per_image` and `per_extra_megapixel` for an image model, and an optional `currency` that defaults to the gateway's billing
currency and is refused in any other, never converted). The price must carry
the rates the kind is metered on — a chat model an input and an output rate, an
embedding or rerank model an input rate, a decision model an input and an output rate (zero is accepted, absent is not), an OCR model a page rate, a transcription model a rate per minute of audio (`per_minute`), an image model a `per_image` or an input rate; a search tier takes no
model price at all: a search backend is priced per request on the Web search screen instead. Each kind's missing rate is
refused per item, so one bad price does not lose the rest of the batch.

A hand-typed price may also carry `cache_read_per_mtok` and `cache_write_per_mtok`,
the same optional, non-negative amounts the price form takes (on any kind, as
there); they land on the price row and in its history, and when absent the
cached slice of a prompt is billed at the input rate.

A catalogue price — the provider's own, or one the community file supplied when
the import was made with `fill_missing_prices` — always outranks a hand-typed
one: the counterparty's figure is the one that will actually invoice. When a
manual price is set aside for that reason, the import result says so in its
`reason`. Whichever figure was written, the append-only price history records
where it came from: `catalogue`, `community`, or `manual` for one a person
typed (ADR 0053), which is what makes a wrong figure traceable to the choice
that took it.

### Anthropic models over chat completions

A model whose provider uses the `anthropic` plugin is called on Anthropic's
native `/v1/messages`, not on its OpenAI-compatibility endpoint, which has no
prompt caching, no thinking signatures and no PDFs. Clients keep sending and
receiving the OpenAI shape on `POST /v1/chat/completions`; only the call to
Anthropic differs. The provider's base URL is `https://api.anthropic.com/v1`.
`POST /v1/messages` is unchanged: it still passes the Messages shape through.

The translation sits at the upstream boundary (`gateway.translate`).
Resolution, access, quotas, **redaction** and the ledger all run on the OpenAI
request before it is translated, and the translated reply goes back through the
same response stages, so placeholders are restored in the text, the reasoning
and the tool-call arguments exactly as for any other provider.

**What is translated.**

| OpenAI | Messages |
|---|---|
| `system` / `developer` messages at the start | top-level `system` blocks, in order |
| a `system` message later in the conversation | a user text block `[system message] ...` in that position (not folded into `system`, which would change the cached prefix) |
| `image_url` data URL / http(s) URL | base64 / URL image block (JPEG, PNG, GIF, WebP) |
| `file` part with `file_data` (PDF, or `text/*`) | `document` block (base64 PDF, or text); `file_id` is refused |
| assistant `tool_calls`, `tool` messages | `tool_use` and `tool_result` blocks; consecutive results share one user turn; arguments that are not a JSON object are sent as `{"_invalid_arguments": "<raw>"}` |
| `tools`, `tool_choice`, `parallel_tool_calls: false` | `tools` (`input_schema`), `tool_choice` (`none`, `auto`, `any`, `tool`), `disable_parallel_tool_use` |
| `max_completion_tokens` / `max_tokens` | `max_tokens`; when absent the model row's `max_output_tokens`, else `GATEWAY_ANTHROPIC__DEFAULT_MAX_TOKENS` (8192) |
| `stop`, `user` | `stop_sequences`, `metadata.user_id` (hashed when it looks like a name or an email) |
| `response_format: json_schema` | `output_config.format` (`additionalProperties: false` is added to objects that lack it) |
| `reasoning_effort` | see below |

The conversation is made to alternate roles (same-role messages merge) and to
start with a user turn (`(conversation start)` is inserted when it does not).
`reasoning_content` on an assistant message is ignored: it carries no signature.

**Refused with a 400:** `n` above 1, `logprobs`, audio, tools that are not
functions, `response_format: json_object` (use `json_schema`), `file_id`, other
part types. These are refused before the reservation, so they leave no ledger
row. **Dropped without error:** `frequency_penalty`, `presence_penalty`,
`seed`, `logit_bias`, `store`, `service_tier`, `prediction`, `stream_options`
(usage is always read from the stream; the caller sees it only if it asked).

**What the model accepts decides what is sent**, from the model id
(`ModelTraits`): Claude 4.7 and later refuse `thinking: {type: "enabled"}`, so
`reasoning_effort` becomes `thinking: {type: "adaptive", display: "summarized"}`
plus `output_config.effort` (`low`, `medium`, `high`; `xhigh` and `max` pass
through). Claude 4.5 and earlier take a budget instead (low 1024, medium 4096,
high 16000, never above half of `max_tokens`). `none` turns thinking off where
the API allows it and sends nothing where it does not (Opus 5.5 always
thinks). Absent, no thinking parameter is sent. Claude 4.7 and later refuse
`temperature` and `top_p`, so they are dropped; older models take `temperature`
(or `top_p`, not both), and neither while manual thinking is on. Opus 5.5,
Sonnet 5.5, Fable 5.1 and Mythos 5.1 refuse forced tool use, so
`tool_choice: "required"` becomes `auto` there. An id the gateway cannot place
is treated as the newest generation.

**Thinking and tool loops.** Anthropic requires the thinking blocks of a turn,
with their signatures, to come back with the tool results. The OpenAI shape
cannot carry them, so the gateway keeps them: when a reply has thinking and
tool calls, the blocks are stored in Valkey (a bounded in-process dictionary if
there is none), keyed by the caller and each tool call id, for
`GATEWAY_ANTHROPIC__THINKING_TTL_SECONDS` (default 3600) and put back, in their
original order, in front of the `tool_use` blocks of the next request that
carries those ids. Another caller's ids, another model and expired entries find
nothing. A request that finds nothing is still sent: adaptive thinking models
accept the turn without the blocks; with manual thinking (Claude 4.5 and
earlier), where the API would answer 400, thinking is switched off for that
request. The reply's thinking text is `reasoning_content` (the 5.x models
return it empty unless asked for a summary, which `reasoning_effort` does).

**Prompt caching.** `GATEWAY_ANTHROPIC__PROMPT_CACHE` is `auto` (default) or
`off`. With `auto` up to four `cache_control` breakpoints are placed: the
newest user-role turn (the last `tool_result` of a loop), the last system
block, the last tool, and the user-role turn before the newest, so each step of
a tool loop reads what the previous one wrote. A `cache_control` the client
sent on a part, message or tool is kept and counts against the four; when there
is one, no automatic breakpoint adds a TTL. A prompt below the model's minimum
cacheable length (512 to 4096 tokens, by model) is simply not cached. The cost
is a trade: a cache write is billed above the input rate (1.25x for the
5-minute TTL), a read far below it (0.1x on most models), so `auto` pays off
from the second request inside the TTL and costs 25% more on the cached part of
a prompt that is never repeated. `GATEWAY_ANTHROPIC__PROMPT_CACHE_TTL=1h` makes
the breakpoints last an hour (writes at 2x input); the gateway bills one-hour
writes at 1.6 times the model's `cache_write_per_mtok`, so keep that field at
the five-minute price.

**Usage and money.** `prompt_tokens` is input plus cache reads plus cache
writes (the OpenAI convention); `prompt_tokens_details.cached_tokens` is the
reads and `cache_write_tokens` the writes (`cache_write_1h_tokens` the
one-hour part). The ledger reads that exactly as it reads Anthropic's own
usage on `/v1/messages`, so the two surfaces bill the same tokens the same:
the remainder at the input price, reads at the cache-read price, writes at the
cache-write price, and thinking tokens as output (Anthropic includes them in
`output_tokens`). `finish_reason`: `end_turn` and `stop_sequence` are `stop`,
`max_tokens` and `model_context_window_exceeded` `length`, `tool_use`
`tool_calls`, `refusal` `content_filter`.

**Errors** keep their status and become OpenAI-shaped; `529` (overloaded)
becomes `503`, which failover treats like any 5xx. A stream that ends before
`message_stop` ends with an error frame, never silently.

**Models and prices.** Discovery on an Anthropic provider reads `GET /v1/models`
(every page, `x-api-key`): id, display name, context window, output limit, and
from the capabilities images, PDFs, tools, reasoning and structured outputs.
Anthropic publishes no price API, so the built-in catalogue entry
(`catalogue_data/providers.d/anthropic.yaml`) carries the public price table
(USD per Mtok, with the date and URL it was read), applied to every listed model
without ticking "fill missing prices"; a model with no row is listed unpriced
and cannot be imported without a typed price. Two things a price row cannot
say: Haiku 5.5 is priced in two tiers by prompt size, and the table holds the higher
one (above 100,000 prompt tokens) so that no prompt is under-billed, at the cost
of over-billing short ones; and `inference_geo: "us"` costs 1.1x (the gateway never
sends it). Update the table, or override it from the operator directory, when
Anthropic changes prices.

To check the whole path against the real API (a few small requests on Haiku and
Sonnet 4.5):

```bash
ANTHROPIC_API_KEY=... uv run pytest apps/gateway/tests/test_anthropic_chat_live.py -s
```

## Reranking: `/v1/rerank`

A model of kind **rerank** is served by `POST /v1/rerank`, in the shape Cohere
and Jina made the standard. A model of another kind is refused with a 400 naming
the route that serves it, and a rerank model is refused on the other routes.

```json
{"model": "qwen3-reranker-4b", "query": "capital of France?",
 "documents": ["Paris is the capital of France.", {"text": "Berlin is in Germany."}],
 "top_n": 1, "return_documents": true}
```

```json
{"id": "score-1", "model": "qwen3-reranker-4b",
 "results": [{"index": 0, "relevance_score": 0.96, "document": {"text": "Paris is the capital of France."}}],
 "usage": {"prompt_tokens": 251, "total_tokens": 251}}
```

| Field | Meaning |
|---|---|
| `query` | Required, non-empty |
| `documents` | Required, 1 to `GATEWAY_RERANK__MAX_DOCUMENTS` (1000) strings or `{"text": ...}` objects, none empty; the query and documents together at most `GATEWAY_RERANK__MAX_TOTAL_BYTES` (2 MiB of UTF-8). Over a limit, or malformed: 400, naming the field or index |
| `top_n` | Optional, at least 1; the results are cut to it (more than the document count is the document count) |
| `return_documents` | Optional, default `false`: each result carries `document: {"text": ...}`, taken from **your** documents by `index` |
| other fields | Passed to the provider as sent |

`results` are ordered by descending `relevance_score`; `index` is the position
of the document in the request. The gateway normalises what providers disagree
on: documents always go upstream as plain strings (Cortecs refuses objects),
`return_documents` is applied here whatever the provider does with it (Melious
honours it, Cortecs ignores it), and the reply's `usage` is read from wherever
the provider puts it (Cortecs: `usage`; Melious: `usage: null` and
`meta.billed_units.total_tokens`).

**Metering and pricing.** Per input token, at the model's input rate (a rerank
model needs no output rate; scores are not text). The settled figure is the
provider's reported count; when it reports none, the estimate is used and
recorded as such, as for embeddings. The reservation counts the query once per
document, because the provider scores each pair. No provider seen prices per
search, so there is no per-search rate. `kind` is discovered from Cortecs
(`Rerank` tag, `output_modalities: ["rerank"]`) and Melious (`type: "reranker"`),
listed by `GET /v1/models` (`kind: "rerank"`; `?include=rerank` for just these)
and addable by hand in the console (Models, kind Rerank) and, for a private
provider, by a user.

**Redaction** applies to the query and every document, as to embedding input:
the redacted text is what leaves, placeholders are deterministic so a query and
the documents it names agree, and the reply's `document` is your own text, not
the provider's.

## Speech to text: `/v1/audio/transcriptions`

A model of kind **transcription** is served by `POST /v1/audio/transcriptions`
(and `/v1/audio/translations`, speech to English text, which is the same
handler without `language`). The request is OpenAI's multipart form, so any
client that speaks it works unchanged:

| Field | |
|---|---|
| `file` | The audio. Required. Held in memory for the request and forwarded as it arrived |
| `model` | A transcription model the caller is granted. A model of another kind is refused with a 400 naming the route that serves it, as on every other surface |
| `language`, `prompt`, `temperature`, `timestamp_granularities[]` | Forwarded to the provider (`language` is not sent on translations) |
| `response_format` | `json` (default), `text`, `verbose_json`, `srt` or `vtt`. Forwarded as asked and returned as the provider answered it; `text`, `srt` and `vtt` come back as text with the provider's content type |

It is never streamed. A file over `GATEWAY_TRANSCRIPTION__MAX_UPLOAD_BYTES`
(default 25 MB, OpenAI's own limit) is refused with a 413 before it reaches the
provider, from the declared length where the client sent one and from the bytes
otherwise. `GATEWAY_TRANSCRIPTION__TIMEOUT_SECONDS` (default 300) bounds how
long the provider may take; the shared upstream client has no read timeout, as
streams idle.

**Pricing is per minute, metered per second.** The price row carries
`per_minute` (the console's **Per minute of audio** field); the ledger records
`audio_seconds` and the cost is `seconds / 60 × per_minute`, so a 90-second clip
is a minute and a half, not two minutes. Where the seconds come from, in order,
and what the row says about it (`usage_records.audio_seconds_source`):

1. **`upstream`**: the provider's own duration, from `verbose_json`'s `duration`
   or `usage.seconds` on a `json` reply. It is what the provider invoices.
2. **`measured`**: read from the file's header (WAV, and MP3, FLAC, Ogg and Opus,
   M4A and a few others up to 2 MB) by `tinytag`, MIT and pure Python, plus the
   standard library's `wave`. Used when the reply carries no duration (`text`,
   `srt`, `vtt`). The header is the caller's, so it is checked against the
   file's size: a header that claims fewer seconds than the size allows at the
   densest plausible rate (the WAV's own rate, otherwise 1.5 Mbit/s) is replaced
   by that floor and labelled `estimated`. The MP3 reader is slow on crafted
   input, so it runs in a thread and only on files up to 2 MB.
3. **`estimated`**: the file's size at 32 kbit/s, for a format neither reader
   knows (or a longer one than the reader is run on).

Only `upstream` is exact: the row's usage source is `upstream_exact` for it and
`estimated` for the other two, so reports count them with the other estimates.

The quota reservation is made before the call, from the file's measured length
whenever it can be read; for a file that cannot be read it is a pessimistic
bound from the size (8 kbit/s). Settlement replaces it with the figure above, so
a provider that reports more than the header said is charged what it reported.
`x-bill-to`, model access groups and quota rules apply as on any `/v1` route.

A model whose price has a per-token rate and no `per_minute` (gpt-4o-transcribe
reports tokens, not a duration) is billed on the tokens the provider reports. A
model with a price row that has neither is refused with a 400
(`no_per_minute_price`) rather than served free; a model with no price row at
all is unmetered by cost, as for every other kind. The upload is counted as it
arrives, so a chunked request with no declared length is cut off with a 413 at
the cap and never spools more than the cap plus the form's framing to disk; the
provider call is bounded in total time by the timeout, not per chunk.

A catalogue entry is imported as a transcription model when its tags say so
(`Transcription`, `STT`, `ASR`, `speech-to-text`), or when it takes **only**
audio in and gives text out: a chat model that also hears audio takes text as
well, and stays a chat model. A rate is read only from a key that names its
unit (`per_minute`, `transcription_cost_per_minute`, `audio_cost_per_minute`),
plus Cortecs's `pricing.audio_cost`, which is per **second** and is multiplied by
60 for a model imported as transcription (on a chat model that hears audio it is
the price of audio input and is left alone). A model whose rate is quoted any
other way is imported unpriced and takes a hand-typed per-minute rate like any
unpriced model, rather than a guessed one. The reply's duration is read from
`duration`, `usage.seconds` or Cortecs's `usage.audio_duration_seconds`;
`segments` and the rest of the reply pass through untouched. Only the OpenAI
form fields are forwarded to the provider: Cortecs routing fields
(`preference`, `allowed_providers`, `eu_native`) are not, as the gateway has no
pass-through for them on this route.

Cortecs answers an untagged `/models` request with the `Instruct` slice only, so
to see its transcription models choose the **Transcription** type in Discover.

**What Cortecs does with it** (checked live, 2026-10-31, through this route, with
`whisper-large-v3-turbo` and `voxtral-mini-transcribe-3.0`; the gateway passes
the provider's answer through as it is, so these are the provider's limits):

- `json`, `verbose_json` and `text` all return a JSON object, `{"text": ...,
  "usage": {"type": "duration", "seconds": n, ...}}`: `text` is not plain text,
  and `verbose_json` carries no segments or `duration` field. The billed duration
  is the `usage.seconds` figure.
- `srt` and `vtt` are refused by the provider with a 400 ("not currently
  supported"); the gateway returns that refusal and records no cost.
- `/v1/audio/translations` answers 404 (`{"error": "Not Found"}`): Cortecs does
  not serve it. The route exists for providers that do.
- The reported seconds are the provider's own and are not always the file's
  length: whisper reports whole seconds rounded up (a 11.2 s clip is 12 s), and
  the same 11.1 s MP3 was reported as 12 s on one call and 6 s on another, which
  looks like different backends measuring differently. The ledger records what
  the provider reported (`audio_seconds_source` = `upstream`), because that is
  what it invoices; compare with the file's own length when reconciling.
- Per-minute prices as discovered under the Transcription type, in EUR:
  `whisper-large-v3-turbo` 0.00077, `whisper-large-v3` 0.00167,
  `voxtral-mini-transcribe-2602` 0.0033, `voxtral-mini-transcribe-3.0` 0.00353,
  `voxtral-small-transcribe-3.0` 0.00404, `faster-whisper-large-v3` 0.00532.
  The provider's own `usage.cost` (micro-euros) agreed with the gateway's figure
  to the rounding on every call.

**Redaction does not apply to the audio.** See
[Audio is not screened](redaction.md#audio-is-not-screened) for what that means
and the one switch an operator has, `GATEWAY_TRANSCRIPTION__ALLOWED_UNDER_REDACTION`.

## Images: `/v1/images/generations`

A model of kind **image** is served by `POST /v1/images/generations` (OpenAI's
JSON shape; `n` is 1 to 10, `size` is `WxH`). The prompt is redacted and
`revised_prompt` is restored; the pictures are not touched. It is priced per
image, or per token for providers that report usage (OpenAI's newer models).

**Price fields.** `per_image` is the rate for one picture. `per_image_by_size`
(`{"1792x1024": "0.08"}`) gives a size its own rate. `per_extra_megapixel` is
charged for each megapixel above the first, per image:

    per_image + max(0, megapixels - 1) x per_extra_megapixel      megapixels = W x H / 1,000,000

so 1536x1024 (1.572864 MP) at 0.013 + 0.001 per extra MP is 0.013572864. A size
listed in `per_image_by_size` always wins (`1792 x 1024`, `1792X1024` and
`1792×1024` are all `1792x1024`). The megapixels are the provider's measured
ones (`usage.out_images_mpx`) when it reports them, with or without a size in the
request, and otherwise come from the size. With neither the flat `per_image`
applies, so **the admission reservation for a request that names no size uses the
flat rate**; the settled cost uses what came back. The usage row records the
measured figure in `usage_records.image_megapixels` beside `image_size`, so a row
can explain its own cost. The ledger records `image_count` and `image_size`; the usage
reports show the money in the image cost. Migration 0076 adds the nullable
`model_prices.per_extra_megapixel` and `usage_records.image_megapixels`.

**Cortecs mapping.** Choose the **Image-Gen** type in Discover. An entry with
output modality `image` imports as kind image, with `pricing.output_cost_per_image`
as `per_image` and `pricing.output_cost_per_additional_megapixel` as
`per_extra_megapixel` (both per image, in the catalogue currency, no divisor).
Cortecs lists `input_token` and `output_token` as 0.0 on every image model, so an
image model with **no** `output_cost_per_image` (`stable-diffusion-xl-base-1.0`)
is treated as unpriced rather than free and takes a hand-typed price (Discover
asks for a per-image rate or token rates, and an optional per-extra-megapixel
rate). Rates seen
on 2026-11-01, EUR per image: `z-image-turbo` 0.009, `flux-1-schnell` 0.0288,
`flux-2-klein-4b` 0.013 (+0.001 per extra MP), `qwen-image-2512` 0.075.

**Upgrading: image models imported before 0.22.** Earlier releases read only the
token rates, so a Cortecs image model with no `output_cost_per_image` (SDXL) was
imported at 0 / 0 and generates pictures for free, and a re-import skips it
because it is not in the importable set now. The console flags an image model
whose price row charges nothing in any unit as **Unpriced (free)** in the model
list. Set a price on its page (a new price row applies from now on); nothing is
changed automatically, because a zero price on an image model can also be a
deliberate choice. A price change through a catalogue re-import keeps the
operator's `per_image_by_size` and `per_search` values.

**What Cortecs answers** (checked live, 2026-11-01, through this route):

- The reply is `{"created", "size", "data": [{"b64_json", "url", "revised_prompt"}],
  "usage": {..., "out_images_mpx", "cost_details": {"image_generation_cost"}}}`.
  Every entry carries both `b64_json` and `url`, one of them null. With no
  `response_format` Cortecs answers **`b64_json`** (JPEG), so a caller that wants
  bytes needs nothing extra; `usage.cost` is absent and `image_generation_cost`
  is in micro-euros (z-image-turbo 9000 = 0.009 EUR).
- **The delivered image can be smaller than the size asked for, and `size` in the
  reply echoes the request.** flux-2-klein-4b answered 1536x1024, 2048x2048 and
  1344x768 requests with 1024x1024, 1024x1024 and 1024x768 JPEGs, reported
  `size` as requested, and reported `usage.out_images_mpx: 1` (per image: it
  stayed 1 for `n` = 2). The gateway therefore bills extra megapixels from
  `usage.out_images_mpx` when the provider reports it, and from the size
  otherwise, so a 2048x2048 request is billed 0.013 (as Cortecs billed it), not
  0.0162. `image_size` on the ledger row is the size reported, which is not
  always the size delivered.
- Latency: about 1.5 to 2 s for flux-2-klein-4b, about 5 s for z-image-turbo.

**`url` versus `b64_json`.** The route does not fetch or rewrite anything: the
provider's `data` array is returned as it came, and the caller's
`response_format` is forwarded as sent. A provider that answers with a `url`
(OpenAI's dall-e family by default; the link usually expires) therefore gives the
caller a `url`, and a caller that needs bytes must either send
`response_format: "b64_json"` or download the link itself. The gateway does not
default `response_format`, because some providers (OpenAI's gpt-image models)
reject the field. Cortecs needs no default, as above.

## Document extraction: which formats are read

`POST /v1/ocr` against the deployment's own extractor reads documents that
travel with the request (a base64 data URI; URLs are refused, not fetched). The
extractor decides the format from the file's **bytes**, not from the declared
type or the file name, so a legacy `.doc` renamed to `.docx` is still read.

| Format | Read? | How |
|---|---|---|
| Word `.docx` | yes | markitdown (mammoth) |
| Word `.doc` (pre-2007, binary) | yes | `antiword`, installed in the extractor image |
| Excel `.xlsx`, `.xls` | yes | markitdown (openpyxl, xlrd) |
| PowerPoint `.pptx` | yes | markitdown (python-pptx) |
| PDF with a text layer | yes | markitdown (pdfminer) |
| PowerPoint `.ppt` (pre-2007) | no | refused: save as `.pptx` |
| Password-protected Office files | no | refused: remove the password |
| Scans and images | no | refused as `no_text_layer`: use an OCR model |

### PDFs with scanned pages: page images

A PDF page with no text of its own is a picture. A caller with a vision model
can ask the local extractor to draw those pages by adding
`"page_images": {"max_pages": 20, "long_side": 1280}` to the `/v1/ocr` body
(`max_pages` is clamped to 50, `long_side` to 2048, defaults 20 and 1280). Each
page is judged on its own: more than about 40 non-whitespace characters and it
is text, otherwise it is an image. When any page is an image the answer is a
`200`, whether or not other pages had text:

```json
{
  "pages": [{"index": 0, "markdown": "typed cover sheet"},
            {"index": 1, "markdown": ""}, {"index": 2, "markdown": ""}],
  "page_images": [{"page": 2, "mime": "image/jpeg", "data": "<base64>"},
                  {"page": 3, "mime": "image/jpeg", "data": "<base64>"}],
  "page_count": 3,
  "truncated": false,
  "usage_info": {"pages_processed": 3}
}
```

`pages` has one entry per PDF page, in order, with empty markdown for the image
pages; `page_images[].page` is 1-based. `truncated` is true when more pages were
images than were returned (the page cap, or about 15 MB of encoded images).
`pages_processed` counts pages read as text plus pages drawn. Without
`page_images`, a scan is refused as before. A remote OCR model never receives
the field. Drawing uses pypdfium2 (Apache-2.0 / BSD-3-Clause) in the extractor.

The images are pictures, so **redaction cannot inspect them**: the policy
applies to the text pages only. A deployment that relies on redaction to keep
personal data from a caller should not grant that caller the local extractor
with `page_images`.

A document that is not read is never returned as an empty success. The response
is a `422` whose `error.code` names the class (`unsupported_document`,
`unreadable_document`, `no_text_layer`, `document_too_large`) and whose
`error.message` carries the extractor's specific reason, for example
*"old PowerPoint .ppt (pre-2007) is not supported; save as .pptx"*. An image
that predates the antiword package (a custom build without it) answers
*"Old Word .doc files need antiword in the extractor image"* for `.doc` files.

Legacy `.doc` extraction runs `antiword -w 0 -m UTF-8.txt` as a separate
process with a 30-second limit and no shell; antiword is GPL-2.0 and is
executed, not linked.

## Things that are easy to get wrong, and where they are handled

| Trap | Where |
|---|---|
| Streamed responses carry no token counts unless `stream_options.include_usage` is forced | `routers/chat.py:build_upstream_payload` |
| The usage frame must be stripped if the client did not ask for it — but only the *usage-only* frame may be dropped whole | `routers/chat.py:usage_visibility_stage` |
| SSE must be parsed at `\n\n` boundaries, not network chunk boundaries | `sse/parser.py` |
| A trailing `\r` split across chunks is ambiguous and must be held back | `sse/parser.py:_pop_line` |
| httpx's 5s default read timeout kills long streams | `upstream.py:build_http_client` |
| Client disconnect must close the upstream *and* still record accrued usage | `routers/chat.py:spawn_finalisation` |
| A client that hangs up the instant the stream ends must not cancel the settling write | `routers/chat.py:settle_completed` |
| Assistant output must be persisted as it streams, not only at the end | `accounting/recorder.py:maybe_flush` |
| An absent usage frame must not be recorded as zero spend | `accounting/tokens.py` |
| Concurrent requests must not each pass the same under-limit check | `quota/engine.py:check_and_reserve` |
| Money must never touch a float, including inside the counter store | `types.py`, `quota/counters.py` |
| A wiped counter cache reports zero spend, not an error, so every group gets a fresh budget | `quota/engine.py:rebuild_if_cache_is_cold` |
| Editing a price would rewrite what past requests cost, so prices are append-only | `routers/admin.py:create_price` |
| Deleting a model must not lose the spend recorded against it: `usage_records.model_id` is `ON DELETE SET NULL` and every row keeps `model_name` | `routers/admin.py:delete_model` |

## Layout

```
src/gateway/
  config.py         all configuration; nothing else reads the environment
  models.py         SQLAlchemy ORM — the usage ledger lives here
  pagination.py     the listing envelope, its bounds and the count query
  protocols.py      per API surface: where usage, the served model and the
                    assistant text live in a response frame
  routers/_metered.py  resolve, reserve, record, settle — shared by every
                    metered /v1 route so the ordering cannot drift between them
  types.py          Money (Numeric, never float) and UTC-safe datetimes
  security.py       API key generation, SHA-256 hashing, verification
  oidc.py           discovery, PKCE, ID token validation, group claim mapping
  upstream.py       the provider client, with read timeout deliberately None
  sse/              event-boundary parsing and the transform pipeline
  accounting/       token counts, cost, and the ledger writer
  quota/            rolling windows, counter stores, reserve-then-settle
  redaction/        the interface, the buffering rewriter, the resolver
  plugins/          provider plugins: facts about a counterparty, no arithmetic
  routers/          HTTP endpoints: /v1, /auth, /api/me, /api/admin, /console
```

## Known gaps

Stated plainly, so none of them is a surprise later.

- **OIDC is fully verified against Authelia 4.39.22** (and was against
  Keycloak 26.7, which is no longer part of the dev stack). Entra ID, Google and others differ in exactly
  the places the OIDC settings make configurable — where
  groups live, whether they appear in the ID token at all, how they are named.
  Test any new provider against your instance before relying on it; see the
  [OIDC guide](oidc-generic-provider.md) for the checklist.
- **Assistant text on the ledger is personal data, with a bounded lifetime.**
  Deleting a person (the Users page) removes their account, keys, memberships
  and bundled login, and erases them from the chat
  ([Merging and deleting accounts](identity.md#merging-and-deleting-accounts)).
  Identity foreign keys on the ledger use `ON DELETE SET NULL`, so the
  financial rows survive (tokens, cost, timestamps), and the delete **blanks
  `assistant_text` on that person's rows** in the same transaction. For
  everyone else, retention clears the text from every usage row after
  `GATEWAY_TRANSCRIPT_RETENTION_HOURS` (default 24; `0` keeps it for ever). The
  sweep runs every `GATEWAY_TRANSCRIPT_SWEEP_SECONDS` (default 600) and
  measures age from finalisation, or from the last streaming flush for a row
  that never finalised, so an answer still being written is never cleared. The
  text exists so a suspended client can recover an answer; a recovery after the
  window finds the row but not the text.
- **The redaction HMAC key must be backed up with the transcripts it
  labelled.** Losing or rotating it breaks cross-turn placeholder consistency
  for existing conversations.
- **Counters rebuild from the ledger only at startup.** If Valkey is wiped
  while the gateway keeps running, the cache reports zero and quotas are
  briefly too permissive until the gateway restarts. An empty cache is not a
  failed read, so nothing detects it at runtime. Restart the gateway after any
  cache loss; a counter that has drifted from the ledger is shown by **Quota
  health** on the console's Quotas page and corrected by **Reconcile** (see
  [Accounting and quotas](accounting-and-quotas.md#quota-health-and-reconcile)).
- **Load behaviour is measured on one small box only.** The gateway's own
  overhead per request is small next to upstream latency, but profile before
  scaling.
