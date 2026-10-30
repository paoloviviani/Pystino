# Redaction

Redaction is the layer between the caller's prompt and the upstream provider:
detected entities are replaced with deterministic placeholders before any text
leaves the building, and restored (or not) in the answer the person reads. With a rule in force, what it matches never reaches the upstream. The shipped default has no rules, so nothing is redacted until an administrator writes one.

It is also **~90% of the CPU**. Detection is named-entity recognition and
scales with prompt length — roughly 0.1ms of CPU per prompt token, against a
gateway cost that stays flat at 24–31ms. Capacity planning is redaction
planning; see [Measured performance](performance.md).

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

## Deploying less of it

Three shapes, and all three work:

| You want | How | What you get |
|---|---|---|
| **No redaction, no sidecar** | Leave `redaction` out of `COMPOSE_PROFILES` and the engine at `noop` | Nothing runs, and `noop` needs nothing. The same profile also runs `extractor`, the local document reader behind `/v1/ocr`, so it goes too |
| **Pattern matching only, no NER** | The `redaction` profile as shipped: the published `pystino-redaction:<version>-pattern` image (built with `SPACY_MODELS=` empty) and `REDACTION_NLP_ENGINE=disabled`, the `.env.example` default; engine `http` | Presidio's pattern recognisers — cards, IBANs, emails, phone numbers, the Italian identifiers — at about 150MB and without the CPU cost that scales with prompt length |
| **Everything** | The `redaction` profile with the NER image built locally (`SPACY_MODELS=en_core_web_lg`, the Dockerfile's default; never published) named in `REDACTION_IMAGE`, and `REDACTION_NLP_ENGINE=spacy` | NER for the configured languages on top of the patterns |

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

## What happens to a detected entity: the policy model

For each entity type, a policy names a **mode** — two independent questions, what
the model sees and what the person gets back:

| Mode | Upstream sees | The person gets back |
|---|---|---|
| `off` | the real value | the real value |
| `anonymise_restore` | `<PERSON_xxxx>`, stable | the real value, restored |
| `anonymise` | `<PERSON_xxxx>`, stable | the placeholder |
| `redact` | `<PERSON>` — lossy, two people collapse into one label | the label |
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

Two settings, both read-only on the Redaction screen because they come from the
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
Redaction screen under the type's action:

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

## Rules, not a deployment policy

There is no deployment-wide policy any more; there is a rules table
(`redaction_rules`), and the **catch-all is one rule among them** — scope
`all`, exactly one row may hold it. A rule can scope to `provider`, `model`,
`group`, `user` or `api_key` —
notably different from quota scopes, because quotas follow who *pays* while
redaction follows where the **text goes** and who **wrote it**.

**The shipped default protects nothing**: no rules, no redaction. That is a
visible absence — an empty rules list on the Redaction screen — rather than an
invisible presence, and a visible absence is the failure mode that gets fixed.
A deployment that wants protection writes one catch-all rule and the screen
says so. This reversed an earlier default ("protect everything the engine finds
except four types") after that default redacted the wrong things for a
language it was never written against: *"Riassumi le notizie del giorno da
ilpost.it"* went upstream as `<PERSON_…> le notizie del giorno da <URL_…>` —
an English model calling an Italian verb a person at 0.85 and the news site a
URL.

An allow-list (values never redacted, compared case-insensitively, **exactly**
— a substring rule would let "it" allow every Italian domain) belongs to the
deployment alone, never to a narrower scope.

### Adding a scope can only tighten

Applicable policies combine as: `max` over the mode ranking, `min` over
thresholds (lower catches more), union over custom patterns (one more regex
only finds more), allow-list from the deployment alone.

So **adding a scope can only tighten, by construction** — a rule saved wrong is
inert rather than dangerous, which is the difference between a bug and an
incident. Nothing needs a priority column or a "most specific wins" rule. This
is the quota model's fold inverted: quotas are *all rules must pass* because a
permissive rule would raise a ceiling; redaction is *the strictest answer wins*
because a permissive rule would remove protection.

One subtlety cost a test to find: there is **one row per subject**, so an
administrator's `scope=user` rule and that person's own policy are the *same
row*. "Their own" is decided by who wrote it, not by scope — otherwise the one
rule a person could overrule is precisely the rule about them. The invariant is
a courtesy, not the enforcement: the fold above already makes a weaker rule
inert. It exists so the refusal names the entity type that failed, because
"accepted and silently ignored" reads as the feature not working.

### Provenance

Every request records `redaction_scope` and `redaction_rule_id` — the
narrowest rule that applied, since the rules table itself is mutable and
"why was this request redacted" has to be answerable from the request.
Resolution costs no extra queries: every subject a rule can name is already
loaded on the request, and the rules arrive on the resolver's existing
ten-second poll as a dictionary swap — never a redactor rebuild, which would
quietly destroy the detection cache's hit rate (the difference between 13ms
and 135ms on a 1,000-token prompt).

## Operating it

- The **Redaction screen** in the console shows the engine, the rules and a
  preview; the per-rule editor is its own page.
- `GET /api/admin/redaction` reports what is in force.
- The detection cache is a **per-process LRU** — adding worker processes
  lowers the hit rate. At scale, a shared cache in Valkey is the answer (see
  [Measured performance](performance.md)).
- Scoping redaction is a **performance lever** as much as a policy one: not
  running NER where no rule requires it is the largest single optimisation
  available.
