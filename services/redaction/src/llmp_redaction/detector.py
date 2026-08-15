"""Presidio, wrapped so the rest of the service does not know about it.

Two things this layer is responsible for, both of which are easy to get subtly
wrong and neither of which is Presidio's fault:

**Never block the event loop.** spaCy inference is CPU-bound and synchronous.
Calling it directly from an async handler stalls every other request in this
process — the same mistake the gateway avoids by calling out to here in the first
place, just moved one hop. Analysis runs in a worker thread.

**Say what you cannot do.** The default image has no Italian NER model, because
those weights are CC BY-NC-SA (see the README). Italian *identifiers* still work,
because they are pattern and checksum recognisers. ``capabilities()`` reports the
difference rather than leaving an operator to infer it from missing detections.
"""

from __future__ import annotations

import asyncio
import functools
import logging
from dataclasses import dataclass, field
from typing import Any, Protocol

from llmp_shared import EntitySpan

logger = logging.getLogger(__name__)

# Presidio recognisers that need no statistical model. Every one of these works
# in the MIT-only image; this list is what lets the service report honestly which
# entity types it can still find without the NER model for a language.
PATTERN_ONLY_ENTITIES = frozenset(
    {
        "CREDIT_CARD",
        "CRYPTO",
        "DATE_TIME",
        "EMAIL_ADDRESS",
        "IBAN_CODE",
        "IP_ADDRESS",
        "PHONE_NUMBER",
        "MEDICAL_LICENSE",
        "URL",
        "US_BANK_NUMBER",
        "US_DRIVER_LICENSE",
        "US_ITIN",
        "US_PASSPORT",
        "US_SSN",
        "UK_NHS",
        "IT_FISCAL_CODE",
        "IT_DRIVER_LICENSE",
        "IT_VAT_CODE",
        "IT_PASSPORT",
        "IT_IDENTITY_CARD",
        "ES_NIF",
        "PL_PESEL",
        "SG_NRIC_FIN",
        "AU_ABN",
        "AU_ACN",
        "AU_TFN",
        "AU_MEDICARE",
        "IN_PAN",
        "IN_AADHAAR",
    }
)

# Entity types that come from the NLP engine and therefore need a language model.
MODEL_BACKED_ENTITIES = frozenset({"PERSON", "LOCATION", "NRP", "ORGANIZATION"})


class Analyzer(Protocol):
    """The slice of Presidio's AnalyzerEngine this service uses.

    Narrowed to a protocol so the service is testable without loading a model,
    and so an operator swapping in a different analyzer has an explicit target.
    """

    def analyze(self, text: str, language: str, **kwargs: Any) -> list[Any]: ...

    def get_supported_entities(self, language: str | None = None) -> list[str]: ...


@dataclass(frozen=True, slots=True)
class Capabilities:
    """What this instance can actually detect, per language."""

    languages: list[str]
    models: dict[str, str] = field(default_factory=dict)
    entities: list[str] = field(default_factory=list)
    # Languages whose identifiers work but whose names will be missed.
    degraded: list[str] = field(default_factory=list)


class Detector:
    """Runs detection and converts Presidio's results into contract spans."""

    def __init__(
        self,
        analyzer: Analyzer,
        *,
        languages: list[str],
        models: dict[str, str] | None = None,
        engine: str = "presidio",
        engine_version: str = "unknown",
    ) -> None:
        self._analyzer = analyzer
        self._languages = languages
        self._models = models or {}
        self.engine = engine
        self.engine_version = engine_version

    def capabilities(self) -> Capabilities:
        """What this instance can detect, asked of the analyzer rather than assumed.

        Originally a hardcoded list, which was wrong in the way that matters:
        Presidio had silently dropped every Italian recogniser (its language did
        not match the registry's) while ``/healthz`` went on advertising
        ``IT_FISCAL_CODE``. A capability report that cannot be wrong about the
        registry is worth more than one that reads well.
        """
        try:
            entities = sorted(self._analyzer.get_supported_entities())
        except Exception:
            logger.warning("could not read supported entities from the analyzer", exc_info=True)
            entities = []
        degraded = [language for language in self._languages if language not in self._models]
        return Capabilities(
            languages=list(self._languages),
            models=dict(self._models),
            entities=entities,
            degraded=degraded,
        )

    def resolve_language(self, requested: str) -> str:
        """The language to analyse in.

        Falls back to the first configured language rather than failing: a caller
        asking for a language this deployment does not load should still get the
        pattern-based detections, which are largely language-independent. Logged,
        because silently analysing Italian text as English is the sort of thing
        that must be visible somewhere.
        """
        if requested in self._languages:
            return requested
        fallback = self._languages[0]
        logger.warning(
            "language %r is not loaded (have: %s); analysing as %r. Pattern-based "
            "entities are unaffected; names and places will be less reliable.",
            requested,
            ", ".join(self._languages),
            fallback,
        )
        return fallback

    def analyse_one(
        self,
        text: str,
        *,
        language: str,
        score_threshold: float,
        entity_types: list[str] | None,
    ) -> list[EntitySpan]:
        """Synchronous, single text. Called on a worker thread."""
        if not text:
            return []
        results = self._analyzer.analyze(
            text=text,
            language=language,
            entities=entity_types or None,
            score_threshold=score_threshold,
        )
        spans = [
            EntitySpan(
                start=result.start,
                end=result.end,
                entity_type=result.entity_type,
                # Presidio scores are already 0..1; clamped because the contract
                # says so and a recogniser returning 1.0000001 should not 422 a
                # whole batch.
                score=min(1.0, max(0.0, float(result.score))),
            )
            for result in results
        ]
        spans.sort(key=lambda span: (span.start, span.end))
        return spans

    async def analyse(
        self,
        texts: list[str],
        *,
        language: str,
        score_threshold: float,
        entity_types: list[str] | None,
    ) -> list[list[EntitySpan]]:
        """Analyse a batch off the event loop.

        One thread hop for the whole batch rather than one per text: the texts in
        a chat request are analysed back to back anyway, and N hops would add
        scheduling overhead to the slowest part of the request for no parallelism
        — the GIL is held by spaCy either way.
        """
        resolved = self.resolve_language(language)
        work = functools.partial(
            self._analyse_batch,
            texts,
            language=resolved,
            score_threshold=score_threshold,
            entity_types=entity_types,
        )
        return await asyncio.to_thread(work)

    def _analyse_batch(
        self,
        texts: list[str],
        *,
        language: str,
        score_threshold: float,
        entity_types: list[str] | None,
    ) -> list[list[EntitySpan]]:
        return [
            self.analyse_one(
                text,
                language=language,
                score_threshold=score_threshold,
                entity_types=entity_types,
            )
            for text in texts
        ]
