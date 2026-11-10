# Privacy pipelines

A **redaction policy** (see [Redaction](redaction.md)) says *what to look for*. A
**pipeline** says *when and how* a request is handled: which extractor opens a file,
whether a request is blocked, and what is hidden in it. Each request is judged on its
own, from what Pystino can see in it. It works for any OpenAI-compatible client, with no
Cerea headers, and nothing is remembered between requests: the outcome is a function of
the request, the pipelines and the configuration.

## The two objects

| | Policy | Pipeline |
|---|---|---|
| Job | what to look for: patterns, keywords, formats, entities | when and how |
| Settings | engine, thresholds, allow-list, placeholder style; **no mode** | scope (who, target, model type) and ordered rules |
| Scope | none | who and target |

A pipeline is a scope and an ordered list of rules. There is no "otherwise".

### A rule is a sentence

> **When** ‹policies› + ‹conditions› **→** ‹mode›

- **Policies** (one or more) are what the rule looks for, and what it hides: an
  administrator's policy, or for a user pipeline the administrators' shared policies and
  the user's own. A content rule needs at least one.
- **Conditions** are optional; `match` says whether **all** (default) or **any** of them
  must hold, and each can be negated. With none, a content rule fires when its policies
  find at least one item.
- **Mode**: `off` < `detect only` < `anonymise and restore` < `anonymise` < `redact` <
  `block`, with optional **overrides by entity type** over the rule's policies.
  - `block` refuses the request (HTTP 403, `routing_refused`, naming the rule and its
    pipeline, never the content).
  - `off` does nothing; `detect only` finds, counts and records in the log, and never
    hides.

Rules apply top to bottom. A rule that blocks refuses the request; otherwise every
matching rule hides what it finds, and the first rule to hide a piece of text decides how.

## The kinds

The kind comes from the endpoint and is fixed when a pipeline is saved.

| Kind | Endpoints | What a rule can see |
|---|---|---|
| Extract | `/v1/ocr` | file name, file type, whether it is a scan: facts about the file, never its text |
| Content | `/v1/chat/completions`, `/v1/responses`, `/v1/messages` and the other chat surfaces, `/v1/embeddings`, `/v1/rerank` | what the policies find in the messages (chat) or in each input (embedding, rerank); what a classifier says |

A content pipeline's target has an optional **model type**: any (the default), chat,
embedding or rerank (a set is allowed: "embedding + rerank"). It is part of the scope, so
a pipeline for remote embedders does not run on chat. What a condition judges is unchanged:
chat judges messages and tool results, embedding judges each input.

## Scope

- **Who**: all, users, groups or keys. *Groups* means the **billing group** the request
  is billed to. A user's own pipeline is fixed to that user.
- **Target**: all, only, or all except, over providers and models, and for content a
  **model type**. For an extract pipeline the target is the OCR model or provider named in
  the request.

A pipeline runs only on requests inside its scope. A facility vLLM, your own TEI or an
extractor deployed in your facility is left out of the scope of the pipelines that guard
remote providers: that is how an administrator treats one differently.

## Conditions

| Condition | Kind | Meaning |
|---|---|---|
| `filetype` | extract | `pdf`, `word`, `excel`, `powerpoint`, `image`, `text` |
| `scan` | extract | yes or no: the file is a scan |
| `filename` | extract | globs: `*`, `?`, `[abc]`, case-insensitive, matched on the base name |
| `finds` | content | "**finds at least N items**" (N from 1 to 1000; "is" or "is not") |
| `classified` | content | "**classified as ‹classifier›**"; administrators only |

There is no built-in topic and no language condition: what is looked for is written by an
administrator, in a policy. An extract rule decides from the file, not its content; the API
refuses `finds` and `classified` on an extract rule with a 400.

### Finds at least N items

`finds` no longer names a policy: it counts the **distinct** items found by the rule's own
policies **combined** (two policies finding one name are one item). It judges every input
separately and in full: embedding judges each input, chat the messages and tool results. A
text over 2,000,000 characters (a request over 4,000,000 in all) cannot be checked in full.
A `detect only` item counts and is never hidden.

- **Patterns** (regular expressions and keyword lists) are evaluated in the gateway and
  always answer.
- **Built-in formats and entities** are the redaction service's. If the service, or the NER
  engine a policy chose, cannot answer, the count may be short and the condition is
  **undecidable**.

The Test trace lists each item that made a condition true: its policy, group, type, and for
entities the engine and the score. In an administrator's Test tab the text found is shown too;
a user's box leaves it out.

### Classified as a classifier

A **classifier** is a decision model: a chat model of a configured provider and a topic
written in plain words. It decides on meaning, reading the text in windows of 8,000
characters. **Classifying sends the text to that model's provider.** Nothing is bundled and no
topic is seeded; a classifier with no model, an inactive model or an unreadable answer is
unavailable. A text can try to talk the model into "no": do not rest a rule that must not
fail on a classifier alone. Only administrators use `classified`.

