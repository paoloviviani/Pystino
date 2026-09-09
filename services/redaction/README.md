# Redaction / PII detection service

Detects PII and returns **spans**. It does not redact, and it never invents
placeholder text — the gateway substitutes, using the keyed-HMAC derivation in
`packages/shared-py`. That split is what makes the engine replaceable: swapping this
service cannot change a placeholder already shown to a user or written into a
transcript.

## The contract

```
POST /detect
  { "texts": [...], "language": "en", "score_threshold": 0.5, "entity_types": null }
  -> { "findings": [ { "index": 0, "spans": [ {start, end, entity_type, score} ] } ],
       "engine": "presidio", "engine_version": "2.2.363" }

GET /healthz
  -> { "status", "engine", "engine_version", "languages", "models",
       "degraded_languages", "entities" }
```

Both sides have Pydantic models for this in `llmp_shared.redaction`.

**This service is one implementation, not the interface.** Anything that serves
`POST /detect` is a valid detector, in any language, with any model; point
`GATEWAY_REDACTION__ENDPOINT` at it and the gateway needs no code. See
ADR 0026.

## Plugging in a different detector

Three ways in, cheapest first. Nothing about the gateway changes in any of them.

**1. Serve the contract yourself.** Anything answering `POST /detect` with the shape
above is a detector — a transformer NER model, a rules engine, a commercial API you
front with fifty lines of FastAPI. Point the gateway at it:

```bash
GATEWAY_REDACTION__ENGINE=http
GATEWAY_REDACTION__ENDPOINT=http://my-detector:8080
```

This is the recommended route, and it is the one this service takes.

**2. Extend this service.** Presidio's registry accepts custom recognisers; the two
places to look are `EXTRA_PATTERN_RECOGNIZERS` and the phone-region block in
`app.build_detector`. A recogniser is a regex, an optional checksum, and some context
words — an internal project code or a staff-number format is an afternoon.

**3. Write a gateway plugin.** For a detector that genuinely must run in-process — a
pure-regex ruleset, where a network hop is absurd. Publish a package with:

```toml
[project.entry-points."llmp.redactors"]
my-engine = "my_package:build_my_redactor"
```

The factory takes `RedactionSettings` and returns anything satisfying
`gateway.redaction.Redactor`. `pip install` it, set
`GATEWAY_REDACTION__ENGINE=my-engine`, done. Subclass
`gateway.redaction.TextRewriteStage` for the response path rather than writing your
own: it already handles the hard part, an entity split across SSE frames.

### What the gateway keeps regardless

Deliberately **not** delegated to the detector, so no plugin can get them wrong:

* **Placeholder derivation.** Keyed HMAC, in the gateway. A detector never sees the
  key and cannot affect placeholder stability across turns.
* **Substitution.** Right-to-left over the spans, with overlaps resolved by score
  then length — two recognisers claiming the same characters is normal and nesting
  one replacement inside another corrupts the text.
* **Restoration**, buffering across frame boundaries, fail-closed behaviour, and the
  per-process detection cache.

A detector returns spans. That is the whole of its job, and it is why swapping one
cannot change a placeholder already shown to a user.

## Licensing, and what the default image cannot do

| Component | Licence |
|---|---|
| Presidio | MIT |
| `en_core_web_lg` | MIT |
| `it_core_news_lg` / `_sm` | **CC BY-NC-SA 3.0** — non-commercial, share-alike |

**The default image is MIT throughout and has no Italian NER model.** What that
does and does not cost:

* Still detected in Italian text: `IT_FISCAL_CODE`, `IT_VAT_CODE`,
  `IT_DRIVER_LICENSE`, `IT_IDENTITY_CARD`, `IT_PASSPORT`, plus IBAN, credit card,
  email, phone, URL, IP. All of these are pattern, context and checksum
  recognisers and need no model at all.
* **Not reliably detected: Italian personal names and place names.** The English
  model will find some and miss many.

`GET /healthz` reports this as `degraded_languages`, and lists the entities the
registry actually holds — read from the analyzer, not from a constant, because the
first version of this service advertised `IT_FISCAL_CODE` while Presidio had
silently dropped every Italian recogniser at startup.

That drop is worth knowing about if you add recognisers of your own: Presidio
registers one only when its `supported_language` is a language of the registry, so
a recogniser tagged `it` disappears in an English-only deployment. Pattern
recognisers do not care about language, so `build_detector` re-registers them under
each loaded language. Two other defaults needed adjusting for a European
deployment, both live-tested and both configurable:

