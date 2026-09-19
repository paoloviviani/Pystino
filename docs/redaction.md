# Redaction

Redaction is the layer between the caller's prompt and the upstream provider:
detected entities are replaced with deterministic placeholders before any text
leaves the building, and restored (or not) in the answer shown to the reader.
PII never reaches the upstream.

It is also **~90% of the CPU**. Detection is named-entity recognition and
scales with prompt length — roughly 0.1ms of CPU per prompt token, against a
gateway cost that stays flat at 24–31ms. Capacity planning is redaction
planning; see [Measured performance](performance.md).

## Out of process, by contract

The detection service (`services/redaction`, Presidio) is a separate process
behind a wire contract defined in `packages/shared-py`
(ADR 0012). The split has two reasons:

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
would save one container and one HTTP hop, and the in-process seam exists
(ADR 0026) — it is meant for engines where a
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
the engine choice is a UI decision that reaches every worker within ten seconds
(ADR 0033), and the sidecar is only
needed when the engine is `http`.

## The engine is a decision, not a build flag

Installed engines are described by a registry; which one runs is an admin
decision stored on an append-only `redaction_config` row
(ADR 0033). `RedactionResolver`
polls it every 10 seconds, so a change reaches every worker without a restart
and without a query on the request path. Switching to an engine that redacts
*less* than the current one requires a written reason, kept permanently — the
row is append-only, so the history of that decision survives.

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

Three shapes, and all three already work. Written down here because the pieces
were scattered across a compose overlay, a build argument and an environment
variable, and "can we run this without Presidio" is a question that should not
require reading three files to answer.

| You want | How | What you get |
|---|---|---|
| **No redaction, no sidecar** | Leave the engine at `noop` and omit `docker-compose.redaction.yml` | The default. The base compose file names no redaction service, and `noop` needs none |
| **Pattern matching only, no NER** | Build the image with `SPACY_MODELS=` empty, run it with `REDACTION_NLP_ENGINE=disabled`, engine `http` | Presidio's pattern recognisers — cards, IBANs, emails, phone numbers, the Italian identifiers — at about 150MB and without the CPU cost that scales with prompt length |
| **Everything** | The redaction overlay as shipped | NER for the configured languages on top of the patterns |

Two things to know before choosing the middle row.

**"Pattern only" is not "without Presidio".** The regex recognisers *are*
Presidio's, so the sidecar is still deployed and still called per request —
what goes away is spaCy, the language models and the inference cost. There is
no in-gateway regex engine, and deliberately so: a detector in the request path
is what ADR 0012 rejected, and the reasons (CPU-bound, synchronous, crash
isolation) do not change because the detector got simpler.

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

For each entity type, a policy names a **mode**
(ADR 0037) — two independent questions, what
the model sees and what the reader gets back:

| Mode | Upstream sees | Reader gets back |
|---|---|---|
| `off` | the real value | the real value |
| `anonymise_restore` | `<PERSON_xxxx>`, stable | the real value, restored |
| `anonymise` | `<PERSON_xxxx>`, stable | the placeholder |
| `redact` | `<PERSON>` — lossy, two people collapse into one label | the label |
| `block` | nothing — the request is refused | a 403 that never quotes the matched text |

`block` exists for values whose *presence* is the incident — a pasted API key —
where silently replacing it would tell nobody it happened. The 403 names the
entity type and the rule that decided, and **never echoes the matched text**:
an error body gets logged and pasted into tickets.

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

There is no deployment-wide policy any more
(ADR 0039); there is a rules table
(`redaction_rules`), and the **catch-all is one rule among them** — scope
`all`, exactly one row may hold it. A rule can scope to `provider`, `model`,
`group`, `user` or `api_key` (ADR 0038) —
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
URL (ADR 0037).

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
because a permissive rule would remove protection
(ADR 0009).

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
- The research and measurements behind scoping are in
  [the scoping design](redaction-scoping-design.md) and
  [the scoping plan](redaction-scoping-plan.md).
