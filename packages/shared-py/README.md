# llmp-shared

Contracts that more than one Python process must agree on byte-for-byte.

Deliberately tiny — this is not a dumping ground for utilities. Something belongs
here only when two separate deployables would otherwise have to reimplement it and
*disagreeing would be a bug*.

## What is here

`redaction.py`

* The detection service wire contract (`DetectionRequest`, `DetectionResponse`,
  `EntitySpan`). The engine returns **spans only** and never invents placeholder
  text, which is what allows Presidio to be swapped for another engine without
  changing placeholders already shown to users or stored in transcripts.
* `placeholder_for()` — the deterministic keyed-HMAC placeholder derivation, and
  `normalise_entity()`, which decides what makes two mentions "the same entity".

Both the gateway and the Phase 2 detection sidecar derive placeholders
independently, so this code and its HMAC key must produce identical output in
both. That is the entire reason the module exists rather than living in
`apps/gateway`.

`normalise_entity` is effectively part of the persisted data format: changing it
changes every placeholder derived afterwards, so previously issued placeholders
stop matching. Treat it as a migration, not a refactor.
