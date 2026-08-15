# 0026 — Detection is a plugin, and the Italian model is a build flag

- Date: 2026-08-15
- Status: accepted
- Supersedes nothing; extends [0012](0012-redaction-interface.md) (redaction interface)
  and constrains Stage 2 of the [Phase 2 plan](../phase-2-plan.md).

## Context

Stage 2 was planned as "wrap Presidio in a service and call it". Two things make that
too narrow.

**The licence.** Verified at source rather than assumed:

| Component | Licence | OSI |
|---|---|---|
| Presidio (`microsoft/presidio`) | MIT | yes |
| `en_core_web_lg` 3.8.0 | MIT | yes |
| `it_core_news_lg` / `it_core_news_sm` 3.8.0 | **CC BY-NC-SA 3.0** | **no** |

The Italian models are non-commercial and share-alike, inherited from UD Italian ISDT.
That is a licence on the *weights*, so it travels with any image that contains them, and
"non-commercial" is not a condition a foundation doing contract research can wave
through. It is exactly the case the project's ground rules say to raise rather than
decide.

What softens it: all five Italian identifiers Presidio ships — `IT_FISCAL_CODE`,
`IT_VAT_CODE`, `IT_DRIVER_LICENSE`, `IT_IDENTITY_CARD`, `IT_PASSPORT` — are pattern,
context and checksum recognisers. **None of them needs a spaCy model.** Only Italian
`PERSON` and place-name detection does. So the NC dependency buys one specific
capability, not Italian support as such.

**The bigger point.** Naming one engine in the architecture is the mistake. Detection
models change faster than gateways do, deployments differ in what they are allowed to
run, and the site that has its own model is the site most likely to have a strong
opinion about which. Presidio should be *a* detector we ship, not *the* detector the
design assumes.

## Decision

### 1. The HTTP detection contract is the primary extension point

`llmp_shared.redaction` already defines it: `DetectionRequest` in, `DetectionResponse`
out, spans only. Anything that can serve that contract over HTTP is a detector, in any
language, with any model, and the gateway needs no code for it — only
`GATEWAY_REDACTION__ENDPOINT`.

The contract stays deliberately thin, and the split from [0012](0012-redaction-interface.md)
is what makes it viable: **the detector never invents placeholder text.** It returns
character spans and labels; the gateway derives placeholders by keyed HMAC. So a
detector cannot break placeholder stability, cannot see the placeholder key, and cannot
be relied on for consistency across turns — those are the gateway's job and stay there
whoever detects.

Entity labels are free-form strings. `_safe_label` coerces whatever an engine emits into
the placeholder alphabet, so a detector that says `phone` interoperates with one that
says `PHONE_NUMBER`; they simply produce different placeholders, which is correct,
because they are different labels.

### 2. In-process engines resolve through an entry-point registry

`build_redactor` no longer hardcodes a `match` over known names. Engines register under
the `llmp.redactors` entry-point group:

```toml
[project.entry-points."llmp.redactors"]
my-engine = "my_package:MyRedactor"
```

so `pip install` plus `GATEWAY_REDACTION__ENGINE=my-engine` is the whole integration.
This is the escape hatch for a detector that must run in-process (a pure-regex ruleset,
say, where a network hop is absurd) — the HTTP contract remains the recommended path,
because in-process inference on the event loop is what
[0012](0012-redaction-interface.md) exists to prevent.

An unknown engine name is refused at startup with the list of registered names. It is
never downgraded to `noop`: a gateway that believes redaction is on when it is not is
the worst available outcome.

### 3. The Italian model is a build flag, off by default

`services/redaction` builds without it. `--build-arg SPACY_MODELS="en_core_web_lg
it_core_news_lg"` adds it, and the build prints the CC BY-NC-SA obligation it has just
taken on. The default image is MIT-only and ships all Italian identifier recognisers,
because those are pattern-based.

The caveat is documented where it is acted on — the service README and the build arg's
help text — not only here, since the person running the build is not necessarily the
person who read the ADRs.

## Consequences

- The default deployment detects Italian fiscal codes, VAT numbers, ID cards, driving
  licences and passports, and does **not** reliably detect Italian personal names. That
  gap is real and must be stated in the service README rather than discovered.
- Presidio becomes a reference implementation living in `services/redaction`, not a
  dependency of the gateway. The gateway depends on the contract.
- A conformance test suite for the contract is worth more than any single engine's
  tests, because it is what a third party runs against their own detector. It belongs
  next to the contract in `packages/shared-py`.
- Cost: an unfamiliar reader now has one more indirection to follow before reaching
  running code. Accepted — the alternative is that swapping the engine means editing
  the gateway.
- Still deliberately absent: chaining several detectors, per-group engine selection, and
  a detector that returns replacement text of its own. Each is a plausible next step and
  none is needed to make the seam real.
