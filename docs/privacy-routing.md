# Privacy pipelines

A **redaction policy** (see [Redaction](redaction.md)) says *what to hide*. A
**pipeline** says *when and how* a request is handled: which reader opens a file,
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
| Job | what to hide | when and how |
| Scope | none | who and target |
| Rules | modes per entity type | ordered conditions → action, plus a fixed "everything else" |

**Inside one pipeline the first rule that matches wins** (observe-only rules are
logged as would-fire and the search goes on). **Across pipelines nothing is
first: every pipeline in scope runs, and the results combine.**

| | How it combines |
|---|---|
| Where it is handled | the **ladder**: the highest level across all pipelines wins |
| What is hidden | the policies each pipeline chose are **added up**; for each entity type the **strictest mode** wins (`anonymise_restore` < `anonymise` < `redact` < `block`, the fold of ADR 0038, unchanged) |
| The reader, the engine | an **administrator** pipeline's choice holds; a user's applies only if no administrator pipeline chose one; otherwise the default |
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
| Read | `/v1/ocr` | file name, file type, whether it is a scan |
| Embed | `/v1/embeddings`, `/v1/rerank` | what the text contains (health data, IDs), its language |
| Chat | `/v1/chat/completions`, `/v1/responses`, `/v1/messages`, and the other chat surfaces | what the messages contain, its language |

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
appears at most once per rule.

| Key | Kind | Values |
|---|---|---|
| `filetype` | read | `pdf`, `word`, `excel`, `powerpoint`, `image`, `text` |
| `scan` | read | yes or no: the file is a scan |
| `filename` | read | globs: `*`, `?`, `[abc]`, case-insensitive, matched on the base name |
| `health` | embed, chat | yes or no: the text contains health data |
| `ids` | embed, chat | yes or no: the text contains IDs |
| `lang` | embed, chat | a language code such as `it` or `en` |

- **A scan** is found while reading. A pipeline that depends on it makes the
  gateway read the file first on the **built-in extractor** (the file never
  leaves the deployment for that), then judge again. If no built-in reader is
  available to the caller, or the document is a link, the request is refused
  (fail closed), naming the rule that was waiting.
- **A condition that cannot be decided never fails open.** When the detector a
  condition decides with is unavailable, or the input is too large to check in
  full, the condition counts as a match only on a rule that protects (a level above
  the base, or a policy) and whose condition is not negated: then the rule acts.
  A negated condition ("no health data -> pass") or a rule that adds nothing could
  let the text it exists to catch through, so the request is refused with a 503
  (`privacy_undecidable`) naming the rule and its pipeline. An observe-only rule
  simply does not match.
- **Unknown name or type fails closed.** A link's name and extension are taken
  from its path. When a file still has no name or no known type, a rule that
  protects (refuses, reads elsewhere, redacts) treats its `filename` and
  `filetype` conditions as **matching** (the editor warns), and the log says so
  under `degraded`.
- **File name and type are a convenience, not protection.** A renamed file is not
  caught. The console says so on a protective reading rule that depends only on
  them. Protect content with embed and chat pipelines that use `health`.

### How to decide: the quick check, or a detector profile

`health`, `ids` and `lang` are judged from the request's own text, **every input
separately and in full** (long texts are scanned window by window with overlap).
A batch holds a verdict if it holds for *any* input; negation flips the whole
batch. A text over 2,000,000 characters (or a request over 4,000,000 in all)
cannot be checked in full: the condition then **matches**, fail closed. The
classifier runs only if an enabled pipeline of that kind has a rule that uses its
verdict, and only for pipelines in scope. Each such condition has **how to
decide**:

- **Quick check** (the default): health vocabulary in Italian and English,
  identifier formats with the checksum that makes a match worth reporting (Italian
  fiscal code, VAT number with its context, IBAN, payment card, US SSN, NHS
  number), and a stop-word language detector. Microseconds. It finds what is
  written in the usual vocabulary and misses health data written in plain words.
- **A detector profile**: the profile's engine decides. A profile can decide only
  if its engine is `patterns` or `presidio`; a model engine is *not installed* and
  follows its fallback.

If the chosen profile cannot decide, **its fallback** applies, set per profile:
`refuse` (the default) fails closed, `fallback_to` another profile uses that
profile's method and records it as a weakening.

## Reading

| Level | Effect |
|---|---|
| default reader | the model the caller named reads the file, possibly a remote one |
| **read with** a reader | any configured `/v1/ocr` model: the **built-in extractor** (`builtin`; the first extractor model the caller may use), or another OCR provider such as a facility reader. It **overrides the model the caller named**. |
| refuse | 403 with `routing_refused`, naming the rule and its pipeline. Never the content or the file's name |

