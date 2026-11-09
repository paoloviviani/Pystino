# Privacy pipelines

A **redaction policy** (see [Redaction](redaction.md)) says *what to look for and
what to do about it*. A **pipeline** says *when and how* a request is handled: which reader opens a file,
whether a request is refused, and which policies are applied to it. Each request
is judged on its own, from what Pystino can see in it. It works for any
OpenAI-compatible client, with no Cerea headers, and nothing is remembered between
requests: the outcome is a function of the request, the pipelines and the
configuration.

A pipeline never lowers a redaction mode. It can choose a reader, refuse, or add a
policy; policies only add up and the strictest mode wins.

## The two objects, and how they combine

| | Policy | Pipeline |
|---|---|---|
| Job | what to find, and hide | when and how |
| Scope | none | who and target |
| Rules | items in two groups (patterns, entities), a mode each | ordered conditions → action, plus a fixed "everything else" |

A policy is also what a condition looks things up with: "**\<policy\> finds at
least N items**" (see [Conditions](#conditions)). One editor defines what is looked
for in both uses; a policy that only decides sets its items to `detect only`.

**Inside one pipeline the first rule that matches wins** (observe-only rules are
logged as would-fire and the search goes on). **Across pipelines nothing is
first: every pipeline in scope runs, and the results combine.**

| | How it combines |
|---|---|
| Where it is handled | the **ladder**: the highest level across all pipelines wins |
| What is hidden | the policies each pipeline chose are **added up**; for each entity type the **strictest mode** wins (`off` < `detect_only` < `anonymise_restore` < `anonymise` < `redact` < `block`, the fold of ADR 0038 with `detect_only` added at the bottom) |
| The reader | an **administrator** pipeline's choice holds; a user's applies only if no administrator pipeline chose one; otherwise the default |
| Who is named | the rule that set the winning level; on a tie, the administrator's |
| The trace | every pipeline in scope and the rule that fired in each |

### The ladder

| Kind | Levels, lowest to highest |
|---|---|
| Read | **default reader** (the model the caller named) < **read with** a chosen reader < **refuse** |
| Embed | **pass** < **refuse** |
| Chat | **pass** < **refuse** |

Every non-refuse action may also **redact with** one policy: on the returned text
(read), the input (embed), or messages and tool results (chat). A refused request
is not handled further, so no policy applies.

Policies fold widest scope first (the same order scoped rules always folded in),
which matters for one field: the allow-list is taken from the first policy alone.

## The kinds

The kind comes from the endpoint and is fixed when a pipeline is saved.

| Kind | Endpoints | What a rule can see |
|---|---|---|
| Read | `/v1/ocr` | file name, file type, whether it is a scan: facts about the file, never its text |
| Embed | `/v1/embeddings`, `/v1/rerank` | what a policy finds in each input, what a classifier says |
| Chat | `/v1/chat/completions`, `/v1/responses`, `/v1/messages`, and the other chat surfaces | what a policy finds in the messages, what a classifier says |

Who is asking and where the request goes are not conditions: they are the
pipeline's **scope**.

## Scope

- **Who**: all, users, groups or keys. *Groups* means the **billing group**: the
  group the request is billed to (as redaction scopes always did), so a user in
  several groups is matched by the one this request is charged to, resolved the
  same way for requests, the Test box and the user's "set by your administrator" list. A user's own pipeline is fixed to
  that user.
- **Target**: all, only, or all except, over providers and models. For a read the
  target is the OCR model or provider named in the request.

A pipeline runs only on requests inside its scope. A facility vLLM, your own TEI
or a reader deployed in your facility is left out of the scope of the pipelines
that guard remote providers: that is how an administrator treats one differently.

## Conditions

All conditions of a rule must hold (AND). A condition may be negated ("is not").
A rule with no conditions matches every request of its pipeline's kind. A key
appears at most once per rule, except `finds` and `classified`, which may name
several different policies or classifiers.

