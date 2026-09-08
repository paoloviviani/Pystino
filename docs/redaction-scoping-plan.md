# Redaction: making it visible, and making it scoped

- Requested 2026-08-17. **Step 1 of 3 is built**: the console can now see the
  redaction layer (`GET /api/admin/redaction`, the Redaction screen). Steps 2
  and 3 — scoping, then configurable engines — are still the requirement below,
  recorded before being designed so the design is not reverse-engineered from a
  diff.
- Related: [ADR 0012](https://example.invalid/viviani/ai-stack/-/blob/main/docs/adr/0012-redaction-interface.md) (the detection contract),
  [ADR 0026](https://example.invalid/viviani/ai-stack/-/blob/main/docs/adr/0026-pluggable-detection.md) (the `llmp.redactors` entry point),
  [ADR 0009](https://example.invalid/viviani/ai-stack/-/blob/main/docs/adr/0009-quota-model.md) (the precedence rule to copy).

## 1. Visibility — **done**

`GET /api/admin/redaction` reports the layer as this worker is actually running
it, and the console renders it at `/admin/redaction`. Three decisions in it worth
keeping when the write path arrives:

- **The engine comes from the constructed redactor, not the setting.** If the two
  ever disagree, reporting the setting describes a deployment that does not
  exist.
- **The service is asked at read time**, not remembered from startup. A detector
  that was reachable then and is not now is the failure that matters, and with
  `fail_open` false it means every request is currently being refused.
- **The warnings are computed server-side** and rendered verbatim, the same
  convention as a report's disclosures, so the wording lives with the rule and is
  testable in Python. They are the point of the screen: a configuration dump
  says what the settings are, the warnings say the settings are not achieving
  what they look like they achieve. Covered so far — `noop`; `fail_open`; an
  unreachable service (worded differently for fail-open and fail-closed, because
  those are different emergencies); a configured language the service does not
  serve; a language served without an NER model; entity types configured that the
  service cannot detect and therefore silently ignores; placeholders left in the
  response.

What it deliberately does **not** expose: the placeholder key, ever — only
whether it is set. The endpoint is stripped of any `user:pass@` on the way out,
because a URL is exactly the setting that grows basic auth and this response is
rendered in a browser.

The activity figures come from `usage_records` rather than a counter, so they
survive a restart and agree with the reports. The count of entities *actually
removed* is the only number that distinguishes a working layer from one that is
switched on and detecting nothing.

## What made this worth doing first

Everything below needs somewhere to show its effect, and this is it. Scoping
without visibility would mean an operator setting a per-group rule and having no
way to confirm it took.

## 2. The original problem statement, kept because it is the acceptance criteria

Redaction *is still* process-global configuration —
`GATEWAY_REDACTION__ENGINE`, `__ENDPOINT`, `__FAIL_OPEN` and the rest, read at
startup into `RedactionSettings`. What changed in step 1 is only that it can now
be seen. These were the questions an operator could not answer, and each is now
answered by the screen; they are listed here as the checklist it was built
against:

- is redaction on at all, or is `engine` still `noop`?
- which engine — and is it the Presidio service or something else?
- is that service actually reachable *now*, or is every request quietly
  fail-closing with a 502 (or worse, fail-opening if someone set
  `fail_open=true`)?
- what is it configured to look for — `entity_types`, `score_threshold`,
  `language`?
- is `restore_in_response` on, i.e. do callers get real values back?

The per-request evidence exists — `usage_records.redaction_engine` and
`.redacted_entity_count` — but nothing aggregates or exposes it.

All of the above are on the screen. The one addition made while building it that
was not in this list: the detection service's own `/healthz` reports its engine
version, the languages it serves, the NER model behind each, and every entity
type it can detect — so the screen shows what the deployment *can* do, not only
what it is configured to ask for. That is also what makes the mismatch warnings
possible.

## 3. Admin-configurable engines — **the engine itself is done**

*Updated 2026-08-24.* An admin can now see every installed engine and choose
which is in force, from the console, recorded on an append-only row. See
[ADR 0033](https://example.invalid/viviani/ai-stack/-/blob/main/docs/adr/0033-redaction-engine-selection.md), which also records why the
warning below about a console toggle was overridden rather than honoured — the
short version being that the effort went into the word *silently*, and that the
alternative an operator actually reaches for during an incident is editing an
environment variable and restarting, which leaves no record at all.

What is still true below, and still to do: the **endpoint** and the detection
**parameters** are not editable. The endpoint especially — an endpoint that can be
typed into a browser is an endpoint that can be pointed at a logger, with every
prompt in plaintext. That needs the test-before-save shape this section describes
and more thought than the engine list needed.

## 3a. The original reasoning, kept

Only **Presidio** exists today, behind the `http` engine contract, and the
contract is deliberately generic — anything that speaks the detection API in
`llmp_shared.redaction` works, and third parties register through the
`llmp.redactors` entry point (ADR 0026). So "which do we support beyond
Presidio" is, honestly: none yet, and the plugin seam is already there for when
there is one.

That makes the useful first step narrow — let an admin point the `http` engine
at a **different endpoint** and change the detection parameters from the
console, rather than invent a provider-registry for engines that do not exist.
An engine dropdown listing exactly one option is not worth a migration; an
editable endpoint plus a test button is.

Two things this must not do:

- **Never let the console turn redaction off silently.** Moving from a working
  engine to `noop` is the one change that makes the system quietly stop
  protecting anything, which is the same argument `fail_open=false` already
  makes. It needs to be loud, and probably needs to be a deployment-level
  decision rather than a console toggle.
- **Never accept an endpoint without testing it.** A saved endpoint that does
  not answer means every subsequent request fails closed. Test-before-save, as
  providers do.

Configuration currently lives in environment variables, so anything editable
needs a database row that overrides them, and a clear precedence between the
two. Worth an ADR of its own — this is the same "environment upstream became a
provider row" migration shape as `providers`.

## 4. Where redaction applies — the actual request

It is all-or-nothing today. Wanted, in the requester's words:

- **always on for any call** — admin-imposed, cannot be overridden;
- **per model** — some models are hosted somewhere that must never see
  personal data, others are local and may;
- **per provider** — the more natural axis for the same concern, since the
  trust boundary is the endpoint, not the model;
- **per user or group** — a research group handling clinical data redacts, an
  engineering group testing prompts does not.

### The precedence rule to use

Copy the quota engine, which already solved this shape: *all matching rules must
pass, so adding one can only tighten a budget.* The redaction equivalent:

> Redaction applies if **any** applicable scope requires it. Adding a scope can
> only ever increase what is redacted, never decrease it.

That makes the global "always on" switch simply the broadest scope rather than a
special case, and it makes every other scope safe to add — because no
combination of them can produce *less* redaction than before. The alternative,
most-specific-wins, means a per-model exemption can silently switch off a
group-level requirement, and the failure is invisible: prompts flow unredacted
and nothing errors.

An explicit exemption (per model, say, for a local endpoint) is then a
deliberate, separate, auditable thing — not a side effect of ordering.

### Things that will need deciding

- Which scopes compose, and where the row lives — one `redaction_rules` table
  scoped like `limit_rules` (`scope` + `scope_id`) is the obvious parallel, and
  reuses a pattern the console already renders.
- Whether a rule can also *narrow* `entity_types` per scope, or only turn
  redaction on. Narrowing per scope reintroduces the "can this make it weaker"
  question and should probably wait.
- `restore_in_response` per scope, or global. It is a correctness property of
  the conversation, not a policy one, so probably global.
- What `usage_records` should record so a past request can be explained: it
  already stores the engine and the entity count, but not *which rule* caused
  redaction to apply. Without that, "why was this redacted" is unanswerable
  after the fact.

## 5. What it detects is wrong, and that is the more urgent half — **fixed, ADR 0037**

Recorded 2026-08-25, from a real request on the deployment. Scoping (§4) decides
*whether* redaction runs; this is about what it does when it does run, and it is
the failure a user actually hit first.

The prompt, in Italian, asking for a summary of a news site:

```
Riassumi le notizie del giorno da ilpost.it
```

What reached the upstream, from the real path (`build_redactor` against the
running detector, engine `http`, language `en`, threshold 0.5, no
`entity_types` filter):

```
<PERSON_7KKAPJPZOK> le notizie del giorno da <URL_LEH7B4IAET>
```

Two spans, both from a default deployment with nothing misconfigured:

| Entity | Score | Text | What it actually is |
|---|---|---|---|
| `PERSON` | 0.85 | `Riassumi` | the **verb**: "summarise" |
| `URL` | 0.50 | `ilpost.it` | the **source the user asked to read** |

The model was asked, by a stranger with no name, to summarise nothing in
particular from somewhere unnamed. It answered as well as that deserves. Note
what is *not* here: no date was detected — the reported suspicion that the date
had been replaced was wrong, and the verb is the surprise.

### Why each one happens

- **`PERSON` on an Italian verb.** The English spaCy model is doing NER on
  Italian text. A capitalised sentence-initial word it does not know is a person,
  confidently — 0.85, well above any threshold anyone would set. This is the
  `degraded_languages` case from ADR 0026 arriving as a wrong answer rather than
  a missing one, which is worse: the healthz field says Italian names are
  *under*-detected, and what actually happens is that ordinary Italian words are
  *over*-detected as names.
- **`URL` on the source.** Presidio detects URLs because a URL can carry
  identity — a profile link, a signed download. `ilpost.it` carries none. But the
  detector has no way to tell those apart, and the gateway's job is to redact
  what it is told is PII, so both halves behaved as designed and the result is
  still useless.

### What this says about the design

*Written before the fix; kept because it is the reasoning ADR 0037 acted on.
Points 1, 2 and 3 are done — a per-entity policy with an allow-list, editable in
the console. Point 4, showing an operator which spans were replaced, is not.*

The two things the request asked for — "more configurable" and "more standard" —
are the same conclusion from two directions:

1. **`entity_types` exists and nothing sets it.** `RedactionSettings.entity_types`
   is `None`, meaning "everything Presidio knows". Nobody chose that; it is the
   default of a field that was never filled in. A deployment that redacts
   `PERSON`, `EMAIL_ADDRESS`, `PHONE_NUMBER`, `IBAN_CODE`, `CREDIT_CARD` and the
   national identifiers, and leaves `URL`, `DATE_TIME`, `LOCATION` and `NRP`
   alone, would have answered this prompt correctly. That is one environment
   variable today and needs no code — but it is per-process, which is exactly the
   limitation §4 is about, and it is not discoverable from the console.
2. **Language is a per-process constant, and the wrong one is not an error.**
   `GATEWAY_REDACTION__LANGUAGE=en` against Italian prompts is not a
   misconfiguration anyone gets warned about. Either the language travels with the
   request (the detector already takes it per call), or it is a scope like any
   other, or the detector is asked to decide. Whichever, "one language per
   gateway" does not survive contact with a bilingual foundation.
3. **An allowlist is missing.** Presidio has `allow_list` in its own request
   model and the contract does not carry it. A deployment that knows `ilpost.it`,
   `github.com` and its own domain are not identity should be able to say so once.
4. **Nothing shows the operator what was replaced.** `redacted_entity_count` says
   two; it does not say a verb became a `PERSON`. Somewhere between "trust it"
   and "read the transcripts" there should be a way to see, for one request, what
   the detector claimed — and that is the feature that would have found this in
   an afternoon rather than in production.

Note the ordering this implies. Scoping (§4) makes redaction apply to fewer
requests; none of it makes redaction *correct* on the requests it does apply to.
On the evidence above, tuning what is detected — entity types, language,
allowlist, and a way to see the spans — is the more valuable half and is mostly
configuration rather than schema.

## The write path, concretely

Now that the read side exists, here is what step 2 actually costs. Recorded so
the next session does not re-derive it.

### The table

`redaction_rules`, shaped like `limit_rules` because the console already renders
that shape and an operator already understands it:

| column | why |
|---|---|
| `scope` | `global` / `provider` / `model` / `group` / `user` — the same enum shape as `LimitScope`, which has `global`/`group`/`user`/`api_key`. Not the same enum: redaction wants provider and model, quotas want api_key. |
| `scope_id` | null for `global`, else the subject. |
| `require_redaction` | bool. **Only ever `true` in the first version** — see below. |
| `is_active` | so a rule can be parked without losing who wrote it. |
| `created_by`, `created_at` | this is a policy decision about personal data; who made it is part of the record. |

A partial unique index on `(scope, scope_id)` where `is_active`, matching the
expression index `limit_rules` uses, so a duplicate is a 409 rather than two
rules that disagree.

### The precedence rule, and why `require_redaction` is write-only-true at first

> Redaction applies if **any** applicable scope requires it.

An `false` value would mean "exempt", and an exemption is the one thing that can
make the system redact *less* than it did yesterday. Shipping the additive half
first means no combination of rules can weaken the layer, which is the property
that makes every later addition safe. Exemptions can come later, as a distinct
and deliberately noisier feature.

This is the quota engine's rule with the comparison inverted, and the parallel is
worth stating in the code: quotas are *all rules must pass*, redaction is *any
rule requiring it wins*. Both mean "adding a rule can only tighten".

### Where it plugs in

`_metered.begin()` already resolves the model, and through it the provider, and
holds the principal — so every scope a rule can name is in scope at the one place
redaction is invoked. The lookup is one query per request against a table that
will hold single digits of rows, so it wants the same treatment `access.py` got:
resolved in the admission path, not lazily mid-stream.

The redactor itself does not change. What changes is whether it is called, which
means `RedactionSettings.engine` stops being the on/off switch and becomes "which
engine, when redaction is required". A deployment with no rules should behave
exactly as today, or upgrading changes behaviour silently — so **no rules means
fall back to the current global setting**, and the Redaction screen must say
which of the two is deciding.

### What the ledger must record

`usage_records` already has `redaction_engine` and `redacted_entity_count`, and
neither answers "why was this redacted". Add the rule id, or at least the scope
that matched. Without it, an operator asked why a particular request was redacted
six weeks ago has no answer, and that is precisely the question a data-protection
review asks.

### Sequencing from here

Scoping next: the table, the precedence rule with tests over every combination,
the admission-path change, and the console screen gaining a rules list. Then
configurable engines, which is still last — only one implementation exists to
point at, so the useful version is an editable endpoint with a test-before-save
rather than a registry of engines that do not exist.
