# Privacy

Privacy in Pystino is two objects, each with one job, and one area of the console
for both: **Privacy**.

| | Redaction policy | Pipeline |
|---|---|---|
| Says | **what to find, and hide**: items in two groups, a mode for each | **when and how** a request is handled |
| Has | a name, a NER engine (spaCy or GLiNER, or the default), **patterns** (built-in formats, regular expressions, keyword lists) and **entities** (the engine's types), each with a mode: leave, detect only, redact, anonymise (with or without restoring the answer), block | a **kind** (read, embed, chat), a **scope** (who, which target), ordered **rules**, and a fixed last rule, "everything else" |
| Scope | none: no user, group, provider or model | who (all, users, groups, keys) and target (all, only, all except, over providers and models) |
| Several apply | all of them, and the **strictest mode wins** per entity type | all in scope run and **combine**; inside one pipeline the **first rule that matches wins** |
| Lives in | console: **Privacy, Redaction policies** | console: **Privacy, Pipelines** |

A policy is also what **conditions** look things up with: a rule can say "*policy*
finds at least N items" (embed, chat), next to the file type, the file name,
whether a file is a scan (reading decides from the file, never its text), and
"classified as *classifier*" (embed and chat; a decision model, for large
deployments). There is no built-in topic and no quick check.

The two are documented separately: [Privacy pipelines](privacy-routing.md) (when
and how: the kinds, the ladder, conditions, readers, user pipelines, migration) and
[Redaction](redaction.md) (what is found and hidden: policies, patterns and
entities, modes, the log and the summary).

## How a request is handled

1. The **kind** comes from the endpoint: reading a file (`/v1/ocr`), embedding
   (`/v1/embeddings` and `/v1/rerank`), or chat (every chat surface).
2. Every pipeline of that kind **whose scope covers the request** runs. Inside
   each, the first matching rule decides what that pipeline asks for.
3. The answers **combine**. *Where it is handled* is a ladder and the highest
   level wins across all pipelines (read: default reader, then read with a chosen
   reader, then refuse; embed and chat: pass, then refuse). *What is hidden* adds
   up: the policies the pipelines chose are folded and the strictest mode wins
   for each entity type.
4. What is left goes to the provider, or the request is refused with a 403 that
   names the rule, never the content.

Nothing is remembered between requests, and every OpenAI-compatible client gets
the same protection with no Cerea headers.

Two things follow from the design and are worth saying once:

- **Whole files leave whole.** A file sent to a remote reader leaves as it is;
  redaction only screens the text that comes back. To keep a file here, read it
  with the built-in extractor, or with a reader deployed in your facility.
- **There is no "local".** A provider is a provider. Pystino does not know which
  are on your own infrastructure; scope decides which pipelines guard which
  targets (leave a facility vLLM or your own embedder out of the scope of the
  pipelines that guard remote providers). Nor is there a reroute between models
  for embedding or chat: vectors from two models do not match, and choosing the
  model is the caller's job. To keep text in-house, refuse it and point the client
  at your own model.

## Users

A user can write **pipelines of their own**, for themselves only, and they can
only **add** protection: read with a reader they are granted, refuse, redact with
a policy (an administrator's, or their own). The combination
takes the highest level and the strictest mode, so a user's pipeline can never
lower anything an administrator's did. A user may name an administrator's policy
only if it is marked **shared with users**, in "apply" and in "finds at least N
items"; "classified as" is the administrators' alone, because it sends text to a
provider. A user's own policy uses custom patterns and only the entity types an
administrator has made available (Redaction service tab). See [User pipelines](privacy-routing.md#user-pipelines).

## The console

| Tab | What it holds |
|---|---|
| **Pipelines** | the **Privacy: On / Off** master switch (off means no pipeline runs and nothing is hidden: the `noop` engine under a plain name, with an inline confirmation, the append-only engine row and its audit, and a warning banner across the Privacy area while it is off); pipelines by kind: scope, rules as sentences, the fixed last rule; the editor (ladder as radios, "Redact with", observe-only), reorder, turn off |
| **Redaction policies** | one editor with two sections: *Patterns (fast, deterministic)*: built-in formats, regular expressions, keyword lists with a case checkbox; *Entities (NER, probabilistic)*: the engine picker (what is available), its entity types with a mode and a threshold, custom GLiNER types. A mode per item, `detect only` among them, same-length placeholders, the allow-list, and a **Try it** box |
| **Classifiers** | decision models: a chat model and a topic in plain words; says when no model provider is configured and that classifying sends the text to that provider |
| **Detectors** | exactly what the redaction service reports, one card per detector (the patterns, spaCy, GLiNER) with an On/Off switch, its capabilities (formats, languages, speed, entity types, custom types) and "used by N policies"; a detector no active policy uses can be switched off, one a policy uses is refused (409, naming the policies); a policy can only choose detectors that are on. Then what users may add to their own policies, and, collapsed, the engine's activity and configuration |
| **Test** | one box: a kind of request, a user and a target, a text or file; every pipeline in scope with its trace (each item that made a condition true, with its group and, for entities, engine and score), the reroute, the ladder, who chose the reader, and the policies added up. Nothing is stored |

Users have **Privacy pipelines** (their own pipelines and policies, the
administrator's read-only list, and a test box locked to themselves) and
**Redactions** (the log). A **How it works** overlay explains the model once; the
screens keep their explanations in (?) tooltips.

Old links keep working: `/admin/redaction` and `/admin/privacy/hidden` go to
Redaction policies, `/admin/redaction/rules/{id}` and
`/admin/privacy/hidden/rules/{id}` go to the same policy (a rule kept its id when
it became a policy), and `/admin/privacy/profiles` and the former `/admin/privacy/service` go to the Detectors tab (`/admin/privacy/detectors`).

## Seeing it before choosing it

`GET /v1/pystino/privacy` answers, for the calling user and per embedding model
and per chat model, what the pipelines in scope do, in short words ("refuses
codenames", "redacts with Default"). A client such as Cerea can show it beside
the embedder choice, so a knowledge base's owner chooses informed, up front.
Read-only; labels and names only.

## Upgrading from scoped rules

Migration `0082` turns what you had into the new shape and requests are handled
exactly as before. See [migration](privacy-routing.md#migration).
