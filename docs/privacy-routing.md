# Privacy routing

Redaction (see [Redaction](redaction.md)) decides *what is hidden* from a
provider. Privacy routing decides *how a request is handled* before that: which
reader opens a file, whether text may be embedded by a cloud model, and whether a
request is refused. An administrator writes **rules**; each request is judged on
its own, from what Pystino can see in it. It works for any OpenAI-compatible
client, with no Cerea headers, and nothing is remembered between requests: the
outcome is a function of the request, the rules and the configuration.

Routing never lowers a redaction mode. It can only choose a reader, send text to a
local model, or refuse; the redaction policy still runs after it, unchanged.

## The kinds

A rule is for one **kind** of request, fixed when it is saved. A request meets
only the rules of its kind, in order, and the first match applies.

| Kind | Endpoint | What a rule can see | What a rule can do |
|---|---|---|---|
| Reading a file | `/v1/ocr` | file name, file type, whether it is a scan, who asked | choose the reader, or refuse |
| Embedding | `/v1/embeddings` | who asked, the target model and provider (local or cloud), what the text contains, its language | embed on a local model, or refuse |
| Chat | `/v1/chat/completions`, `/v1/responses`, `/v1/messages` | recorded only (rules arrive in a later release) | none yet |

Every request of these kinds is *described* in the log and the summary
([routing attribution](redaction.md#routing-attribution)) whether or not a rule
exists.

## Conditions

All conditions of a rule must hold (AND). A condition has one or more values (any
of them, OR) and may be negated ("is not"). A rule with no conditions matches
every request of its kind. A key appears at most once per rule.

| Key | Kinds | Values |
|---|---|---|
| `filetype` | read | `pdf`, `word`, `excel`, `powerpoint`, `image`, `text`, `scan` |
| `filename` | read | globs: `*`, `?`, `[abc]`, case-insensitive, matched on the base name |
| `who` | read, embed | `user:<email>` or `group:<name>` |
| `target` | embed | `local`, `cloud`, `provider:<name>`, `model:<name or alias>` |
| `verdict` | embed | `health`, `ids` |
| `lang` | embed | a language code such as `it` or `en` |

- **A scan** is found while reading: the extractor reports a page with no text
  layer (or one it had to OCR). A rule that depends on it makes the gateway read
  the file first on the local extractor, then judge the rules again; if the
  winning rule wants a different reader than the one used, the file is read a
  second time. Nothing leaves the deployment during that read. A document sent as
  a link (not bytes) cannot be read here, so a scan rule cannot be judged on it.
- **Unknown name or type fails closed.** A link's name and extension are taken from
  its path (`https://host/a/referto.pdf?sig=...`). When a file still has no name
  or no known type (a link with no extension, no MIME type sent), a rule that
  refuses, keeps text local or chooses a reader treats its `filename` and
  `filetype` conditions as **matching** (the editor warns: "unnamed_matches"), and
  the log says so under `degraded`. A rule that is observe-only is unaffected.
- **File name and type are a convenience, not protection.** A renamed file is not
  caught. The console says so on any protective reading rule that depends only on
  them. Protect content with embedding rules that use `verdict`.
- **Negation and unknown.** "File name is not `referto*`" matches a request that
  names no file; a protective rule is not skipped because a fact is missing.

### How to decide: the quick check, or a detector profile

`verdict` and `lang` are judged from the request's own text, **every input
separately and in full** (long texts are scanned window by window with overlap,
so padding or a late chunk cannot hide anything). A batch holds a verdict if it
holds for *any* input ("contains health data": any input does; "is in Italian": any
input is). Negation flips the whole batch: "does not contain health data" holds only
if *no* input does. A text over 2,000,000 characters (or a request over 4,000,000
in all) cannot be checked in full: the condition then **matches**, fail closed,
and the log says it was too large. The condition is evaluated only if a rule uses it, and only if an
enabled rule of that kind uses them. Each such condition has a **decide** field:

- **`patterns`** (the default): the quick check. Health vocabulary in Italian
  and English (clinical terms count alone; common words such as "therapy" count
  only in pairs), identifier formats with the checksum that makes a match worth
  reporting (Italian fiscal code, VAT number with its context, IBAN, payment card,
  US SSN, NHS number), and a stop-word language detector. It takes microseconds.
  It finds what is written in the usual vocabulary and misses health data written
  in plain words ("he takes insulin since March"). The patterns are written out in the gateway; Presidio itself stays
  in the redaction service.
- **A detector profile.** The profile's engine decides. In this release a profile
  can decide only if its engine is `patterns` or `presidio` (whose verdicts are the
  quick check); a model engine such as GLiNER is *not installed* and arrives with
  chat rules (phase 2).

If the chosen profile cannot decide, **its fallback** applies, set per profile:

- `refuse` (the default): fail closed. The condition is treated as holding, so a
  rule with a protective action takes it. A rule whose other conditions do not
  hold is not triggered.
- `fallback_to` another profile: that profile's method decides, and the fact is
  recorded as a weakening (`fallbacks` in the log and summary, and a warning in
  the console).

An optional in-process LRU keyed by `sha256(text)` stores the three labels of a
verdict, never text. It only saves time: a miss recomputes the same answer.

## Actions

| Kind | Action | Effect |
|---|---|---|
| read | `reader`: `markitdown`, `ocr`, `docling` | The file is read by that reader (sent to the extractor as `X-Extractor-Engine`), **overriding the model the caller named**. If the caller named a remote OCR model, the request is served by a local extractor model the caller may use; if the document is a link or no such model is available, the request is refused (fail closed). `ocr` is markitdown with OCR switched on for the one request. The response reports the reader that ran. |
| read, embed | `refuse` | 403 with `routing_refused`, naming the rule. Never the content or the file's name. |
| embed | `local` (optional `model`) | The request is embedded by a local embedding model: a model on a provider an administrator marked **local** (`is_local`). The caller must be allowed to use it. A named `model` is checked when the rule is saved (a deployment embedding model on a local provider; not a private `my/` model, not an alias) and again on every request against what actually resolved: if the provider has since stopped being local, the rule shows a "Check" warning and requests are refused. If the target is already local, nothing changes. If there is none the caller may use, the request is refused. |

There is no "let it go as usual" action: no action means default handling.

A refusal is also a row in the ledger (status `blocked`, zero cost, with the
routing block), exactly as a redaction block is.

### Local providers

`is_local` is a provider attribute only an administrator sets (Providers, "Runs
on this deployment's own infrastructure"). It is never inferred from the URL, and
a member's own private provider cannot claim it. It is what `target: local` and a
`local` action mean.

## Evaluation

1. The kind comes from the endpoint.
2. The rules of that kind are taken in order. Disabled rules are skipped.
3. A rule matches when all its conditions hold. Cheap conditions (who, target,
   file type and name) are checked first; the text is classified only when they
   all hold.
4. An **observe-only** rule that matches is recorded (`observed_rules`) and the
   search continues. The first non-observe rule that matches applies.
5. If no rule matches, the request is handled as it always was.

The rules, the profiles and the names of local models are held in memory on each
worker and reloaded every ten seconds (and at once on the worker that served an
administrator's change), so the request path adds no database query.

A worker that has **never** read the rules (the first read failed) answers
`/v1/ocr` and `/v1/embeddings` with a 503 (`privacy_rules_unavailable`) rather
than run with none; `/readyz` reports `privacy_routing` as an error and `/healthz`
mentions it. A single rule row that cannot be read is skipped with an error log
and the rest load; `/readyz` says how many were skipped and
`GET /api/admin/privacy/status` lists them. File name patterns are matched by a
linear matcher, not a regular expression, so a hostile pattern cannot stall a
worker.

## Saving a rule

Errors block saving (HTTP 422, a message naming the field): a kind that is not
available, a key the kind does not have, a value of the wrong shape (a bad glob,
an unknown file type), an action the kind does not have, a missing name, a
profile that does not exist. Names are unique within a kind.

Warnings never block; they come back with every rule and from
`POST /api/admin/privacy/rules/check` (a draft, before saving):

| Code | Meaning |
|---|---|
| `never_fires` | An earlier enabled rule of the kind catches everything this one does. |
| `partly_shadowed` | An earlier rule catches some of what this one does. |
| `unavailable_now` | A profile it decides with cannot decide now, or no local embedding provider exists. |
| `weakens` | A profile it uses falls back to a weaker method. |
| `convenience_only` | A protective reading rule that depends only on name or type. |
| `catches_all` | The rule has no conditions. |
| `soft_cap` | More than 20 rules of a kind. A warning, not a limit. |

Shadowing is a **hint, not a proof**: the check is conservative (it says "never
fires" only when it can show it for one earlier rule, and treats a different way
of deciding as different), so it can miss a case where several earlier rules
together cover a later one. Observe-only and disabled rules never shadow.

## The administrator API

Under `/api/admin/privacy` (administrators only, audited as
`privacy_rule.*` and `privacy_profile.*`):

| Route | Purpose |
|---|---|
| `GET /rules` | Every rule, grouped by kind and in order, each with its warnings. |
| `POST /rules` | Create (`kind`, `name`, `enabled`, `observe_only`, `conditions`, `action`, optional `position`). |
| `PATCH /rules/{id}` | Change a rule. The kind is fixed. |
| `DELETE /rules/{id}` | Delete; later rules move up. |
| `POST /rules/reorder` | `{kind, order: [ids]}`; must name every rule of the kind. |
| `POST /rules/check` | Warnings for a draft. |
| `GET/POST /profiles`, `PATCH/DELETE /profiles/{id}` | Detector profiles: name, engine, labels, fallback (`refuse` or `fallback_to` with `fallback_profile_id`). Status comes from health. The default profile and a profile a rule decides with cannot be deleted; a fallback loop is refused. |
| `GET /status` | Whether this worker loaded the rules, and any unreadable rule rows. |
| `POST /test` | Given a kind and facts (or a file, or text), which rule fires, why the earlier ones did not, and the outcome. The text and the file are judged in memory and are never stored or logged; the answer has labels and names, not content. Rate limited to 30 runs a minute. |

## The console

**Admin, Privacy routing** is the list: rules grouped by kind, each as a sentence
with a badge when it needs a look (*Off*, *Observe*, *Never fires*, *Partly
shadowed*, *Check*); tap a badge to see why. Rules move with the arrows (the whole
new order is saved at once) and switch on and off in place. *How rules work* and
*Start from a goal* (templates) open as overlays. The editor is a sentence: pick
the kind, name it, then chips for each condition (is or is not, the values, and
for what the text contains, *how to decide*) and the action. **Test it** runs
the box. **Detector profiles** shows health, cost and the fallback.

## What it does not do yet

Chat rules (profile, policy and refusal on chat), model-based classifiers such as
GLiNER, optional enrichment by clients (Cerea's descriptions of retrieved text),
and rerouting chat to a local model are later phases of ADR 0113. A model
profile can be created and chosen today, but is reported *not installed* and
follows its fallback.
