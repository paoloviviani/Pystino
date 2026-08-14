# Redaction / PII detection service — placeholder

**Nothing is built here yet.** Phase 2. The gateway ships a no-op engine and the
interface it will plug into.

## What goes here

A Presidio-backed HTTP service that **detects** PII and returns spans. It does not
redact, and it never invents placeholder text.

That split is the important part of the design, and it is what makes the engine
replaceable: the gateway performs substitution itself using the shared keyed-HMAC
derivation in `packages/shared-py`. Swapping Presidio for another engine therefore
cannot change a placeholder that has already been shown to a user or written into a
transcript.

## The contract

Already defined, in `packages/shared-py/src/llmp_shared/redaction.py`:

```
POST /detect
  { "texts": [...], "language": "en", "score_threshold": 0.5, "entity_types": null }
  -> { "findings": [ { "index": 0, "spans": [ {start, end, entity_type, score} ] } ],
       "engine": "presidio", "engine_version": "2.2.363" }
```

Both sides already have Pydantic models for this. Report `engine` and
`engine_version` honestly: an auditor asking "what was redacted from this
conversation" needs to know which detector version produced the answer.

## Why this is a separate process, and must stay one

Presidio means spaCy, which is CPU-bound and synchronous. Loaded in-process it
would block the gateway's event loop for hundreds of milliseconds and stall *every
concurrent stream on that worker*, not just the request being screened. The
gateway calls this over HTTP with a timeout budget and a fail-open/fail-closed
switch (default **closed** — a redaction layer that silently stops redacting is
worse than an outage). See [ADR 0012](../../docs/adr/0012-redaction-interface.md).

## Presidio has moved

It is no longer a Microsoft project. As of 2026 it is community-governed under
`data-privacy-stack`, still MIT, actively released (2.2.363, June 2026).

**Use `ghcr.io/data-privacy-stack/presidio-analyzer`.** The old
`mcr.microsoft.com/presidio-analyzer:latest` tag still resolves but no longer
tracks current releases — pointing at it would silently pin you to an old detector.

## What the gateway already provides

- `TextRewriteStage` in `gateway/redaction/base.py` handles the genuinely hard
  part: buffering a streamed response so an entity split across SSE frames
  (`Ma` / `rio Ros` / `si`) is still detected, and releasing held-back text in a
  valid position in the stream. It is tested. A Phase 2 engine supplies only a
  `transform`, which must be idempotent.
- Deterministic placeholders, tested for stability across turns, requests and
  processes.