| Condition | Kind | Meaning |
|---|---|---|
| `filetype` | read | `pdf`, `word`, `excel`, `powerpoint`, `image`, `text` |
| `scan` | read | yes or no: the file is a scan |
| `filename` | read | globs: `*`, `?`, `[abc]`, case-insensitive, matched on the base name |
| `finds` | embed, chat | "**\<policy\> finds at least N items**" (N from 1 to 1000; "is" or "is not") |
| `classified` | embed, chat | "**classified as \<classifier\>**" ("is" or "is not"); administrators only |

There is no built-in topic, no quick check and no language condition: nothing in
Pystino is about health or any other subject. What is looked for is written by an
administrator, in a policy.

A reading rule decides from the file, not its content; content is checked when the
text is embedded or sent to chat. The API refuses `finds` and `classified` on a reading
rule with a 400 saying so, and the console offers only the three facts there.

### Finds at least N items

The condition runs the policy's own items over the request's text and counts the
**distinct** items found, in any mode but `off` (a `detect only` item counts and
is never hidden). Two mentions of one name are one item. It judges the text of
**every input separately and in full**: embedding judges each input, chat the
messages and tool results. A text over 2,000,000 characters (a request over 4,000,000
in all) cannot be checked in full.

The two groups of a policy behave differently when something is down:

- **Patterns** (your regular expressions and keyword lists) are evaluated in the
  gateway and always answer.
- **Built-in formats and entities** are the redaction service's. If the service, or
  the NER engine the policy chose, cannot answer, the count may be short and the
  condition is **undecidable**.

The test trace lists each item that made a condition true: its group, its type,
and for entities the engine and the score. In an administrator's Test tab the text
found is shown too (it is the text they typed); a user's box leaves it out.

### Classified as a classifier

A **classifier** is a decision model: a chat model and a
topic written in plain words. It decides on meaning, not on words, reading the text
in windows of 8,000 characters (any window positive makes the text positive).
**By default a classifier may use only a model that runs inside the deployment**
(the built-in provider, the same notion of "local" the readers use), so the text
never leaves it. The deployment setting **Allow remote classifiers** (Privacy,
Classifiers tab; off by default) lets an administrator choose a model of an outside
provider too; classifying then sends the text to that provider, and the console
says so where the model is chosen. Turning the setting off keeps a classifier that
already uses a remote model but it cannot answer: the condition is undecidable
(never forwarded silently) and the pipeline check and the listing flag it as
`unavailable_now`. Creating or changing a classifier to a remote model while the
setting is off is refused with 400 `remote_classifier_not_allowed`. It ships as scaffolding for large deployments: nothing is
bundled, no topic is seeded, and a classifier with no model, an inactive model or an
unreadable answer is unavailable. A text can try to talk the model into "no"; do not
rest a rule that must not fail on a classifier alone. Only administrators use
`classified`, because it sends text to a provider.

### An undecidable condition never fails open

An engine that cannot serve a type the policy asks for counts as unable to look: when
the service answers that a requested entity type or custom label is in its
`unsupported_types` (an engine with no NER model, or a default switched from spaCy to
GLiNER where a type such as NRP does not exist), a "finds" condition is undecided, not
"found nothing". A policy that lists entity items against an engine that reports no
entities is refused when saved.

