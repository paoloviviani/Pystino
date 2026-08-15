"""The detection service.

Serves exactly one useful endpoint, ``POST /detect``, defined by
``llmp_shared.redaction`` — spans in, spans out, no placeholder text ever. Any
service that serves this contract can replace this one without the gateway
changing (docs/adr/0026-pluggable-detection.md).

It holds no state, has no database, and needs no authentication of its own: it is
reachable only on the internal compose network, and it is given text that has
already been authenticated at the gateway. Do not publish its port.
"""

from __future__ import annotations

import logging
import os
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI
from llmp_shared import DetectionRequest, DetectionResponse, TextFindings
from pydantic import BaseModel

from llmp_redaction.detector import Detector

logger = logging.getLogger(__name__)

DEFAULT_MODELS = {"en": "en_core_web_lg"}


def _configured_models() -> dict[str, str]:
    """Language-to-spaCy-model map from ``REDACTION_SPACY_MODELS``.

    Format: ``en=en_core_web_lg,it=it_core_news_lg``. Set by the Dockerfile from
    the models it actually installed, so the service cannot claim a language whose
    weights are absent.
    """
    raw = os.getenv("REDACTION_SPACY_MODELS", "").strip()
    if not raw:
        return dict(DEFAULT_MODELS)

    models: dict[str, str] = {}
    for pair in raw.split(","):
        if "=" not in pair:
            continue
        language, _, model = pair.partition("=")
        models[language.strip()] = model.strip()
    return models or dict(DEFAULT_MODELS)


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
# model (docs/adr/0026-pluggable-detection.md). They are pattern and checksum
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

    models = _configured_models()
    started = time.monotonic()

    provider = NlpEngineProvider(
        nlp_configuration={
            "nlp_engine_name": "spacy",
            "models": [
                {"lang_code": language, "model_name": model} for language, model in models.items()
            ],
        }
    )
    nlp_engine = provider.create_engine()

    registry = RecognizerRegistry()
    registry.load_predefined_recognizers(languages=list(models), nlp_engine=nlp_engine)
    for name in EXTRA_PATTERN_RECOGNIZERS:
        recognizer_class = getattr(recognizers, name, None)
        if recognizer_class is None:
            # Presidio renamed or dropped it. A warning rather than a crash: a
            # detection service that will not start detects nothing at all, which
            # is strictly worse than one missing a national identifier.
            logger.warning("recogniser %s is not in this Presidio version; skipping", name)
            continue
        for language in models:
            registry.add_recognizer(recognizer_class(supported_language=language))

    # Replaces the default phone recogniser rather than adding a second one, so a
    # number is not reported twice by two recognisers with different regions.
    regions = _phone_regions()
    for language in models:
        for existing in list(registry.recognizers):
            if type(existing).__name__ == "PhoneRecognizer" and (
                existing.supported_language == language
            ):
                registry.remove_recognizer(type(existing).__name__)
        registry.add_recognizer(
            recognizers.PhoneRecognizer(supported_language=language, supported_regions=regions)
        )

    analyzer = AnalyzerEngine(
        nlp_engine=nlp_engine, registry=registry, supported_languages=list(models)
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
        "presidio %s ready in %.1fs with models: %s; phone regions: %s",
        version,
        time.monotonic() - started,
        ", ".join(f"{lang}={model}" for lang, model in models.items()) or "none",
        ", ".join(regions),
    )
    detector = Detector(
        analyzer,
        languages=list(models),
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
        )

    @app.post("/detect", response_model=DetectionResponse)
    async def detect(request: DetectionRequest) -> DetectionResponse:
        engine: Detector = app.state.detector
        found = await engine.analyse(
            request.texts,
            language=request.language,
            score_threshold=request.score_threshold,
            entity_types=request.entity_types,
        )
        return DetectionResponse(
            findings=[TextFindings(index=index, spans=spans) for index, spans in enumerate(found)],
            engine=engine.engine,
            engine_version=engine.engine_version,
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