- The built-in extractor may name an **engine** (`markitdown`, `ocr`, `docling`;
  passed as `X-Extractor-Engine`). The engine is quality, not privacy, so it is
  not on the ladder. An administrator pipeline's engine holds, then a user's, then
  the deployment default.
- **It fails closed when it cannot be done**: the document is a link and the
  reader is the built-in extractor (which reads only documents sent with the
  request, never a URL: that would be a server-side request forgery), or the
  reader is not available to the caller (not granted, switched off).
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
   (file type and name) come before the text is classified, and the first
   matching non-observe rule decides (or the fixed last rule when none does).
3. The answers combine as above.
4. A refusal is a 403; otherwise the folded policy redacts, and the request goes on.

Pipelines, policies, profiles and the names of readers are held in memory on each
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
  policy, choose an engine. There is no "default reader" or "pass" action for a
  rule (that is what happens anyway), though "everything else" may add nothing.
- **Policies**: an administrator's, or their own. A user pipeline's policies are
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
unknown file type), a level the kind does not have, a missing name, a profile,
policy, reader, user, group, key, provider or model that does not exist, two rules
with one name. Names are unique within a kind (per owner).

Warnings never block; they come back with every pipeline and from `POST
/api/admin/privacy/pipelines/check` (a draft):

| Code | Meaning |
|---|---|
| `never_fires` | An earlier enabled rule of the pipeline catches everything this one does |
| `partly_shadowed` | An earlier rule catches some of what this one does |
| `unavailable_now` | A profile or engine it uses cannot decide or run now |
| `weakens` | A profile it uses falls back to a weaker method |
| `convenience_only` | A protective reading rule that depends only on name or type |
| `catches_all` | The rule has no conditions |
| `soft_cap` | More than 20 rules in one pipeline |

Shadowing is a hint, not a proof. Observe-only and disabled rules never shadow.

## The administrator API

Under `/api/admin/privacy` (administrators only, audited as `privacy_pipeline.*`,
`redaction_policy.*`, `privacy_profile.*` and `privacy_settings.*`):

| Route | Purpose |
|---|---|
| `GET /pipelines`, `POST /pipelines`, `GET/PATCH/DELETE /pipelines/{id}` | pipelines (`kind`, `name`, `enabled`, `who`, `target`, `rules`, `otherwise`); the kind is fixed |
| `POST /pipelines/reorder`, `POST /pipelines/check` | the order within a kind; warnings for a draft |
| `GET/POST /policies`, `PATCH/DELETE /policies/{id}` | redaction policies; one a pipeline applies cannot be deleted |
| `GET/POST /profiles`, `PATCH/DELETE /profiles/{id}` | detector profiles, including `user_available` |
| `GET/PUT /settings` | the entity types users may name in their own policies |
| `GET /options` | what the editors may pick: users, groups, keys, providers, models, readers, policies, profiles |
| `GET /status` | whether this worker loaded the pipelines, and unreadable rows |
| `POST /test` | the real combination over given facts, with no side effects; the text and file are never stored or logged; 30 runs a minute |

A user's are under `/api/me/privacy` (`pipelines`, `policies`, `in-force`,
`options`, `test`, cookie session): the same shapes, enforced as above.

## Detector profiles on policies

A policy may name only the default detector profile for now (the API refuses any
other with "Detector profiles on policies take effect in a later release", and the
console disables the picker): detection runs on the deployment's one detector, so
another profile would look applied and not be. Profile engine names are validated.

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
- A routing rule that lost a condition (a `local`/`cloud` target, a mixed who) goes in a pipeline of its own so it can only add; a verdict condition with no values always holds.
- **Downgrading** writes a policy plus its three plain pipelines back as the scoped rule, and a user's policy back as their rules. If anything cannot be said in the older model (pipelines with rules, an unusual scope, two policies on one scope, a limit on users' entity types) the downgrade is **refused** with the list; `PYSTINO_ALLOW_LOSSY_DOWNGRADE=1` drops it anyway.

A deployment migrated from redaction rules logs as it always did: only a request
where something was hidden leaves an entry, until a pipeline does real routing
(a rule, or a last rule that is not the base level).

## What it does not do yet

Model-based classifiers such as GLiNER (a model profile can be created and chosen
today, but is reported *not installed* and follows its fallback), and optional
enrichment by clients (Cerea's descriptions of retrieved text), are later phases of
ADR 0113.
