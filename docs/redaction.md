# Redaction

Redaction is the layer between the caller's prompt and the upstream provider:
detected entities are replaced with deterministic placeholders before any text
leaves the building, and restored (or not) in the answer the person reads. With a policy applied, what it matches never reaches the upstream. The shipped default applies none, so nothing is redacted until an administrator writes a policy and a pipeline that applies it.

It is also **~90% of the CPU**. Detection is named-entity recognition and
scales with prompt length — roughly 0.1ms of CPU per prompt token, against a
gateway cost that stays flat at 24–31ms. Capacity planning is redaction
planning; see [Measured performance](performance.md).

## Policies and pipelines

Two objects, one job each (ADR 0113, 0114; see [Privacy](privacy.md) and [Privacy
pipelines](privacy-routing.md)). A **redaction policy** is *what to look for*:
items in two groups, [patterns and entities](#items-two-groups-never-mixed), with
their detection settings (engine, thresholds, allow-list, placeholder style), and
**no mode and no scope**. A **pipeline** is *when and how*: a kind (extract or
content), a scope (who, which target, a model type) and ordered rules "When
policies + conditions -> mode". The **mode** (what to do with what the rule's
policies find) belongs to the rule.

Every rule of every pipeline in scope is evaluated, administrators' first, then
the user's own. A rule in mode `block` refuses the request; otherwise each
matching rule hides what its policies find, and **the first rule to hide a piece
of text decides how**. A policy is applied to the extracted text (extract), the
input (embedding and rerank), or messages and tool results (chat).

| | Redaction policy | Pipeline |
|---|---|---|
| Purpose | what to look for | when and how a request is handled |
| Scope | none | who, target and model type |
| Several apply | every rule that matches applies; the first to hide a span decides how | every rule of every pipeline in scope is evaluated, in order |
| Lives in | console: **Privacy, Redaction policies** | console: **Privacy, Pipelines** |

## Out of process, by contract

The detection service (`services/redaction`, Presidio) is a separate process
behind a wire contract defined in `packages/shared-py`. The split has two reasons:

- **The event loop must not block.** Detection is CPU-bound NER; in-process it
  would stall every concurrent request for the length of every prompt.
- **spaCy must never enter the gateway's lockfile.** The service is
  deliberately not a workspace member; it narrows what it uses to a Protocol,
  so the coupling is one interface.

The contract is one-directional on purpose: **the engine only ever detects
spans; it never invents placeholder text.** The gateway performs substitution
itself with `placeholder_for` from the shared package. That is what makes the
placeholder scheme stable across turns — the same entity yields the same
placeholder in turn 1 and in turn 40, keyed by an HMAC of the entity value with
`GATEWAY_REDACTION__PLACEHOLDER_KEY`. That key must be backed up with the
transcripts it labelled: rotating it re-labels every entity, so old
placeholders stop matching.

### Why not embed Presidio in the gateway?

The question recurs, so the numbers that answer it are recorded here. Embedding
would save one container and one HTTP hop, and the in-process seam exists — it is meant for engines where a
network hop is absurd, such as a pure-regex ruleset. NER is not that case:

- **Memory multiplies by worker.** Presidio with NER is ~900MB; the gateway runs
  two workers, so embedding means two model copies (~1.8GB) against one 900MB
  sidecar — a regression on a small box. One worker closes the gap but gives up
  the headroom.
- **Crash isolation is lost.** Today a detector crash is 502s
  (`fail_open=false`) and the gateway stays healthy. Embedded, a spaCy crash
  takes a gateway worker — and every stream it was serving — down with it.
- **Scaling stays separable.** Detection is ~90% of the CPU and scales with
  prompt length ([Measured performance](performance.md)); the sidecar scales
  without touching the gateway, and its ~150MB no-NER mode
  (`REDACTION_NLP_ENGINE=disabled`) covers pattern-only deployments cheaply.

Enabling and disabling redaction from the console needs no architecture change:
the engine choice is a UI decision that reaches every worker within ten seconds, and the sidecar is only
needed when the engine is `http`.

## The engine is a decision, not a build flag

Installed engines are described by a registry; which one runs is an admin
decision stored on an append-only `redaction_config` row. `RedactionResolver`
polls it every 10 seconds, so a change reaches every worker without a restart
and without a query on the request path. Switching to an engine that redacts
*less* than the current one takes an optional reason, kept permanently when
given — the row is append-only, so the history of that decision survives.

The shipped default is `noop`, which redacts nothing. Redaction being **on**
must be a choice somebody made, never a default someone forgot about.

## Items: two groups, never mixed

A policy's items come in two groups, in the API and in the console editor, and the
two are different in kind:

| | **Patterns** (fast, deterministic) | **Entities** (NER, probabilistic) |
|---|---|---|
| What | built-in formats, regular expressions, keyword lists | the entity types of the policy's NER engine; custom types for GLiNER |
| Where it runs | the built-in formats in the redaction service's recognisers (checksummed where there is one); the regular expressions and keyword lists in the gateway | the redaction service, per engine |
| Can fail | the gateway's never; the service's if it is down | yes: if the service or the engine is unavailable the request fails closed |
| Stored as | `formats`, `patterns`, `keywords` | `entities`, `custom_entities` |

- **Built-in formats** are the service's own `pattern_entities` (email, IBAN, card,
  the Italian identifiers, and so on). The console lists what the service reports.
