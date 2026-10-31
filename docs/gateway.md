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
| `POST /v1/decisions`, `POST /v1/systemone` | key or bearer | [Decision models](decision-models.md): typed questions about a text, answered with calibrated probabilities. Metered per token, redacted, the request and the answer passed through in the format they arrived in |
| `POST /v1/images/generations` | key or bearer | Image generation, billed per picture or per token depending on the model |
| `POST /v1/ocr` | key or bearer | Document extraction, metered per page. Two backends: an upstream OCR model, or this deployment's own extractor |
| `POST /v1/audio/transcriptions`, `POST /v1/audio/translations` | key or bearer | [Speech to text](#speech-to-text-v1audiotranscriptions), OpenAI's multipart shape, metered per second of audio and priced per minute. The audio is not screened by redaction |
| `POST /v1/search` | key or bearer | Web search against a configured backend (Linkup, Exa, Jina, DuckDuckGo), metered per call and billed per request at the backend's price ([Web search: a price per request](accounting-and-quotas.md#web-search-a-price-per-request)). The backend is the billing group's search policy, or the one the body names in an optional `backend` (a search model the caller is granted; an ungranted name is a 404, never a fallback to the policy). `POST /v1/search/{backend}` is the verbatim passthrough |
| `GET /v1/models` | key, bearer or none | Models this caller may use — billing-group grants (or the group named by `x-bill-to`), personal grants, and public models. Unauthenticated: the full catalogue, brochure fields only |
| `GET /v1/pystino/usage` | key or bearer | This caller's own spend, for a client that wants to show it without a console session |
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
Nine routers import it today: chat, responses, messages, embeddings, images,
ocr, transcription, search and decisions.
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
Anthropic, Mistral, Nebius, OpenRouter, Cortecs and Tensorix, this
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
OCR model, and an optional `currency` that defaults to the gateway's billing
currency and is refused in any other, never converted). The price must carry
the rates the kind is metered on — a chat model an input and an output rate, an
embedding model an input rate, a decision model an input and an output rate (zero is accepted, absent is not), an OCR model a page rate, a transcription model a rate per minute of audio (`per_minute`); a search tier takes no
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