### An undecidable condition never fails open

When a policy's NER part or a classifier cannot answer, or the input is too large to check in
full, a rule that does **not** protect (`off`, `detect only`) simply does not match. A rule that
protects treats a non-negated condition as a match (it acts). A **negated** condition on a
protecting rule ("finds fewer than 3") cannot be assumed either way, so the request is
refused with a 503 (`privacy_undecidable`) naming the rule and its pipeline. Items the patterns
already found are certain: "at least 2" is true with two keyword hits whatever the NER part
would add. `fail_open` does not apply: a condition must never read an outage as "found
nothing".

Redaction with a policy whose NER engine the service no longer reports (409
`engine_unavailable`) is refused with a 503 naming the policy
(`redaction_engine_unavailable`): a request is never screened by an engine the administrator
did not choose, nor let through unscreened.

### The other conditions

- **A scan** is found while reading. A pipeline that depends on it makes the gateway read the
  file first on the **built-in extractor** (the file never leaves the deployment for that),
  then judge again. If no built-in extractor is available to the caller, or the document is a
  link, the request is blocked (fail closed), naming the rule that was waiting.
- **Unknown name or type fails closed.** When a file has no name or no known type, a rule that
  protects treats its `filename` and `filetype` conditions as **matching**.
- **File name and type are a convenience, not protection.** A renamed file is not caught.
  Protect content with content pipelines that use `finds`.

## Order and evaluation

1. The kind comes from the endpoint (content for chat, embeddings and rerank; extract for
   `/v1/ocr`).
2. Collect every enabled pipeline of that kind whose scope covers the request, in this order:
   **administrator pipelines in console order (`position`), then the user's own**. Rules within
   a pipeline go top to bottom. **Every rule is evaluated; there is no first match.**
3. **All conditions are judged on the original text.**
4. **Block first.** If any matching rule blocks (a content rule in mode `block`, an extract rule
   whose action is block, or an override that blocks a type that was found), the request is
   refused, naming the first such rule.
5. Otherwise hiding is applied **in order**: each matching rule hides what its policies find,
   using its mode and overrides. **The first rule to hide a span decides how**; later rules only
   see text not already claimed. `off` and `detect only` never claim a span. This is one
   detection pass over the original text per request, with overlaps resolved by rule order
   (nothing is detected again on placeholder text). Placeholders stay HMAC-stable and restore
   works for `anonymise and restore` spans.
6. The **trace** (the Test tab, and the log's `routing` block) lists every pipeline and every
   rule with matched or not, the items found, and which rule claimed which span.

Pipelines, policies, classifiers and the names of extractors are held in memory on each worker
and reloaded every ten seconds (and at once on the worker that served a change), so the request
path adds no database query. A worker that has **never** read the pipelines answers with a 503
(`privacy_rules_unavailable`) rather than run with none; `/readyz` reports `privacy_routing` as
an error. A row that cannot be read is skipped with an error log and the rest load; `GET
/api/admin/privacy/status` lists them.

## Extracting

An extract rule has an **action** instead of a mode, lowest to highest:

| Action | Effect |
|---|---|
| default extractor | the model the caller named extracts the file, possibly a remote one |
| **extract with** an extractor | any configured `/v1/ocr` model: one of the **built-in extractor's models** (`markitdown`, `markitdown-ocr`, `docling`) or another OCR provider. It **overrides the model the caller named**. |
| block | 403 with `routing_refused`, naming the rule and its pipeline. Never the content or the file's name |

- Between two "extract with" rules the **first in the order above wins**: an administrator's
  before a user's.
- An extract rule may also **hide** in the text the extractor returns: it has policies, a mode
  and overrides in the same shape, applied in the same order, and optional.
- **It fails closed when it cannot be done**: the document is a link and the extractor is one of
  the built-in extractor's models (which read only documents sent with the request), or the
  extractor is not available to the caller.
- **After a change of extractor, the pipelines whose target scope covers the new one also run**,
  so a reroute can never escape a pipeline that guards that extractor. The loop is bounded
  (three hops).
- **Whole files leave whole.** A file sent to a remote extractor leaves as it is. Redaction only
  screens the text that comes back. See [extractor engines](extractor-engines.md).

## User pipelines

A user may create pipelines, for themselves alone. Kind and target are free; the who-scope is
fixed to the user.

- Their rules come after the administrators', so they can add protection (hide more, block, pick
  an extractor only when no administrator did) and never remove any: a value an administrator's
  rule hides is claimed first, a block refuses, and an administrator's extractor holds.
