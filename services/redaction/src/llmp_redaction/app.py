"""The detection service.

Serves exactly one useful endpoint, ``POST /detect``, defined by
``llmp_shared.redaction`` — spans in, spans out, no placeholder text ever. Any
service that serves this contract can replace this one without the gateway
changing (ADR 0026).

It holds no state, has no database, and needs no authentication of its own: it is
reachable only on the internal compose network, and it is given text that has
already been authenticated at the gateway. Do not publish its port.
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI, Request
from llmp_shared import (
    DetectionRequest,
    DetectionResponse,
    ExtractionResponse,
    TextFindings,
)
from pydantic import BaseModel, Field

from llmp_redaction.detector import Detector
from llmp_redaction.documents import extract

logger = logging.getLogger(__name__)

DEFAULT_MODELS = {"en": "en_core_web_lg"}

# Which NLP engine Presidio runs behind the pattern recognisers.
#
# ``spacy`` (default) loads the language models baked into the image and is what
# makes PERSON / LOCATION / ORGANIZATION / NRP detectable. ``disabled`` loads no
# model at all: Presidio's own NoOpNlpEngine, which leaves every pattern and
# checksum recogniser (cards, IBANs, phones, fiscal codes, ...) working and the
# model-backed entities undetectable. A no-NER deployment uses roughly 150 MB
# instead of ~900, which is the entire point — but it must then say so, and
# ``capabilities()`` does: ``models`` comes back empty, every language is
# reported degraded, and the entity list the gateway shows the console shrinks
# to what can actually be found.
DEFAULT_NLP_ENGINE = "spacy"
DISABLED_NLP_ENGINE = "disabled"


def _nlp_engine_name() -> str:
    value = os.getenv("REDACTION_NLP_ENGINE", DEFAULT_NLP_ENGINE).strip().lower()
    if value not in {DEFAULT_NLP_ENGINE, DISABLED_NLP_ENGINE}:
        raise ValueError(
            f"REDACTION_NLP_ENGINE must be '{DEFAULT_NLP_ENGINE}' or '{DISABLED_NLP_ENGINE}', "
            f"not {value!r}"
        )
    return value


def _configured_models() -> dict[str, str]:
    """Language-to-spaCy-model map from ``REDACTION_SPACY_MODELS``.

    Format: ``en=en_core_web_lg,it=it_core_news_lg``. Set by the Dockerfile from
    the models it actually installed, so the service cannot claim a language whose
    weights are absent.

    An **empty or absent value means no models installed** — the no-NER build
    writes exactly that. Callers that need the historical "nothing configured,
    load the default" behaviour get it from :func:`build_detector`, which knows
    which NLP engine is running; this function reports the environment as it is.
    """
    raw = os.getenv("REDACTION_SPACY_MODELS", "").strip()
    models: dict[str, str] = {}
    for pair in raw.split(","):
        if "=" not in pair:
            continue
        language, _, model = pair.partition("=")
        models[language.strip()] = model.strip()
    return models


def _configured_languages() -> list[str]:
    """Languages to serve, with or without models.

    With NER the languages are the keys of ``REDACTION_SPACY_MODELS``. Without
    NER there are no models to key on, so ``REDACTION_LANGUAGES`` names them
    directly (``en,it``); a no-model build whose operator never set it still
    serves its patterns under ``en`` rather than nothing.
    """
    from_models = list(_configured_models())
    if from_models:
        return from_models
    raw = os.getenv("REDACTION_LANGUAGES", "").strip()
    if not raw:
        return ["en"]
    return [part.strip() for part in raw.split(",") if part.strip()]


# Locale-specific recognisers to register for *every* loaded language.
#
# Found against a running container, and the whole reason this list exists:
# Presidio registers a recogniser only if its own `supported_language` is one of
# the registry's languages. Load only `en` and it logs
#
#   Recognizer not added to registry because language is not supported by
#   registry - ItFiscalCodeRecognizer supported languages: it
#
# and every Italian identifier silently stops being detected — including in the
# MIT-only image, whose entire justification is that those recognisers need no
# model (ADR 0026). They are pattern and checksum
# matchers, so the language tag has no bearing on whether they match; registering
# them under the loaded language is what makes the claim true rather than merely
# plausible. Their context words stay Italian, which only affects score boosting.
EXTRA_PATTERN_RECOGNIZERS = (
    "ItFiscalCodeRecognizer",
    "ItVatCodeRecognizer",
    "ItDriverLicenseRecognizer",
    "ItIdentityCardRecognizer",
    "ItPassportRecognizer",
    "EsNifRecognizer",
    "EsNieRecognizer",
    "UkNinoRecognizer",
    "PlPeselRecognizer",
)


# Phone-number regions. Presidio's default set is ('US','GB','DE','FR','IL','IN',
# 'CA','BR') — no IT, so `+39 011 227 6543` was detected as a PERSON and left in
# the prompt. Found by sending one to a running container. Configurable because
# the right list is a property of the deployment, not of the software.
DEFAULT_PHONE_REGIONS = ("IT", "US", "GB", "DE", "FR", "ES", "CH", "AT")


def _phone_regions() -> tuple[str, ...]:
    raw = os.getenv("REDACTION_PHONE_REGIONS", "").strip()
    if not raw:
        return DEFAULT_PHONE_REGIONS
    regions = tuple(part.strip().upper() for part in raw.split(",") if part.strip())
    return regions or DEFAULT_PHONE_REGIONS


def build_detector() -> Detector:
    """Load Presidio. Slow (seconds) and done once, at startup."""
    from presidio_analyzer import AnalyzerEngine, RecognizerRegistry
    from presidio_analyzer import predefined_recognizers as recognizers
    from presidio_analyzer.nlp_engine import NlpEngineProvider

    nlp_engine_name = _nlp_engine_name()
    started = time.monotonic()

    if nlp_engine_name == DISABLED_NLP_ENGINE:
        # No model is loaded or needed: the languages stand alone, and every
        # entity the engine then reports is one a pattern can actually find.
        languages = _configured_languages()
        from presidio_analyzer.nlp_engine import NoOpNlpEngine

        # Presidio's no-op engine validates a model_name per language even
        # though it never loads one; the language code is an honest placeholder.
        nlp_engine = NoOpNlpEngine(
            models=[{"lang_code": lang, "model_name": lang} for lang in languages]
        )
        nlp_engine.load()
        models: dict[str, str] = {}
    else:
        models = _configured_models() or dict(DEFAULT_MODELS)
        languages = list(models)
        provider = NlpEngineProvider(
            nlp_configuration={
                "nlp_engine_name": "spacy",
                "models": [
                    {"lang_code": language, "model_name": model}
                    for language, model in models.items()
                ],
            }
        )
        nlp_engine = provider.create_engine()

    registry = RecognizerRegistry()
    # The NLP recogniser (SpacyRecognizer) is added by this call whenever it can
    # resolve one — Presidio raises outright if handed the no-op engine here —
    # so in no-NER mode it is loaded *without* an engine and the SpacyRecognizer
    # it appends anyway is removed below. Leaving it registered would put
    # PERSON/LOCATION/... in `get_supported_entities` while nothing could ever
    # find them: exactly the lie the capabilities report exists to prevent.
    registry.load_predefined_recognizers(languages=languages, nlp_engine=nlp_engine)
    if nlp_engine_name == DISABLED_NLP_ENGINE:
        from presidio_analyzer.predefined_recognizers import SpacyRecognizer

        for existing in list(registry.recognizers):
            if isinstance(existing, SpacyRecognizer):
                registry.remove_recognizer(type(existing).__name__)
    for name in EXTRA_PATTERN_RECOGNIZERS:
        recognizer_class = getattr(recognizers, name, None)
        if recognizer_class is None:
            # Presidio renamed or dropped it. A warning rather than a crash: a
            # detection service that will not start detects nothing at all, which
            # is strictly worse than one missing a national identifier.
            logger.warning("recogniser %s is not in this Presidio version; skipping", name)
            continue
        for language in languages:
            registry.add_recognizer(recognizer_class(supported_language=language))

    # Replaces the default phone recogniser rather than adding a second one, so a
    # number is not reported twice by two recognisers with different regions.
    regions = _phone_regions()
    # In no-NER mode Presidio cannot lemmatise, so its context enhancer never
    # fires and every phone match reports the bare class score 0.4 — below the
    # gateway's default threshold of 0.5. NER mode can lift the same match to
    # 0.75 (0.4 + the enhancer's 0.35 context factor) when a context word like
    # "phone" sits next to it; without the lemma table that lift is simply not
    # attainable. Restoring the attainable score keeps PHONE_NUMBER findable on
    # the terms NER mode would offer; whether a phone is redacted stays the
    # gateway policy's decision alone — spans are re-filtered per type there
    # (gateway/redaction/http.py), so a policy that wants phones kept still
    # keeps them.
    phone_recognizer_class = recognizers.PhoneRecognizer
    if nlp_engine_name == DISABLED_NLP_ENGINE:

        class NoNerPhoneRecognizer(phone_recognizer_class):  # type: ignore[misc, valid-type]
            SCORE = 0.4 + 0.35  # Presidio's base + context_similarity_factor

        phone_recognizer_class = NoNerPhoneRecognizer
    for language in languages:
        for existing in list(registry.recognizers):
            if type(existing).__name__ == "PhoneRecognizer" and (
                existing.supported_language == language
            ):
                registry.remove_recognizer(type(existing).__name__)
        registry.add_recognizer(
            phone_recognizer_class(supported_language=language, supported_regions=regions)
        )

    analyzer = AnalyzerEngine(
        nlp_engine=nlp_engine, registry=registry, supported_languages=languages
    )

    try:
        from importlib.metadata import version as package_version

        version = package_version("presidio-analyzer")
    except Exception:  # pragma: no cover - metadata is always present in the image
        # Reported to the gateway and recorded against redactions, so an unknown
        # version is worth a line in the log rather than a silent "unknown".
        logger.warning("could not read the presidio-analyzer version", exc_info=True)
        version = "unknown"

    logger.info(
        "presidio %s ready in %.1fs with NLP engine: %s; languages: %s; phone regions: %s",
        version,
        time.monotonic() - started,
        nlp_engine_name,
        ", ".join(languages) or "none",
        ", ".join(regions),
    )
    detector = Detector(
        analyzer,
        languages=languages,
        # Empty in no-NER mode, and *empty is the report*: `Detector.capabilities`
        # derives "degraded" from a language having no model, which is what
        # healthz surfaces and what keeps the gateway's entity list honest.
        models=models,
        engine="presidio",
        engine_version=version,
    )
    missing = detector.capabilities().degraded
    if missing:
        logger.warning(
            "no NER model for: %s. Identifier recognisers (fiscal code, VAT, IBAN, "
            "card, phone, email) still work for those languages; personal and place "
            "names will be missed. See services/redaction/README.md",
            ", ".join(missing),
        )
    return detector


class HealthResponse(BaseModel):
    status: str
    engine: str
    engine_version: str
    languages: list[str]
    models: dict[str, str]
    # Languages served without an NER model. Named `degraded_languages` rather
    # than omitted, because "we return fewer entities for Italian" is exactly the
    # thing an operator must be able to discover without reading the Dockerfile.
    degraded_languages: list[str]
    entities: list[str]
    # Installed entities partitioned by recognizer family, so a gateway can show
    # only the entity types its enabled families can actually detect.
    pattern_entities: list[str] = Field(default_factory=list)
    model_entities: list[str] = Field(default_factory=list)
    # False on detectors that predate family selection. The gateway must not
    # treat an unpartitioned entity list as narrowed support.
    family_partition: bool = False


def create_app(detector: Detector | None = None) -> FastAPI:
    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        # Loaded during startup rather than on the first request, so a container
        # that cannot load its model fails immediately and visibly instead of
        # timing out whoever happens to send the first prompt.
        app.state.detector = detector or build_detector()
        yield

    app = FastAPI(
        title="llmp detection service",
        version="0.1.0",
        lifespan=lifespan,
    )

    @app.get("/healthz", response_model=HealthResponse)
    async def healthz() -> HealthResponse:
        engine: Detector = app.state.detector
        capabilities = engine.capabilities()
        return HealthResponse(
            status="ok",
            engine=engine.engine,
            engine_version=engine.engine_version,
            languages=capabilities.languages,
            models=capabilities.models,
            degraded_languages=capabilities.degraded,
            entities=capabilities.entities,
            pattern_entities=capabilities.pattern_entities,
            model_entities=capabilities.model_entities,
            family_partition=True,
        )

    @app.post("/extract", response_model=ExtractionResponse)
    async def extract_document(request: Request) -> ExtractionResponse:
        """Text out of one document. The body *is* the document.

        Raw bytes with their own `Content-Type` rather than multipart: the
        payload is a single file, multipart would add a dependency and a parser
        to a service whose job is to be small, and 25 MB of base64 in JSON is
        33 MB of JSON. `X-Filename` is optional and only consulted when the
        content type is unrecognised — several clients send
        `application/octet-stream` for every upload.

        Always 200 with an outcome, never 4xx for an unreadable document: "we
        could not read this" is an answer the caller has to act on, and an HTTP
        error makes it indistinguishable from the service being broken. The one
        thing that must not happen is an empty string reaching a caller that
        reads it as "nothing sensitive in here", which is why the contract's
        `kind` carries the reason and `inspected` is the predicate to branch on.

        Runs in a worker thread for the same reason detection does: parsing a
        200-page PDF is CPU-bound, and doing it on the event loop stalls every
        other request in the process.
        """
        data = await request.body()
        started = time.monotonic()
        outcome = await asyncio.to_thread(
            extract,
            data,
            media_type=request.headers.get("content-type"),
            filename=request.headers.get("x-filename"),
        )
        logger.info(
            "extracted %d bytes: %s (%s) in %dms",
            len(data),
            outcome.kind.value,
            outcome.extractor or "none",
            int((time.monotonic() - started) * 1000),
        )
        return outcome

    @app.post("/detect", response_model=DetectionResponse)
    async def detect(request: DetectionRequest) -> DetectionResponse:
        engine: Detector = app.state.detector
        # Filter the request to what this engine can actually serve, and name
        # what was dropped. Presidio raises — "No matching recognizers were
        # found to serve the request" — when asked for an entity type no
        # recognizer supports, so a policy naming PERSON against a no-NER
        # build answered 500 and the gateway's fail-closed answered 502: the
        # whole redaction layer down because *one rule* does not fit the
        # engine. Detecting what it can and reporting the rest is the
        # fact-returning behaviour the contract asks for; the caller decides
        # whether a partial detection is acceptable (it is, and the gateway
        # logs it loudly every time the set changes).
        capabilities = engine.capabilities()
        effective = set(
            Detector.family_entities(
                capabilities,
                request.presidio_pattern_matching,
                request.presidio_ner,
            )
        )
        requested = request.entity_types
        unsupported = sorted({t.upper() for t in (requested or [])} - effective)
        if requested is None:
            # Preserve Presidio's historical all-recognizers call only when no
            # family is disabled. A narrowed request must name the family subset
            # explicitly, because an empty `entities` list raises.
            servable = (
                None
                if request.presidio_pattern_matching is not False
                and request.presidio_ner is not False
                else sorted(effective)
            )
        else:
            servable = [t for t in requested if t.upper() in effective]
        # An entirely unservable enumeration skips the engine: handing Presidio
        # an empty `entities` list raises the same way an unknown type does.
        if not effective or (requested is not None and not servable):
            found: list[list[Any]] = [[] for _ in request.texts]
        else:
            found = await engine.analyse(
                request.texts,
                language=request.language,
                score_threshold=request.score_threshold,
                entity_types=servable,
            )
        return DetectionResponse(
            findings=[TextFindings(index=index, spans=spans) for index, spans in enumerate(found)],
            engine=engine.engine,
            engine_version=engine.engine_version,
            unsupported_types=unsupported,
        )

    return app


def main() -> Any:  # pragma: no cover - the container entry point
    import uvicorn

    logging.basicConfig(level=os.getenv("REDACTION_LOG_LEVEL", "INFO"))
    return uvicorn.run(
        create_app(),
        host="0.0.0.0",  # noqa: S104 - internal network only; the port is not published
        port=int(os.getenv("REDACTION_PORT", "8080")),
    )