When a policy's NER part or a classifier cannot answer, or the input is too large to
check in full, the condition counts as a match only on a rule that protects (a level
above the base, or a policy) and whose condition is not negated: then the rule acts.
A negated condition ("finds no names, so pass") or a rule that adds nothing could
let the text it exists to catch through, so the request is refused with a 503
(`privacy_undecidable`) naming the rule and its pipeline. An observe-only rule
simply does not match. A match is assumed only when the rule's action is at least as strict as every later
enabled rule of the pipeline (and its last rule): otherwise it would fire in place of a
stricter one it was shadowing (for example "finds at least 3 -> read with the local
reader" ahead of "pdf -> refuse"), so the request gets the 503 instead. Items the
patterns already found are certain: "at least 2"
is true with two keyword hits whatever the NER part would add. `fail_open` does not
apply: it is about redaction, and a condition must never read an outage as "found
nothing".

Separately, **redaction** with a policy whose NER engine the service no longer
reports (409 `engine_unavailable`) is refused with a 503 naming the policy
(`redaction_engine_unavailable`): a request is never screened by an engine the
administrator did not choose, nor let through unscreened.

### The other conditions

- **A scan** is found while reading. A pipeline that depends on it (or on the text
  of a file) makes the gateway read the file first on the **built-in extractor**
  (the file never leaves the deployment for that), then judge again. If no built-in
  reader is available to the caller, or the document is a link, the request is
  refused (fail closed), naming the rule that was waiting.
- **Unknown name or type fails closed.** A link's name and extension are taken
  from its path. When a file still has no name or no known type, a rule that
  protects (refuses, reads elsewhere, redacts) treats its `filename` and
  `filetype` conditions as **matching** (the editor warns), and the log says so
  under `degraded`.
- **File name and type are a convenience, not protection.** A renamed file is not
  caught. The console says so on a protective reading rule that depends only on
  them. Protect content with embed and chat pipelines that use `finds`.

The content conditions run only if an enabled pipeline of that kind has a rule that
uses them, only for pipelines in scope, and only after cheaper conditions (file type
and name, scan) have held.

## Reading

| Level | Effect |
|---|---|
| default reader | the model the caller named reads the file, possibly a remote one |
| **read with** a reader | any configured `/v1/ocr` model: one of the **built-in extractor's models** (`markitdown`, `markitdown-ocr`, `docling`, below), or another OCR provider such as a facility reader. It **overrides the model the caller named**. |
| refuse | 403 with `routing_refused`, naming the rule and its pipeline. Never the content or the file's name |

- **The built-in extractor's engines are models.** Each engine the deployment's
  extractor can run is an OCR model of its own under the built-in provider:
  `markitdown` (the plain reader), `markitdown-ocr` (markitdown plus RapidOCR, for scans and
  pictures) and `docling`. The reader picker lists them by name beside any other OCR
  model, and a rule that names none reads with `markitdown`. There is no separate
  engine choice: the engine is the model, so one rule picks the reader and the engine
  together, and the "administrator's reader wins over a user's" rule covers both.
  Reading with `markitdown-ocr` or `docling` is quality, not privacy: the file stays
  in the deployment either way. See [extractor engines](extractor-engines.md).
- **It fails closed when it cannot be done**: the document is a link and the
  reader is one of the built-in extractor's models (which read only documents sent
  with the request, never a URL: that would be a server-side request forgery), or the
  reader is not available to the caller (not granted, switched off, or an extractor
  model whose engine the running extractor does not offer).
