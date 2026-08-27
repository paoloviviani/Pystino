# 0037 — One engine, and a per-entity policy an admin can set

- Date: 2026-08-26
- Status: **accepted, built**
- Requested as: "we need it to make more configurable and more standard […] the
  key features I need are: configurable targets (i.e., letting the admin chose
  what should be redacted vs. what should not.) and possibility to anonymize
  instead of redacting (i.e., replacing the fields back in the response, that
  should also be opted-in/out by the admin)", after a real request came back
  useless.
- Related: [0012](0012-redaction-interface.md) (out of process, spans only),
  [0026](0026-pluggable-detection.md) (the contract this narrows),
  [0033](0033-redaction-engine-selection.md) (the append-only row this reuses),
  [redaction-scoping-plan.md](../redaction-scoping-plan.md) §5 (the measurement).

## Context

`RedactionSettings.entity_types` was `None`, which means "everything Presidio
knows". Nobody chose that; it was the default of a field never filled in. On the
deployment, this prompt:

```
Riassumi le notizie del giorno da ilpost.it
```

reached the provider as:

```
<PERSON_7KKAPJPZOK> le notizie del giorno da <URL_LEH7B4IAET>
```

`PERSON` at 0.85 on *Riassumi* — the verb — and `URL` at 0.50 on the news site
the user asked to be summarised. Both components behaved exactly as designed, and
the answer was worthless. A redaction layer that destroys ordinary requests gets
switched off, which is a worse outcome than any single over-redaction.

## Decision

### 1. Presidio is the engine, not *an* engine

The generic-plugin framing is dropped. The HTTP boundary stays — it is about
CPU-bound inference blocking the event loop (ADR 0012), not about pluggability,
and it is the same boundary Philter and phileas-python arrive at independently.
The thin `/detect` contract stays too, because it is what keeps placeholder
derivation and restoration in the gateway where the key lives. What goes is the
pretence that the *choice* of engine is a live axis: one is installed, and the
work goes into configuring it rather than into abstracting over it.

Alternatives measured rather than assumed, and why each was not adopted:

| | Why not |
|---|---|
| **presidio-anonymizer** | No stable keyed pseudonym: `replace` emits one constant per entity type, so two people in a prompt collapse and nothing survives between turns. `encrypt`/`decrypt` is the only reversible operator and puts base64 in the prompt and the key in the engine. And the restore side is not its problem at all — it happens on the *model's* output, incrementally, mid-SSE. |
| **Philter / Phileas** (Apache-2.0, better footprint: 349 MB against our 1.53 GB) | Its policies live in Philter and are selected by name, so the console would become a second store to keep in step with the first. Our admin surface is our own; Presidio takes its configuration per request, so one store stays one store. |
| **DataFog** (MIT) | Regex-first with optional spaCy/GLiNER — the same wall in-process, and 69 stars for something in the request path. Worth revisiting as a cheap in-process tier ahead of NER, which is independent of this decision. |
| **pii-codex** (BSD-3) | Wraps Presidio. Adopting it means adopting Presidio *plus* a research layer. Its NIST/HIPAA categorisation is interesting for reporting, not for detection. |

### 2. Two axes, four modes, per entity type

"Redact or anonymise" is two questions, and collapsing them is what made the old
single switch unable to express what almost every deployment wants:

| Mode | Model sees | Reader sees |
|---|---|---|
| `off` | the real value | the real value |
| `anonymise_restore` | `<PERSON_K3QF…>` | the real value |
| `anonymise` | `<PERSON_K3QF…>` | the placeholder |
| `redact` | `<PERSON>` | `<PERSON>` |

The fifth combination — opaque upstream, real value back — does not exist,
because an opaque label maps to nothing. That is not a limitation of the
implementation; it is what choosing `redact` means, and the cost is stated on the
screen: two people in one prompt become the same label and the model cannot tell
them apart.

Each entity type may also set its own confidence threshold. `PERSON` at 0.85 and
`URL` at 0.5 are not the same judgement, and one global number forces them to be.

### 3. The default protects by default, minus four types

`default_mode` is `anonymise_restore`, with `URL`, `DATE_TIME`, `LOCATION` and
`NRP` switched off.

The alternative — ship a curated list of "real PII" and default everything else
off — was rejected in the direction that matters: a detector that gains a
national-identifier recogniser in its next release would then start finding
something nobody protects, silently. The failure direction of this file has to be
over-protection.

The four exclusions are the ones measured to break ordinary requests. Each is
still *detected*, and an admin can switch any of them back on and see it take
effect within the resolver's poll interval.

`GATEWAY_REDACTION__ENTITY_TYPES` is translated into a policy rather than
consulted beside one — two mechanisms answering the same question is how a
deployment ends up redacting something the screen says it does not.

### 4. Stored where engine changes are stored

One nullable JSON column on `redaction_config`, the append-only table from
ADR 0033. Same table, same "newest row wins", same poll, same audit trail — a
policy change is exactly the kind of decision that table exists for: it decides
what personal data leaves this deployment, and *who changed it, when and why* is
asked about a window that has already closed.

Null means the row said nothing about the policy, which is not "redact nothing":
the deployment's own default stands. Same rule as "no row means the environment
decides", one level down.

A reason is required **only when the change protects less** — a type switched
off, a mode downgraded, a value exempted. Turning protection on needs no
justification: a prompt with no reader trains people to type "x" (ADR 0033's own
argument, applied to the field below the engine).

### 5. Filtering happens before overlap resolution

The one ordering that is load-bearing, and it is not obvious. A high-scoring
`URL` covering the same characters as a `PERSON` wins the overlap; if the policy
were applied afterwards, discarding the URL would take the person with it and the
name would reach the provider. Removing noise must never remove protection.

## Consequences

- An admin can now answer "what does this deployment redact" from the console,
  and change it, and the change is on the record with a reason where one is due.
- The reported failure is fixed at the source: with `URL` off, that entity type
  is not even asked for when the policy is enumerated, so the detector does less
  work as well.
- The `allow_list` is ours rather than Presidio's request parameter, so it works
  for any detector serving the contract and needed no change to it.
- Still to do, unchanged by this: **scoping** — per model, provider, user or
  group — which is a different question from *what* is redacted, and is the
  remainder of redaction-scoping-plan.md §4. This policy is deployment-wide.
- Not done, and worth naming: nothing yet shows an operator *which spans* were
  replaced in a given request. That is the feature that would have caught the
  original bug in an afternoon rather than in production.