- **Policies**: an administrator's that is **marked shared with users**, or their own. A user's own
  policy is an ordered list of custom patterns (the administrators' RE2 validation) and entity
  types to look for (those the administrator has made available). Never another user's policy.
- **Extractors**: only ones they are granted; the request path resolves it again and fails closed.
- At most 25 pipelines and 10 policies per user. They are removed with the account; a merge keeps
  the surviving account's and drops the merged-away account's.

## Saving a pipeline

Errors block saving (HTTP 422, naming the field): a kind that does not exist, a rule with no
policy, a condition the kind does not have, a value of the wrong shape, a mode that does not
exist, an action the kind does not have, a policy, classifier, extractor, user, group, key,
provider or model that does not exist (or, for a user, a policy that is not shared), a count
outside 1 to 1000, two rules with one name. A body with a removed field (`otherwise`,
`observe_only`, `redact_with`, a policy's `mode`) is a 400. Names are unique within a kind
(per owner).

Warnings never block; they come back with every pipeline and from `POST
/api/admin/privacy/pipelines/check`:

| Code | Meaning |
|---|---|
| `unavailable_now` | A policy's NER engine, a classifier or an extractor cannot answer now (or no longer exists) |
| `migrated` | A rule the migration switched off: its note says why |
| `convenience_only` | A protective extract rule that depends only on name or type |
| `unnamed_matches` | A rule on file name or type that treats an unnamed file as matching |
| `soft_cap` | More than 20 rules in one pipeline |

## The administrator API

Under `/api/admin/privacy` (administrators only, audited as `privacy_pipeline.*`,
`redaction_policy.*`, `privacy_classifier.*` and `privacy_settings.*`):

| Route | Purpose |
|---|---|
| `GET /pipelines`, `POST /pipelines`, `GET/PATCH/DELETE /pipelines/{id}` | pipelines (`kind`, `name`, `enabled`, `who`, `target` with `model_types`, `rules`); the kind is fixed |
| `POST /pipelines/reorder`, `POST /pipelines/check` | the order within a kind (the order the rules apply); warnings for a draft |
| `GET/POST /policies`, `PATCH/DELETE /policies/{id}` | policies, with `shared_with_users`; one a pipeline names cannot be deleted. An engine is accepted only if the service reports it available |
| `GET /engines` | what the redaction service offers: pattern formats and the NER engines |
| `POST /policies/try` | a policy as edited and a text: every item found with its group, engine and score. No side effects; rate limited with the test box |
| `GET/POST /classifiers`, `PATCH/DELETE /classifiers/{id}` | decision models |
| `GET/PUT /settings` | the entity types users may name in their own policies |
| `GET /options` | what the editors may pick |
| `GET /status` | whether this worker loaded the pipelines, and unreadable rows |
| `POST /test` | the real evaluation over given facts, with no side effects (`claims` says which rule claimed which text); 30 runs a minute |

A user's are under `/api/me/privacy` (`pipelines`, `policies`, `in-force`, `options`, `test`,
cookie session): the same shapes, enforced as above.

## The NER engine of a policy

A policy chooses its NER engine, **spaCy** or **GLiNER**, among the engines the redaction service
reports available; no choice means the deployment's default. When several policies of different
engines apply to one request, the redaction service is called **once per engine** and the spans
are merged. Built-in formats go with every call. Keyword lists and regular expressions always
run in the gateway. At most 30 custom entity types (each label at most 80 characters) can be asked
of one engine at once. See [Redaction](redaction.md#items-two-groups-never-mixed).

## Migration

Migration `0086` (ADR 0114) converts an existing deployment and is **irreversible** (take a database
backup first): `chat` and `embed` pipelines become `content` (identical copies merge into one for
any model type, otherwise each keeps a model type of its own), `read` becomes `extract`, each rule
becomes "When policies + conditions -> mode" (`refuse` is `block`, `redact with P` is a rule over P
with P's most common item mode and the rest as overrides, `observe_only` is `detect only`,
"everything else" with a policy is a final rule), and policies lose their per-item modes after they
are converted. Every lossy case is logged (`privacy rules v2 0086: ...`) and listed in the ADR:
first-match shadowing is gone, a block with no policy is kept switched off with a note, items that
were `off` are dropped, and a policy default mode is written out as explicit types.
Earlier migrations (`0082` to `0085`) are unchanged.

Rotating `GATEWAY_REDACTION__PLACEHOLDER_KEY` invalidates embeddings indexed with old placeholders:
knowledge bases must be re-embedded.

## What it does not do yet

Classifiers are scaffolding (configuration, the call path, fail-closed handling; no model is
bundled), and optional enrichment by clients (Cerea's descriptions of retrieved text) is a later
phase of ADR 0113. A user cannot choose a NER engine for their own policy; they use the
deployment's default.