- **After a reroute, the pipelines whose target scope covers the new reader also
  run**, so a reroute can never escape a pipeline that guards that reader. The
  loop is bounded (three hops). A pipeline that guards the reader the request was
  re-pointed at decides the next hop (an administrator's choice still holds over
  a user's); if it cannot be honoured, or the hops run out with the pipelines still
  asking for another reader, the request is refused (fail closed).
- **Whole files leave whole.** A file sent to a remote reader leaves as it is.
  Redaction only screens the text that comes back.

## Embedding and chat

Only two levels: pass and refuse. **There is no reroute for embed or chat.** An
embedding sent to another model breaks the vector space, and choosing the provider
is the caller's job. The refusal is a 403 naming the rule; an applied policy hides
what it names in the input (embed) or in messages and tool results (chat).

## Evaluation

1. The kind comes from the endpoint.
2. Every enabled pipeline of that kind whose scope covers the request runs; in each
   the rules are taken in order, disabled rules are skipped, cheap conditions
   (file type, name and scan) come before the text is judged, and the first
   matching non-observe rule decides (or the fixed last rule when none does).
3. The answers combine as above.
4. A refusal is a 403; otherwise the folded policy redacts, and the request goes on.

Pipelines, policies, classifiers and the names of readers are held in memory on each
worker and reloaded every ten seconds (and at once on the worker that served a
change), so the request path adds no database query.

A worker that has **never** read the pipelines answers the kinds they govern with
a 503 (`privacy_rules_unavailable`) rather than run with none; `/readyz` reports
`privacy_routing` as an error and `/healthz` mentions it. A row that cannot be
read is skipped with an error log and the rest load; `GET
/api/admin/privacy/status` lists them. File name patterns are matched by a linear
matcher, not a regular expression, so a hostile pattern cannot stall a worker.

## User pipelines

A user may create pipelines, for themselves alone. Kind and target are free; the
who-scope is fixed to the user (the server sets it whatever is sent, and the
request-time scope check holds it again).

- **Their actions only tighten**: read with a reader, refuse, redact with a
  policy. There is no "default reader" or "pass" action for a
  rule (that is what happens anyway), though "everything else" may add nothing.
- **Policies**: an administrator's that is **marked shared with users**, or their
  own, both to apply and in "finds at least N items". A user pipeline's policies are
  folded **after** every administrator pipeline's, as tighten-only additions (modes
  and patterns; never the allow-list, thresholds or placeholder styles), and never
  replace the environment's policy: picking an administrator's policy can only add. A user's own policy is an ordered
  list of custom patterns (the administrators' own RE2 validation) and stricter
  modes for entity types the administrator has made available; no `off`, no
  threshold, no allow-list. It is folded after the administrators' answer and can
  only tighten. Never another user's policy.
- **Readers**: only ones they are granted; the request path resolves it again and
  fails closed.
- Because the combination is monotone, a user's pipeline can never loosen
  anything: a value an administrator's policy hides stays hidden whatever the
  user's policies say. A stored row that loosens is skipped when read.
- At most 25 pipelines and 10 policies per user. They are removed with the
  account; a merge keeps the surviving account's and drops the merged-away
  account's; an administrator pipeline that names the merged-away user among its
  users is re-pointed at the survivor.

## Saving a pipeline

Errors block saving (HTTP 422, naming the field): a kind that does not exist, a
condition the kind does not have, a value of the wrong shape (a bad glob, an
unknown file type), a level the kind does not have, a missing name, a policy,
classifier, reader, user, group, key, provider or model that does not exist (or, for
a user, a policy that is not shared), a count outside 1 to 1000, two rules with one
name. Names are unique within a kind (per owner).

Warnings never block; they come back with every pipeline and from `POST
/api/admin/privacy/pipelines/check` (a draft):

| Code | Meaning |
|---|---|
| `never_fires` | An earlier enabled rule of the pipeline catches everything this one does |
| `partly_shadowed` | An earlier rule catches some of what this one does |
| `unavailable_now` | A policy's NER engine or a classifier it uses cannot answer now (or no longer exists) |
| `migrated` | A rule the migration switched off: its note says why, and the rule keeps it until it is turned on |
| `convenience_only` | A protective reading rule that depends only on name or type |
| `catches_all` | The rule has no conditions |
| `soft_cap` | More than 20 rules in one pipeline |

Shadowing is a hint, not a proof. Observe-only and disabled rules never shadow.

## The administrator API

Under `/api/admin/privacy` (administrators only, audited as `privacy_pipeline.*`,
`redaction_policy.*`, `privacy_classifier.*` and `privacy_settings.*`):

| Route | Purpose |
|---|---|
| `GET /pipelines`, `POST /pipelines`, `GET/PATCH/DELETE /pipelines/{id}` | pipelines (`kind`, `name`, `enabled`, `who`, `target`, `rules`, `otherwise`); the kind is fixed |
| `POST /pipelines/reorder`, `POST /pipelines/check` | the order within a kind; warnings for a draft |
| `GET/POST /policies`, `PATCH/DELETE /policies/{id}` | redaction policies, with `shared_with_users`; one a pipeline applies or counts items of cannot be deleted. An engine is accepted only if the service reports it available |
| `GET /engines` | what the redaction service offers: pattern formats and the NER engines (available, default, entity types, custom labels, speed) |
| `POST /policies/try` | a policy as edited and a text: every item found with its group, engine and score, and what redaction would do. No side effects; rate limited with the test box |
| `GET/POST /classifiers`, `PATCH/DELETE /classifiers/{id}` | decision models (a chat model and a topic); the listing also names the chat models that can be chosen (local ones, plus remote ones when allowed) and `allow_remote`; a remote model while that is off is 400 `remote_classifier_not_allowed` |
| `GET/PUT /settings` | the entity types users may name in their own policies, and `allow_remote_classifiers` (omit a field to leave it as it is) |
| `GET /options` | what the editors may pick: users, groups, keys, providers, models, readers, policies, classifiers |
| `GET /status` | whether this worker loaded the pipelines, and unreadable rows |
| `POST /test` | the real combination over given facts, with no side effects; the text and file are never stored or logged; 30 runs a minute |

A user's are under `/api/me/privacy` (`pipelines`, `policies`, `in-force`,
`options`, `test`, cookie session): the same shapes, enforced as above.

