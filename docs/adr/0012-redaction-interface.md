# 0012 — The redaction interface and deterministic placeholders

- Status: accepted
- Date: 2026-08-14
- Phase 1 ships the **no-op** engine. Presidio is Phase 2.

## Context

A pluggable redaction layer with full SSE handling, where Presidio is the reference
implementation but the interface must accommodate other engines. What has to be right
*now* is the shape, because the hard parts are structural and retrofitting them means
changing placeholders that have already been shown to users.

## Decisions

### The engine detects; the gateway substitutes

The detection service returns **spans only** and never invents placeholder text. The
gateway performs substitution itself using the shared derivation in
`packages/shared-py`.

That split is what makes the engine genuinely replaceable: swapping Presidio for
another engine cannot change a placeholder already shown to a user or stored in a
transcript, because the placeholder never depended on the engine.

### Out of process, always

Presidio means spaCy: CPU-bound, synchronous, hundreds of milliseconds. In-process it
would stall **every concurrent stream on that worker**, not just the request being
screened. The engine is an HTTP call with a timeout budget.

`fail_open` defaults to **false**: if the detector errors or times out, return 502. A
redaction layer that silently stops redacting is worse than an outage.

`engine="http"` currently raises `NotImplementedError` with a message pointing at
Phase 2 — refused rather than silently downgraded to no-op, so nobody can believe
redaction is on when it is not. `redaction_engine` is recorded on **every** usage row
(`'noop'` included), so a row can never later be mistaken for one that was screened.

### Deterministic placeholders: keyed HMAC of the normalised entity

```
placeholder = "<" + TYPE + "_" + base32(HMAC-SHA256(key, TYPE || 0x1f || normalise(value)))[:10] + ">"
```

- **The same entity yields the same placeholder** in turn 1 and turn 40, across
  requests, users and processes, **with no session state anywhere**. Two processes
  holding the same key derive the same answer independently.
- That is what keeps a multi-turn conversation coherent for the model: it sees
  `<PERSON_K3QF7RZM2A>` consistently and can reason about "that person" without ever
  receiving the real name.
- `0x1f` domain separator: `"AB" + "C"` can never collide with `"A" + "BC"`.
- Entity type is inside the HMAC, so the same string as `PERSON` and as `LOCATION`
  gets different placeholders.
- **Base32**, not base64: no `+`, `/` or `=`, which interact badly with markdown, URLs
  and tokenizers.
- Normalisation (NFKC, casefold, whitespace collapse; digits-only for phone/IBAN/fiscal
  codes) is what makes the scheme robust to the same entity being written differently
  between turns. **It is effectively part of the persisted data format** — changing it
  changes every placeholder derived afterwards, so treat it as a migration, not a
  refactor.

### The asymmetry, which is the whole reason this works without a database

Deriving a placeholder is stateless. *Restoring* the original is not, because an HMAC
cannot be inverted. Restoration uses the `PlaceholderMap` built while redacting the
**current** request — which is sufficient, because any placeholder in a response must
have entered the conversation through the request being served.

`restore()` is a literal substitution over known placeholders, not a regex sweep:
restoration must not depend on the emitted placeholder still matching
`PLACEHOLDER_RE`. If the pattern and the generator ever drift, a regex-driven restore
silently stops restoring; this cannot. Unknown placeholders are left alone rather than
having an "original" invented for them.

### `TextRewriteStage` — the buffering, shipped and tested now

The genuinely hard part of response redaction is that an entity does not respect frame
boundaries: `Ma` / `rio Ros` / `si` across three SSE frames is invisible to a stage
that inspects each frame alone. The base class handles it:

- accumulates per choice, transforms the buffer **including** the tail, and only then
  releases a prefix;
- emits held-back text as a synthesised chunk copying the stream's `id`/`model`/`created`;
- flushes **before** a `finish_reason` frame and before `[DONE]`, so a client never sees
  "finished" ahead of the last of the text;
- flushes if the stream ends without a terminator.

**A bug this design nearly shipped with:** the first version released the prefix and
transformed it in isolation, which cut the entity in half at the release boundary — the
exact failure the class exists to prevent. Caught by a test feeding a name split across
frames.

Subclasses must supply an **idempotent** `transform`, because the unreleased remainder
is kept in already-transformed form. Placeholder substitution satisfies this: a
placeholder does not look like the entity it replaced.

## Consequences

- The HMAC key must be stored alongside the transcripts it labelled. Rotating it
  re-labels every entity and breaks cross-turn consistency for existing conversations.
- `tail_size` must exceed the longest entity the engine can match, or a match straddling
  the boundary is missed. A Phase 2 engine has to pick this from its own entity set.
- Phase 2 adds one branch in `build_redactor()` and one module. The request path,
  response path and placeholder scheme do not change.
- Redaction is not yet wired for **non-streaming** placeholder restoration beyond
  `redact_response_text`, which the no-op satisfies trivially. Revisit when the real
  engine lands.