* `REDACTION_PHONE_REGIONS` — Presidio's default phone regions are
  `US,GB,DE,FR,IL,IN,CA,BR`, so `+39 011 227 6543` was returned as a `PERSON`.
  Default here: `IT,US,GB,DE,FR,ES,CH,AT`.
* Phone numbers score ~0.4, below the gateway's default `score_threshold` of 0.5.
  Lower `GATEWAY_REDACTION__SCORE_THRESHOLD` if phone numbers matter more than
  false positives do.

To accept the non-commercial obligation and add the model:

```bash
docker build \
  --build-arg SPACY_MODELS="en_core_web_lg it_core_news_lg" \
  -f services/redaction/Dockerfile .
```

The build prints the licence notice when it installs an Italian model. Do not do
this for a deployment that supports commissioned or contract work without checking
with whoever owns that question.

## Why this is a separate process, and must stay one

spaCy is CPU-bound and synchronous. In-process it would block the gateway's event
loop for hundreds of milliseconds and stall *every concurrent stream on that
worker*. Inside this service the same rule applies one level down: analysis runs on
a worker thread, never on the request loop. See
ADR 0012.

Latency is the honest risk. The out-of-process design protects the event loop; it
does not make inference fast. The gateway caches detections per process on a hash of
the message text, which matters because a chat client resends the whole history
every turn — without it, inference is quadratic in conversation length.

It is also why this project is **not** a member of the root uv workspace: making it
one would put spaCy in the gateway's lockfile and make "the gateway depends on
Presidio" true in the one place it must not be.

## Running

```bash
# With the stack
docker compose --env-file deploy/.env \
  -f deploy/compose/docker-compose.yml \
  -f deploy/compose/docker-compose.smoke.yml \
  -f deploy/compose/docker-compose.redaction.yml up --build

./scripts/test_redaction_live.py

# Tests, which need neither Presidio nor a model
cd services/redaction && uv run --extra dev pytest
```

## Security posture

No authentication, and no `ports:` in the compose overlay. It is reachable only on
the internal network and receives text the gateway has already authenticated.
Publishing its port would make that assumption false — it would be an unauthenticated
endpoint that accepts arbitrary text.

## Presidio has moved, and why this is not the prebuilt image

No longer a Microsoft project: community-governed under `data-privacy-stack` since
2026, still MIT.

**Check which registry you are looking at.** The old
`mcr.microsoft.com/presidio-analyzer:latest` tag still resolves and no longer
tracks releases — checked 2026-08-25: that image was built 2026-03-15 and its
tags stop at 2.2.362, so pointing at it silently pins a detector five months
behind. The project's own registry is current:
`ghcr.io/data-privacy-stack/presidio-analyzer` carries 2.2.363 and 2.2.364, and
its `latest` was built the same day PyPI got 2.2.364.

So staleness is not the reason this service exists. The reasons are:

1. **The gateway speaks our contract, not a vendor's.** `POST /detect` returns
   spans in `llmp_shared.redaction`'s schema. Pointing the gateway at Presidio's
   `/analyze` would put one engine's wire format in the gateway and make every
   future detector imitate it — exactly what
   ADR 0026 refuses. Presidio is *a*
   detector we ship, not *the* detector the design assumes.
2. **The licence gate is a build decision.** The stock image ships English only,
   and `it_core_news_lg` is CC BY-NC-SA 3.0 — so an Italian-capable deployment
   needs a built image whatever else is true. Ours makes that a build arg that
   prints the obligation, and bakes what was actually installed into
   `models.env`, so `/healthz` cannot claim a language whose weights are absent.
3. **The registry needed correcting, and the corrections have to be reported.**
   The Italian pattern recognisers dropped under an English-only registry, and
   Presidio's default phone regions have no IT. Both fixes are at least partly
   expressible through the image's `RECOGNIZER_REGISTRY_CONF_FILE`; what is not
   is `degraded_languages` and the entity list on `/healthz`, read from the live
   analyzer — which exists precisely because the first version of this service
   advertised `IT_FISCAL_CODE` while Presidio had silently registered nothing.

What that leaves as the honest alternative: the stock image plus a mounted
recogniser conf plus a small adapter translating `/detect` to `/analyze` — two
containers and an adapter rather than one container, with the health reporting
still to write in the adapter. Batching is no longer a difference: `/analyze`
takes a list of texts as of 2.2.36x, as this contract always has.