## The NER engine of a policy

A policy chooses its NER engine, **spaCy** or **GLiNER**, among the engines the
redaction service reports available; no choice means the deployment's default. The
choice is saved only if the service says the engine is available (and, for custom
entity types, that it takes labels). When several policies of different engines
apply to one request, the redaction service is called **once per engine** and the
spans are merged: one span per identical range and type, and the strictest mode of
the folded policy decides. Built-in formats go with **every** call, because which
engine finds one can depend on the model (DATE_TIME is spaCy's; GLiNER maps it from its
own label). Keyword lists and regular expressions do not depend on the engine and
always run. At most 30 custom entity types (each label at most 80 characters) can be
asked of one engine at once: a policy is refused when saved if, with the other active
policies of its engine, the merged count could pass 30, and a request that still does
gets a named 503, never a 500. A custom label may not equal one the engine already uses
(case-folded): the gateway refuses it when saving and the service answers 422
`label_taken`. A GLiNER queue that stays full past `REDACTION_GLINER_QUEUE_SECONDS`
(30) answers 503 `engine_busy`, which the gateway treats as the engine being
unavailable. See [Redaction](redaction.md#items-two-groups-never-mixed).

## Migration

Migration `0082` rewrites what an existing deployment had and handles requests
exactly as before; a contract test proves it byte for byte (the redacted text on
chat, responses, embeddings, rerank and both readers, and the refusals) for
representative configurations, and that a deployment with no rules behaves
exactly as before.

- Every **scoped redaction rule** becomes a **policy** (same id and name) plus one
  administrator **pipeline per kind** with the rule's scope and a last rule that
  applies that policy. A rule on everything becomes pipelines scoped to all;
  provider and model scopes become a target; group, user and key scopes a who.
  Rules fold in the same order as before, so strictest-mode-wins, unioned
  patterns and the allow-list from the first policy come out identical.
- A user's **own rules** (ADR 0112) become one user policy ("My rules", entries in
  the order written) and three user pipelines.
- **Routing rules** from the first build of this feature become rules inside
  per-kind administrator pipelines ("Routing rules (read)"), scoped to all; a rule
  that named users, groups or targets gets a pipeline with that scope. "This or
  that" conditions (a file type or a scan; health data or IDs) are split into
  adjacent rules, which keeps first-match meaning. A `local` action becomes
  `refuse` (it failed closed without a local model).
- `providers.is_local` is dropped.
- **Migration `0083`** (one detection vocabulary): see below. It is reversible: the
  downgrade merges `formats` back into `entities`, writes `detect_only` as `off`, turns a
  "finds at least 1 item" of the migrated identifiers policy back into `ids`, and
  refuses (unless `PYSTINO_ALLOW_LOSSY_DOWNGRADE=1`) what the older shape cannot say.