- **Custom regular expressions** are evaluated in the gateway under RE2 (below).
- **Keyword lists** are a name, a comma-separated list of words and phrases, a
  **case sensitive** checkbox. They are found as **whole words**; spacing
  and punctuation between the words of a phrase do not matter ("data protection"
  finds "Data-protection"). Unless case sensitive, case is ignored and **accents are
  folded** ("perche" finds "Perché"). Evaluated in the gateway in **linear time**, as
  a trie over word tokens: no regular expression is ever built from what was typed.
  A list holds up to 1,000 entries, each up to 100 characters and six words.
- **Entities** are the types the chosen NER engine exposes, each with a
  confidence threshold. A policy chooses its **engine**, spaCy or GLiNER, among
  those the service reports available (none chosen: the deployment's default). With
  GLiNER an administrator may add **custom entity types**: an UPPER_SNAKE name and a
  natural-language label the model is given at inference ("project code name").

A policy stores no mode. A rule gives its policies one **mode**, with optional
overrides by entity type (see [What happens to a detected entity](#what-happens-to-a-detected-entity-the-rule-mode)),
including **`detect only`**: found and counted, **never hidden**, and recorded in the
log. A policy that exists only to be counted by a `finds` condition is a policy like
any other; the rule that uses it says what to do.

Nothing here is specific to any topic: no list ships, no template is seeded.

The policy editor has a **Try it** box (`POST /api/admin/privacy/policies/try`) that
runs the real detection over a text, with no side effects, and lists each item found
with its group, and the engine and score for entities.

Send a policy to the API with both keys (`formats` and `entities`): a body with only
`entities` is read as the older single table and split by name (PERSON, LOCATION, NRP
and ORGANIZATION are NER, every other name a built-in format), which is also how
policies written before the two groups, and `GATEWAY_REDACTION__POLICY`, still load.

### The redaction service's contract

`GET /healthz` reports `pattern_entities` and `ner_engines`, one entry per engine
(`name`, `available`, `default`, `entities`, `custom_labels`, `labels` (the labels
the engine always asks of its model), `ms_per_token`,
`languages`). `POST /detect` takes `ner_engine` (null is the service default) and
`custom_labels` (`[{entity, label}]`) and answers **409 `{"detail": {"code":
"engine_unavailable"}}`** for an engine it has not got. Spans carry `score` and
`engine` (`pattern`, `spacy` or `gliner`). When several policies in force choose
different engines the gateway calls the service **once per engine** and merges the
spans (identical range and type: the higher score), and the strictest mode of the
fold applies. Requested types or labels the answer names in `unsupported_types` are a
log line for redaction and, for a "finds" condition, make the count unavailable (never
"found nothing"); such an answer is not cached. An engine the service reports unavailable can neither be chosen nor,
if it goes away, be skipped: a request that needs it is refused with a 503 naming
the policy.

## Detection families

The HTTP detector has two independent Presidio families: pattern matching for
structured and checksum identifiers, and NER for names and places. Either
family can be on while the other is off, and rule building lists only the
entity types belonging to families that are actually enabled. Gateway custom
patterns are separate policy and are unaffected by the pattern-matching switch.

The switches live in the console's Presidio detection families card: one big
switch per family, saved immediately. Each switch starts from the deployment
default — the explicit console choice when one was saved, otherwise whether
the detector build actually offers that family — and flipping one writes an
explicit on/off choice. There is no third state to pick; the default is only
the starting position. Choices are stored on the same append-only engine row
as the engine choice and reach other workers on the resolver's poll, so
turning NER off has the same audit trail as changing engines. Deployment
defaults can also be set with `GATEWAY_REDACTION__PRESIDIO_PATTERN_MATCHING`
and `GATEWAY_REDACTION__PRESIDIO_NER`; a console choice overrides them.
Turning both families off is reported as a warning because the detector then
returns no findings.

Disabling a family never refuses, deactivates or rewrites rules. A rule that
names a now-undetectable type stays in force and carries a warning beside it,
naming the types whose values would reach providers unprotected; a rule whose
default mode is on gets a generic form of the same warning.

The console cannot conjure a recognizer the sidecar was not built to run. In
particular, a `REDACTION_NLP_ENGINE=disabled` deployment has no NER weights, so
enabling NER in the console does not make `PERSON` detectable; the service still
reports no model-backed entities. Use the console switch to choose among
installed capabilities, and the image/build setting to choose which
capabilities are installed.

## NER engines: spaCy and GLiNER

The detector service can host **two NER engines in one process** and a policy
picks one per request (`ner_engine` on `POST /detect`). Pattern and checksum
recognisers (cards, IBANs, fiscal codes, phones, emails) run whatever the engine;
only the model-backed types (names, places, organisations, and for GLiNER the
medical ones) come from the engine.

| | spaCy | GLiNER |
|---|---|---|
| Model | per-language `en_core_web_*` / `it_core_news_*` | `urchade/gliner_multi_pii-v1` (Apache-2.0, multilingual) |
| Entity types | `PERSON`, `LOCATION`, `ORGANIZATION`, `NRP` | `PERSON`, `LOCATION`, `ORGANIZATION`, `DATE_TIME`, `HEALTH_INSURANCE_ID`, `MEDICAL_CONDITION`, `MEDICATION` (the label map, `REDACTION_GLINER_LABELS`) |
| Custom entity types | no | yes, per request |
| Speed | about 0.2 ms per token | about 5 to 10 ms per token (CPU, this box) |
| Memory | about 0.7 GB (md) | about 2.5 GB resident (about 4.2 GB at load) |
| In every image | yes | only when built with `GLINER_MODEL` |

GLiNER is **optional at build time**: pass the model and pinned revisions as build
arguments, and the weights are baked in so the running container never needs the
network (`HF_HUB_OFFLINE=1`).

```bash
docker build -f services/redaction/Dockerfile \
  --build-arg GLINER_MODEL=urchade/gliner_multi_pii-v1 \
  --build-arg GLINER_REVISION=1fcf13e85f4eef5394e1fcd406cf2ca9ea82351d \
  --build-arg GLINER_TOKENIZER_REVISION=a0484667b22365f84929a935b5e50a51f71f159d .
```

Measured on a 5-core box with `en_core_web_md` and `it_core_news_md`: the image is
1.27 GB without GLiNER and 4.81 GB with it (CPU-only PyTorch, the `gliner`
package and 1.1 GB of weights). When the image has GLiNER it loads at startup
(about 20 s, with a one-off timing pass), and `GET /healthz` lists it in
`ner_engines` as available. When it has not, `ner_engines` still lists `gliner`
with `available: false`, and a request naming it is answered `409`
`{"detail": {"code": "engine_unavailable", "engine": "gliner"}}`: the gateway
fails closed rather than receive another engine's findings.

`GET /healthz` gains `ner_engines`, one element per engine:
`{name, available, default, entities, custom_labels, ms_per_token, languages}`.
`ms_per_token` is measured once at startup on a short synthetic text, so it
reflects the hardware the service is on; it is `null` when not measured.

Which engine a request without `ner_engine` gets is `REDACTION_NER_DEFAULT`
(`spacy` unless set to `gliner`, which needs an image with GLiNER). With `ner_engine`
null the answer is the deployment default's, as before. Every span carries
`engine` (`pattern`, `spacy` or `gliner`) next to its `score`, so a console can show
where a hit came from. GLiNER inference runs one request at a time (a semaphore in
the process), because a forward pass uses every core and two at once only slow
each other down.

### Custom labels

GLiNER takes its labels at inference, so an admin can add an entity type without
retraining anything. `custom_labels` is a list of `{entity, label}`: `entity` is an
`UPPER_SNAKE` type name (at most 64 characters) and `label` the natural-language
phrase the model is asked for (at most 80), at most 30 per request.

```json
{"texts": ["..."], "ner_engine": "gliner",
 "custom_labels": [{"entity": "PROJECT_NAME", "label": "project code name"}]}
```

Spans of that type are returned like any other (with `engine: "gliner"`). A
request that lists `entity_types` gets a custom type only if it names it. With
spaCy, or with the NER family switched off, custom labels are ignored and their
entity names come back in `unsupported_types`.

### What counts as a pattern format

`pattern_entities` lists only entity types a deterministic recogniser (a regular
expression or a checksum) finds; `model_entities` and each engine's `entities`
list the ones a model finds. The service reads the split from the recognisers
that are loaded, not from a list of names: Presidio's spaCy recogniser lists
`AGE`, `ID`, `EMAIL`, `PHONE_NUMBER` and `DATE_TIME` in its label map though the
stock models never produce most of them, and a recogniser that is not loaded
advertises nothing. A policy saved while `AGE` or `ID` was listed under formats is
kept: the console moves the item to the entities if the engine finds it, otherwise
keeps it turned off with a warning, and the API accepts a format the service does
not list only while it is `off`.

When the service has no NER engine at all (the pattern-only image, or
`REDACTION_NLP_ENGINE=disabled`), the policy editor hides the Entities section and
says so, the API refuses an entity item ("No NER engine is installed in the
redaction service; entities need one."), and My privacy offers no model-backed
type. Enable one as described below.

### Detector switches

The console's **Detectors** tab switches each detector on or off: the patterns
(`presidio_pattern_matching`), spaCy and GLiNER (`spacy_enabled`, `gliner_enabled`
on the append-only `redaction_config` rows, migration `0084`; `presidio_ner` is on
while either engine is). A detector an active policy uses cannot be switched off
(409 naming the policies), and a policy can only choose detectors that are on. The
whole of privacy is the **Privacy: On / Off** switch on the Pipelines tab, which
is the old `noop` engine under a plain name.

### Policy templates

The Redaction policies tab has **New policy from template** next to **New
policy**, and My privacy has the same for a user's own policies. A template is
an example, never seeded: choosing one opens a new, unsaved policy to edit before
saving. The examples are secrets and credentials, personal and banking data,
government IDs, contact details, network and infrastructure, and health
identifiers. A template adds only what the service reports: a format from
`pattern_entities`, an entity the policy's engine exposes, and the health types
only with GLiNER. Whatever is left out is named under the template in the picker
("Skipped: PERSON, LOCATION, which need an NER engine."). A user's template keeps
to the entity types the administrator made available, and has no keyword lists.

## Deploying less of it

Three shapes, and all three work:

| You want | How | What you get |
|---|---|---|
| **No redaction, no sidecar** | Leave `redaction` out of `COMPOSE_PROFILES` and the engine at `noop` | Nothing runs, and `noop` needs nothing. The same profile also runs `extractor`, the local document reader behind `/v1/ocr`, so it goes too. Reading scans and complex layouts is an option of that service: [extractor engines](extractor-engines.md) |
| **Pattern matching only, no NER** | The `redaction` profile as shipped: the published `pystino-redaction:<version>-pattern` image (built with `SPACY_MODELS=` empty) and `REDACTION_NLP_ENGINE=disabled`, the `.env.example` default; engine `http` | Presidio's pattern recognisers — cards, IBANs, emails, phone numbers, the Italian identifiers — at about 150MB and without the CPU cost that scales with prompt length |
| **Everything** | The `redaction` profile with the NER image built locally (`SPACY_MODELS=en_core_web_lg`, the Dockerfile's default; never published) named in `REDACTION_IMAGE`, and `REDACTION_NLP_ENGINE=spacy` | NER for the configured languages on top of the patterns |
| **Everything, on a small box** | The same, built with the medium models: `SPACY_MODELS="it_core_news_md"` (add `en_core_web_md` for English) | The same NER with models of about 40MB each, roughly twice as fast as `lg` and somewhat less accurate on names. `it_*` weights are CC BY-NC-SA 3.0; the build prints that notice |

Two things to know before choosing the middle row.

**"Pattern only" is not "without Presidio".** The regex recognisers *are*
Presidio's, so the sidecar is still deployed and still called per request —
what goes away is spaCy, the language models and the inference cost. There is
no in-gateway regex engine, and deliberately so: a detector in the request path
would be CPU-bound and synchronous, and would share the gateway's fate when it
crashes; that does not change because the detector got simpler.

**It is a deploy-time choice, unlike the engine.** `REDACTION_NLP_ENGINE` is
read by the sidecar at startup, so moving between NER and pattern-only means
restarting that service — whereas switching engine, or switching redaction off
entirely, is a console decision that reaches every worker in ten seconds. The
asymmetry is worth knowing when planning a change: one is a config edit, the
other is a deployment.

What keeps the middle row honest is that the service reports what it can
actually find. With NER off, `capabilities()` returns no models, marks the
language degraded, and drops `PERSON`, `LOCATION`, `NRP` and `ORGANIZATION`
from its entity list rather than advertising types it will never return —
including the compensation for phone numbers described in
`test_detector.py`, where a bare pattern match scores 0.4 and would otherwise
sit permanently under the gateway's default threshold while the entity was
still advertised.

## What redaction covers

Redaction applies to message text and tool results. It does not apply to tool
definitions (`tools`: names, descriptions and parameter schemas), `response_format`
schemas, `metadata`, `user`, or image URLs. Masking inside a schema could break a
tool name or an enum value that must round-trip exactly, so those fields are sent as
the caller wrote them. Do not put personal data in tool definitions.

## Rerank input

`POST /v1/rerank` redacts the query and every document before they leave, as
embeddings redact their input: the document is the sensitive half, so a rerank
route that forwarded raw text would bypass the policy. The reply echoes the
caller's own document text by index (never the provider's copy), so there is
nothing to restore.

## Audio is not screened

`POST /v1/audio/transcriptions` and `/v1/audio/translations` send the caller's
audio to the provider as it arrived. Redaction works on text, so **nothing is
detected, substituted or blocked in the audio**, whatever rules are in force; a
voice recording that names a person reaches the provider with the name in it.
The transcript that comes back is text and is returned as the provider wrote it:
nothing was substituted on the way out, so there is nothing to restore, and the
response is not screened either (the OCR route screens the text it returns
because there the document is the sensitive half; here the sensitive half is
the audio, which has already left).

The gateway does not pretend otherwise, and offers one control: a deployment
switch, `GATEWAY_TRANSCRIPTION__ALLOWED_UNDER_REDACTION`. It defaults to `true`,
so nothing changes on upgrade. Set it to `false` and a caller for whom an
*active* redaction rule applies is refused with a 403
(`transcription_not_allowed_under_redaction`) before anything is reserved or
sent. "Active" means the engine is not `noop` and the folded policy for that
caller (the policies of the chat pipelines in scope for them, judged without
content, or the deployment policy) has some entity type, the default, or a custom
pattern in a mode other than `off`, **or** a chat pipeline in scope refuses them
(audio has no text for a pipeline to judge, so a refusal that names the caller
counts as much as a policy). A rule whose mode is `off` or `detect only` does not count.
**Known limit:** chat pipelines are judged without content here, so a content pipeline whose rules depend on what the text says (for example "when a policy finds at least 1 item → block") cannot act on audio, and does not by itself block transcription; only a block that does not depend on content, or a rule that hides, does.
Leave it `true` only if you accept that audio from those callers reaches the
provider unscreened, although the same words typed as text would have been
substituted or blocked by their rule. If you do not accept that, set it to
`false`, or do not grant the transcription model to those groups.

## What happens to a detected entity: the rule mode

For each rule, a **mode** (and per entity type overrides) says what happens to what the
rule's policies find: two independent questions, what the model sees and what the
user gets back:

| Mode | Upstream sees | The person gets back |
|---|---|---|
| `off` | the real value | the real value |
| `detect_only` | the real value | the real value; the item is found, counted and recorded in the log, never hidden |
| `anonymise_restore` | `<PERSON_xxxx>`, stable | the real value, restored |
| `anonymise` | `<PERSON_xxxx>`, stable | the placeholder |
| `redact` | `<PERSON>` — lossy, two users collapse into one label | the label |
| `block` | nothing — the request is refused | an error saying which kind of value was blocked and by which rule, never the value itself (HTTP 403) |

`block` exists for values whose *presence* is the incident — a pasted API key —
where silently replacing it would tell nobody it happened. The 403 names the
entity type and the rule that decided, and **never echoes the matched text**:
an error body gets logged and pasted into tickets.

### Restoring the answer, and tool calls

With `anonymise_restore`, the real value is put back in what the model returns:
the answer, and the model's reasoning when the provider streams it (as
`reasoning_content`, `reasoning` or `reasoning_text`, or as reasoning summaries
on the Responses surface). Thinking blocks on the Anthropic Messages surface are
left as the provider wrote them: they are signed over their own text, and
restoring one would make the provider refuse the next turn.

A tool call's **arguments** are restored as well. The model only knows a value
by its placeholder, so when it calls a tool it writes the placeholder into the
arguments, and the tool needs the real value. The arguments are JSON text, so
the value goes back JSON-escaped: an original containing a quote, a backslash or
a line break leaves the arguments valid JSON. A streamed call is buffered per
tool call with the same hold-back rule as prose. This covers chat completions
(`tool_calls`), the Responses surface (`function_call`) and Anthropic Messages
(`tool_use`).

Two settings, both read-only on the Redaction service tab because they come from the
environment:

- `GATEWAY_REDACTION__RESTORE_IN_RESPONSE` (default on) puts real values back in
  the response at all. Off, callers receive the placeholders.
- `GATEWAY_REDACTION__RESTORE_IN_TOOL_ARGUMENTS` (default on) extends that to
  tool-call arguments. It only has an effect while the first one is on. Turn it
  off if a tool must never receive a value that was hidden from the model.

**Restored arguments reach the tools.** Whatever executes the call, including an
external connector or MCP tool the client runs, receives the real value. That is
the point of restoring them, and it is also the reason for the second setting:
turn `RESTORE_IN_TOOL_ARGUMENTS` off when a tool must never see a value that was
hidden from the model.

**What comes back in the next request is screened too.** A client replays the
assistant's tool calls and reasoning in the follow-up request of the same turn,
and since those were restored on the way out they carry the real values back in.
Request redaction therefore also covers the Responses `instructions` field and `tool_calls[].function.arguments`
(decoded, each string value redacted, and written back as JSON), the reasoning
fields, Responses `function_call`, `function_call_output` and `reasoning` items,
and Anthropic `tool_use` input and `tool_result` blocks, all into the same
placeholder map. A tool call with nothing to hide is sent byte for byte as it
came. A provider that sends `content` as a list of parts is restored too, and a
provider that streams tool calls without an `index` is told apart by `id`.

Only placeholders issued in mode `anonymise_restore` are ever restored;
`anonymise` and `redact` stay as they are everywhere.

### Telling the model what a placeholder is

A model that sees `<IT_FISCAL_CODE_Y5RD4GZTEK>` where a fiscal code belongs
tends to call it a malformed value, remark on its length, and ask the user for
the real one. So whenever a rule is **in force** for a request, the gateway
appends one short note to the end of the outbound system prompt (the `system`
message on chat completions, `instructions` on the Responses surface, `system`
on Anthropic Messages). A rule is in force when at least one entity type,
custom pattern or the default is in an anonymising mode (`anonymise_restore`,
`anonymise` or `redact`), whether or not this particular request matched
anything.

The note is a function of the **policy alone**, never of the request's content.
Providers cache the start of a request, and the system prompt is the start: a
note that came and went with the turns that happened to match something would
change that prefix and discard the cache for the whole history. The same policy
gives byte-identical text on every turn. It is appended after the caller's own
system prompt, never before it; on chat completions it joins the first system
message (a new one is created at index 0 only if there is none), and on Anthropic Messages
it is a separate final block, so blocks the caller marked with `cache_control`
are untouched.

There are two wordings. When any in-force entity is `anonymise_restore` and
restoring is on, the note says the real values are put back automatically in
the answer (and in tool calls, if `restore_in_tool_arguments` is on) and asks
the model to use them exactly as written without commenting on their format or
length. Otherwise it says they are hidden stand-ins that stay hidden and must
not be guessed. A third sentence describes `#...#` stand-ins when any in-force
entity uses `same_length` (below). The text is the constants in
`gateway/redaction/note.py`.

Nothing is added when redaction is off or every entity is `off` or `block`.
The note is added after redaction, so it is never screened itself, and it is
not part of what the caller sent, so it appears in no transcript; its tokens
are billed as input like any other system text.

### Same-length placeholders, for forms

Some things the model writes into have a fixed width: a PDF form field that
takes 16 characters, a column, a masked input. `<IT_FISCAL_CODE_Y5RD4GZTEK>` is
27 characters and a model asked to fill a 16-character field with it will,
reasonably, refuse. Each entity type and each custom pattern can therefore ask
for `placeholder: same_length` (default `tagged`, today's form), edited on the
rule page under the type's action:

```
RSSMRA80A01F205X   ->   #J45PBO2XY7TXCF#     (16 characters, as the original)
```

The stand-in is `#`, a token, `#`, and its total length equals the original's.
The token is cut from the **same keyed HMAC** as the tagged form (base32,
uppercase), so the same value always gets the same stand-in, different values
differ, and the gateway still holds no state. It applies to `anonymise_restore`
and `anonymise`; `redact` writes the bare label and has no token to shape.

It is **best effort**. The tagged form is used instead when the original is
shorter than 8 characters (too little room for a token that is both clearly
synthetic and unlikely to collide), when another value in the same request would
get the same stand-in (types that compare digits only, such as fiscal codes, can
do this for two values that differ only in letters), or when the stand-in already
appears in the text around the value. Restoring is a literal substitution from
the request's map, as for any placeholder, and a stream holds back one character
less than the longest placeholder issued, so a 40-character stand-in is held
back as 39.

The token is **deliberately not a realistic fake**. A stand-in that looked like a
fiscal code or an IBAN would be mistaken for the real one by the model, by
whoever reads the transcript and by any system downstream that accepts it;
`#J45PBO2XY7TXCF#` cannot be mistaken for anything. When a rule uses same-length
stand-ins the system note describes them too.

The cost is that it tells the provider how long the value is. For that reason a
scope can only veto it, never impose it: when several rules name the same type,
same-length applies only if every one of them asks for it, and moving a rule
from tagged to same-length is recorded as a weakening. Rules saved before this
option existed load as `tagged`; the policy is stored as JSON, so no migration
is involved.

Each type can also carry its own detector **threshold** (`PERSON` at 0.85 and
`URL` at 0.5 are not the same judgement), and the deployment can define
**custom patterns** — operator-written regexes treated as one more entity type,
evaluated in the gateway under **RE2** rather than Python's `re`. The reason is
measured: `re.search(r"(a+)+$", "a"*26 + "!")` takes 10.8 seconds on Python's
engine, which has no timeout; under RE2 the same call is microseconds, because
RE2 cannot backtrack. One pattern of that shape, written by accident, hangs a
worker; RE2 makes the hang structurally impossible. The cost: no backreferences
and no lookaround — a pattern using them is refused at save time with RE2's own
message.

New rules start with no custom patterns. The pattern editor offers
**Add pattern from template** for credential shapes the detector cannot see and
for common structured identifiers; choosing a template adds an editable row.
Credential templates block the request, while identifier templates redact the
matched value.

## Policies, not a deployment policy

There is no deployment-wide policy; there is a table of **policies**
(`redaction_policies`) and pipelines that apply them. What ADR 0038 called a
scoped rule is now a policy with no scope, and the scope moved to the pipeline
that applies it: a pipeline's **who** (all, users, groups, keys) and **target**
(all, only, all except, over providers and models) are what a policy's
`all`/`provider`/`model`/`group`/`user`/`api_key` scope used to be. Redaction
follows where the **text goes** and who **wrote it**, which is why *groups* means
the group the request is billed to.

**The shipped default protects nothing**: no policy applied, no redaction. That is
a visible absence (an empty list on the Pipelines tab) rather than an invisible
presence, and a visible absence is the failure mode that gets fixed. A deployment
that wants protection writes one policy and a pipeline on everything that applies
it. This reversed an earlier default ("protect everything the engine finds except
four types") after that default redacted the wrong things for a language it was
never written against: *"Riassumi le notizie del giorno da ilpost.it"* went
upstream as `<PERSON_…> le notizie del giorno da <URL_…>` — an English model
calling an Italian verb a person at 0.85 and the news site a URL.

An allow-list (values never redacted, compared case-insensitively, **exactly** — a
substring rule would let "it" allow every Italian domain) is taken from the
**first** policy in the fold, which is the widest scope's, never a narrower one's.

### The first rule to hide a span decides how

Rules apply in one order: administrator pipelines in console order, then the user's
own, each pipeline's rules top to bottom. **Block first**: if any matching rule blocks
(or an override blocks a type that was found), the request is refused and that rule is
named. Otherwise one detection pass over the original text finds every span; each
matching rule, in order, hides what its policies find using its mode and overrides,
and **a span a rule has hidden is no longer seen by the rules after it**. `off` and
`detect_only` never claim a span. Thresholds and allow-lists are each policy's own, so
what one policy exempts is exempt in that policy only. Placeholders stay HMAC-stable
(`placeholder_for`), and restore works for `anonymise_restore` spans.

Because administrators come first and the first rule to hide decides how, a user's
rule can add protection and can never loosen what an administrator's did. This
replaces the strictest-mode fold of ADR 0038 (ADR 0114).

Rotating `GATEWAY_REDACTION__PLACEHOLDER_KEY` invalidates embeddings indexed with
old placeholders: knowledge bases must be re-embedded.

### Provenance

Every request records `redaction_scope` and `redaction_rule_id`: the **policy** of
the last administrator rule that applied one (and that pipeline's scope); the log's
per-type `rule` is the privacy rule that claimed the type, since the
tables are mutable and "why was this request redacted" has to be answerable from
the request. A policy keeps its id when it is edited. Resolution costs no extra
queries: every id a scope can name is already loaded on the request, and
pipelines and policies arrive on the state's existing ten-second poll as a swap,
never a redactor rebuild, which would quietly destroy the detection cache's hit
rate (the difference between 13ms and 135ms on a 1,000-token prompt).

## User policies and pipelines

A user can keep **policies of their own** and **pipelines** whose rules apply them (or an
administrator's shared policies). What they write can only **add** protection, because
their rules come after the administrators': the first rule to hide a span decides how, a
block refuses, and an administrator's choice holds. A user's policy is an ordered list of
entries, and an entry only says **what to look for** (the rule says what to do):

- **a custom pattern**, under the same RE2 engine, the same validation and the same
  limits as an administrator's custom pattern (no backreferences or lookaround; a
  pattern that matches the empty string is refused);
- **an entity type** to look for. It must be one the administrator has made available
  to users (Detectors tab; blank means any).

There is no threshold, exception, allow-list or placeholder style to set: the request has
no such fields and an unknown one is refused. A pattern of theirs never displaces what an
administrator's rule already claimed (a pattern such as `ar` cannot cut `Mario Rossi` down
to its own letters): text already hidden is not seen by later rules. Names that collide
with an administrator's pattern or type are refused.

They apply to requests whose principal is that user: a token the user signed in
with, and the user's own API keys. They never reach another user's requests. They
are held in memory by every worker, loaded on the same ten-second poll, so the
request path stays query-free; the worker that handled the edit has it at once.

- Each user keeps at most 25 pipelines, 10 policies and 25 entries per policy.
  They are removed with the account, and a merge keeps the surviving account's and
  drops the merged-away account's.
- If the engine is `noop`, nothing is screened, so a user's policies have nothing
  to act on. The console says so.
- A user policy that blocks makes that user's own requests fail with the usual 403
  when it matches.

The API is `/api/me/privacy` (policies, pipelines, options, in-force, test; cookie
session and the `console` capability, like the rest of `/api/me`). `POST
/api/me/privacy/test` runs the real combination over a text or a file's facts, as
the user, and answers with the pipelines in scope (every rule, matched or not) and which rule claimed which text.
It never echoes the text and never logs it, and it is limited to 30 calls a minute
per user (per worker); the 31st is a 429. `GET /api/me/privacy/in-force` lists the
administrators' pipelines that cover the user, read-only (rule names and what each
does; policies by name), showing only the caller's own matches (not other groups',
users' or people's keys).

## What users can see

**The log.** Whenever redaction hid something, or refused a request, the usage
row for that request carries a log entry (`usage_records.redaction_detail`).
Per entity type it has the count, the mode, the rule that decided (id, name,
owner `admin` or `user`) and where the value was (`message`, `tool_result`, or
`document` for `/v1/ocr`, whose screened text is the extracted document), plus
whether the request was blocked.

**It never holds a value.** Not the original, not the matched text, and no
character offsets. A request where redaction ran and found nothing leaves no
entry, unless a pipeline does routing (a rule, or a last rule that is not the base
level): then every request of a known kind leaves one, with an empty `entities` list and the `routing` block described
below. The entry lives on the usage row, so it costs no extra write and its
retention is the request log's (which keeps rows; only the reply text has a
sweep, see `GATEWAY_TRANSCRIPT_RETENTION_HOURS`). With accounting switched off
there is no request log and so no redaction log.

- `GET /api/me/redaction/events` lists the caller's own entries, newest first,
  in the usual paginated envelope, with a `request_id` filter. A user's
  own-key (BYOK) requests are theirs and are included, flagged `byok`.
- `GET /api/admin/redaction/events` is the administrator's view of everyone's,
  with `user_id` and `request_id` filters. Like every administrator read of the
  ledger it leaves BYOK requests out.
- The console's **Redactions** page, next to Your usage, shows the log; the
  user's own pipelines, policies and test box are on **Privacy pipelines**.

### The per-response summary

A caller that asks gets a summary of what redaction did to *this* request, in
the response. It is opt-in: send `x-pystino-redaction-summary: 1`. Without the
header a response is byte for byte what it was, so an OpenAI client never sees a
field it did not ask for.

On `/v1/chat/completions` the response then carries a top-level
`pystino_redaction` object, in the JSON body when not streaming and on the final
chunk when streaming. The final chunk is the one that carries `usage`, or, when
the client is not shown usage, the chunk that finishes the answer; it is never
a separate event.

```json
{
  "applied": true,
  "request_id": "9f2c...",
  "blocked": false,
  "entities": [
    {"type": "PERSON", "count": 2, "mode": "anonymise_restore",
     "rule": {"id": "6c1e...", "name": "Cloud providers", "owner": "admin"},
     "sources": ["message", "tool_result"]}
  ]
}
```

- The object is **absent** when redaction is off for the request (the engine is
  `noop`, or no policy applies) and no pipeline does routing. `applied` is
  `false` with an empty `entities` when redaction ran and found nothing.
- `rule.id` is `null` for the deployment's own policy, which has no row.
- A blocked request is a 403; with the header its error body also carries the
  object, with `blocked: true`.
- `request_id` is the id the request was logged under. (The gateway otherwise
  never returns the id it uses; this is the one place it does, because the
  caller asked.)
- Chat completions carry it, as do `/v1/ocr` and `/v1/embeddings` (top level of
  the JSON body). `/v1/responses` and `/v1/messages` do not yet.
- `routing` (new, optional) says what Pystino observed and what handled the
  request; see "Routing attribution" below. Clients that ignore unknown fields
  need no change.
- **No cost.** `pystino_cost` is not sent: the cost of a request is settled
  after the response is sent, so it is not known when the object is built. Read
  it from `GET /v1/pystino/usage` once the request is over.

### Routing attribution

Since ADR 0113 the log entry and the summary also carry a `routing` object: what
Pystino itself observed in the request, and what handled it. It is present on
every chat completion, response and message (kind `chat`), every `/v1/embeddings`
call (`embed`) and every `/v1/ocr` call (`read`). It lives in the same place as
the entities: `usage_records.redaction_detail["routing"]` (so no extra column
and no extra write), and `pystino_redaction["routing"]` in the opt-in summary.
The log entry itself is written for every request once a pipeline does routing (a
rule, or a last rule that is not the base level), even when nothing was hidden;
otherwise the entry is written only when something was hidden, as before.

```json
{
  "v": 2,
  "kind": "extract",
  "observed": {
    "target": {"alias": "reader", "model": "local-reader", "provider": "onsite"},
    "filename": "referto-rossi.pdf (summary only)", "filename_match": {"rule_id": "6c1e...", "pattern_index": 0}, "mime": "application/pdf",
    "filetype": "pdf", "is_scan": true,
    "finds": {"Codenames": 2}, "classified": {"Refunds": false}
  },
  "handled": {
    "extractor": "docling", "fallback_reason": "docling busy", "reader": "docling", "engine": "docling",
    "policies": ["Default"],
    "routing_rule": {"id": "6c1e...", "name": "Codename files", "pipeline": "Reports", "pipeline_id": "…", "owner": "admin"}
  },
  "outcome": "rule",
  "pipelines": [
    {"id": "…", "name": "Reports", "owner": "admin", "hop": 0, "rule": {"id": "6c1e...", "name": "Codename files"}, "level": "read_with"},
    {"id": "…", "name": "My reports", "owner": "user", "hop": 0, "rule": null, "level": "default"}
  ],
  "observed_rules": [{"id": "...", "name": "Trial"}],
  "degraded": []
}
```

- `observed.filename` (summary only), `mime`, `filetype`, `is_scan` and `filename_match` are only for `read`;
  `finds` (the number of distinct items each counted policy found, by policy name)
  and `classified` (what each classifier said) only when a rule asked for the text's
  content. Items are never listed: counts only. `is_scan` is what the
  extractor reported, so it is `null` for a document that was never read here.
- **The file name is never stored.** File names often carry personal data, so the
  log keeps the type, the MIME, `is_scan` and `filename_match` (the rule id and
  the index of the pattern that matched, if a `filename` condition did). The base
  name (at most 120 characters, never a path) appears as `observed.filename` only
  in the per-response summary, for that one response. Everything else here is a
  label, a group or model name, or a count.
- `handled.extractor` and `fallback_reason` come from the extractor's own answer.
  `routing_rule`
  is the rule that set the winning level (the administrator's on a tie), with its
  pipeline and `owner`, or `null`; `policies` names the policies applied.
- `pipelines` lists every pipeline that was in scope and the rule that fired in
  each (`rule` is `null` when the pipeline's fixed last rule applied), with the
  level it asked for and `hop` (1 or more for pipelines that ran after a reroute).
- `outcome` is `default` (nothing applied), `rule`, `rerouted`, `refused`, or
  `blocked` (redaction refused it).
- `observed_rules` lists `detect only` rules that matched (they record and hide nothing).
- `degraded` appears when a policy's NER part or a classifier a rule needed was
  unavailable, and says how that was handled (see [Privacy pipelines](privacy-routing.md)).
- The console's **Redactions** page shows these fields compactly in a "Handled
  by" column, and lists requests where nothing was hidden when routing is on.

**The values.** With a second header, `x-pystino-redaction-values: 1`, *and* a
caller that authenticated with the user's own sign-in (an external IdP access token, or the bundled IdP's session token, which is a key minted at login), never a key the user or anyone created, the object
also has `values`: `{"type", "original", "placeholder"}` for every value that
was substituted. They are put in that one response and **never written to a log
or a database**: the usage row, the redaction log and the application log hold
none of them. A standing API key never gets them, because a key may belong to a script
whose output is logged.

## Operating it

- The **Privacy** area of the console holds all of it: **Pipelines** (when and
  how), **Redaction policies** (what is hidden; the editor is its own page),
  **Classifiers** (decision models), **Redaction service** (the redaction engine
  choice, the detection families, what users may add to their own policies), and
  **Test** (one box that runs the real combination for a sample).
- `GET /api/admin/redaction` reports what is in force.
- The detection cache is a **per-process LRU** — adding worker processes
  lowers the hit rate. It is keyed on the text *and* everything the detector
  was asked (language, threshold, entity types, engine options), so a change
  of policy never serves a result computed under another. At scale, a shared cache in Valkey is the answer (see
  [Measured performance](performance.md)).
- Scoping is a **performance lever** as much as a policy one: not
  running NER where no pipeline applies a policy is the largest single optimisation
  available.

## Screening time limit

`GATEWAY_REDACTION__TIMEOUT_SECONDS` (default 30) is how long the gateway waits
for the redaction service to screen a request. Named-entity screening of a large
tool result, such as a LaTeX file from an MCP tool, can take many seconds. When
the limit passes, the request is refused (fail closed) with the code
`redaction_timeout`; when the service cannot be reached at all the code is
`redaction_unreachable`. Both appear in the console's request list as the reason
for the refusal, and the gateway log gives the input size and the engine, never
the text. Raise the limit if large inputs still time out. With
`GATEWAY_REDACTION__FAIL_OPEN` on, either case forwards the request unredacted
instead, and logs it loudly.