- A routing rule that lost a condition (a `local`/`cloud` target, a mixed who) goes in a pipeline of its own so it can only add; a verdict condition with no values always holds.
- **Downgrading** writes a policy plus its three plain pipelines back as the scoped rule, and a user's policy back as their rules. If anything cannot be said in the older model (pipelines with rules, an unusual scope, two policies on one scope, a limit on users' entity types) the downgrade is **refused** with the list; `PYSTINO_ALLOW_LOSSY_DOWNGRADE=1` drops it anyway.

A deployment migrated from redaction rules logs as it always did: only a request
where something was hidden leaves an entry, until a pipeline does real routing
(a rule, or a last rule that is not the base level).

### Migration 0083: one detection vocabulary

Policies became the only object that finds items, and the quick check went. Pipelines
exist only on unreleased builds so far, but nothing is dropped silently:

- Every policy is rewritten in the two-group shape (`formats` for built-in pattern
  formats, `entities` for NER types, plus empty `keywords` and `custom_entities`)
  with the **engine null**, the deployment default: what every policy did before,
  byte for byte (the contract test, extended for the engine field and for keyword
  lists that stand in for the regular expressions they replace).
- Detector profiles are gone (their table and the `profile_id` columns); the engine
  choice replaces them.
- A condition on **IDs** becomes "finds at least 1 item" of a policy named
  **Identifiers (migrated)**, which lists the formats the quick check knew
  (fiscal code, VAT number, IBAN, card, US SSN, NHS number) as `detect only`.
- A **reading** rule carrying `finds`, `classified` (or `ids`) is kept switched off with
  the condition removed and a note, and named in the log: reading decides from the
  file, not its text.
- The `ids` conversion now depends on the redaction service (the quick check ran inside
  the gateway and always answered); US_SSN and UK_NHS are English-only and low-scoring.
  The migration log says so.
- A pipeline whose policy **cannot be loaded** (stored rows over the current limits: a
  label over 80 characters, more than 30 custom entities, or a policy switched off by
  hand) **refuses the request** with a named 503 (`privacy_policy_unavailable`); it never
  runs without the policy. Fix or replace the policy. Custom entity types are also
  unique across the active policies of one engine (one name per description and one
  description per name, at most 30 in all), checked when a policy is saved or turned on.
- A condition on **health data** or **language** has no equivalent. The rule is
  **kept, switched off, with the condition removed and a note** the console shows as
  a warning. A protective rule can then only apply to more requests when turned on;
  a rule that only passed requests is also made observe-only, so turning it on cannot
  loosen anything.
- Administrator policies that users' pipelines already apply are marked shared with
  users, so nothing stops working.
- Every conversion is in the migration log (`privacy migration 0083: ...`), naming
  the pipeline and the rule.

### Migration 0085: the extractor's engines are models

Rules no longer say "the built-in extractor" plus an engine; they name a model.
`builtin` with engine `markitdown` becomes `markitdown`, `ocr` becomes `markitdown-ocr`,
`docling` becomes `docling`, and `builtin` with no engine becomes `markitdown`. A rule is
only pointed at a model on the extractor provider: if a model of that name exists
elsewhere (a remote service, an alias) the rule reads with `markitdown` instead and the
log says so loudly, so a local-only rule never leaves the stack. Every changed rule is
in the migration log (`extractor engine models 0085: ...`). The downgrade turns the
extractor's model names back into `builtin` and an engine. The API's `engine` field on a rule's
action and the options' `engines` list are gone (a request that sends `engine` is
refused with 400).

## What it does not do yet

Classifiers are scaffolding (configuration, the call path, fail-closed handling; no
model is bundled), and optional enrichment by clients (Cerea's descriptions of
retrieved text) is a later phase of ADR 0113. A user cannot choose a NER engine for
their own policy; they use the deployment's default.
