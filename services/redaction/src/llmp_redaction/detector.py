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
import os
import re
import time
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any, Literal, Protocol

from llmp_shared import CustomLabel, EntitySpan

from llmp_redaction.gliner_engine import CUSTOM_LABELS

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
MODEL_BACKED_ENTITIES = frozenset(
    {
        "PERSON",
        "LOCATION",
        "NRP",
        "ORGANIZATION",
        # Only produced by the GLiNER engine's default label map.
        "HEALTH_INSURANCE_ID",
        "MEDICAL_CONDITION",
        "MEDICATION",
    }
)
MODEL_BACKED_UPPER = frozenset(entity.upper() for entity in MODEL_BACKED_ENTITIES)


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
    # The installed entities partitioned by recognizer family. A model-backed
    # label absent from MODEL_BACKED_ENTITIES is treated as a pattern until it
    # is registered there: the alternative is to exclude an entity the analyzer
    # can actually find.
    pattern_entities: list[str] = field(default_factory=list)
    model_entities: list[str] = field(default_factory=list)
    # Languages whose identifiers work but whose names will be missed.
    degraded: list[str] = field(default_factory=list)


EngineName = Literal["spacy", "gliner"]

# What ``/healthz`` and the engine picker need to know about one NER engine.
# ``available`` means "can be asked for by name"; the default slot is always
# usable for requests that name no engine, so a no-model deployment keeps
# answering pattern-only requests exactly as before.


@dataclass(slots=True)
class EngineSlot:
    """One NER engine hosted by this process, with the analyzer that runs it."""

    name: EngineName
    analyzer: Analyzer
    languages: list[str]
    models: dict[str, str] = field(default_factory=dict)
    available: bool = True
    # Model-backed entity types this engine can return.
    ner_entities: list[str] = field(default_factory=list)
    custom_labels: bool = False
    ms_per_token: float | None = None


class EngineBusyError(Exception):
    """The engine's queue is full for longer than the configured wait (503 ``engine_busy``)."""

    def __init__(self, engine: str) -> None:
        super().__init__(f"the {engine} engine is busy")
        self.engine = engine


class EngineUnavailableError(Exception):
    """A request named a NER engine this deployment does not have."""

    def __init__(self, engine: str) -> None:
        super().__init__(f"NER engine {engine!r} is not available")
        self.engine = engine


_TOKEN = re.compile(r"\w+|[^\w\s]")

# Synthetic Italian text used once at startup to measure each engine's speed.
CALIBRATION_TEXT = (
    "Il paziente Mario Bianchi, nato a Torino il 12 marzo 1970, abita in via Roma 15 a Milano. "
    "Il codice fiscale è BNCMRA70C12L219X e la visita è fissata presso l'Ospedale San Giovanni. "
    "Prende metformina ogni mattina per il diabete e il medico Laura Verdi ha scritto a "
    "laura.verdi@example.com il giorno dopo."
)


def _ner_entities_of(capabilities: Capabilities) -> list[str]:
    return list(capabilities.model_entities)


def _is_model_recognizer(recognizer: Any) -> bool:
    """True for a recogniser backed by a statistical model (spaCy, GLiNER, transformers).

    Presidio keeps those under ``predefined_recognizers.ner`` (and the NLP-engine
    ``SpacyRecognizer``); everything else (pattern, checksum, phonenumbers) is
    deterministic and needs no model.
    """
    for klass in type(recognizer).__mro__:
        module = getattr(klass, "__module__", "")
        if ".ner." in module or ".nlp_engine." in module or klass.__name__ == "SpacyRecognizer":
            return True
    return False


def _partition_registry(recognizers: Any) -> tuple[list[str], list[str]]:
    """(pattern entities, model entities) read from the recognisers actually loaded.

    A recogniser that is not loaded advertises nothing. A model recogniser
    advertises everything it can produce, except the spaCy one, whose Presidio
    mapping also lists labels (ID, AGE, EMAIL, PHONE_NUMBER, DATE_TIME) that the
    stock models never emit: for it only the real NER vocabulary counts.
    """
    pattern: set[str] = set()
    model: set[str] = set()
    for recognizer in recognizers:
        if not getattr(recognizer, "is_loaded", True):
            continue
        supported = list(getattr(recognizer, "supported_entities", None) or [])
        if not _is_model_recognizer(recognizer):
            pattern.update(supported)
        elif any(
            klass.__name__ in ("SpacyRecognizer", "TransformersRecognizer")
            for klass in type(recognizer).__mro__
        ):
            model.update(e for e in supported if e.upper() in MODEL_BACKED_UPPER)
        else:
            model.update(supported)
    return sorted(pattern), sorted(model)


class Detector:
    """Runs detection and converts Presidio's results into contract spans.

    Hosts one or two NER engines. The constructor's ``analyzer`` is the spaCy one
    (or the no-model pattern-only one); ``gliner`` is an optional second slot.
    ``default_engine`` is what a request that names no engine gets.
    """

    def __init__(
        self,
        analyzer: Analyzer,
        *,
        languages: list[str],
        models: dict[str, str] | None = None,
        engine: str = "presidio",
        engine_version: str = "unknown",
        gliner: EngineSlot | None = None,
        default_engine: EngineName = "spacy",
    ) -> None:
        self._analyzer = analyzer
        self._languages = languages
        self._models = models or {}
        self.engine = engine
        self.engine_version = engine_version
        spacy_slot = EngineSlot(
            name="spacy",
            analyzer=analyzer,
            languages=list(languages) if self._models else [],
            models=self._models,
            available=bool(self._models),
        )
        spacy_slot.ner_entities = _ner_entities_of(self.capabilities_of(spacy_slot))
        self._slots: dict[str, EngineSlot] = {"spacy": spacy_slot}
        if gliner is not None:
            self._slots["gliner"] = gliner
        if default_engine not in self._slots:
            raise ValueError(f"default NER engine {default_engine!r} is not hosted by this process")
        self.default_engine: EngineName = default_engine
        # GLiNER runs one request at a time: it is memory-hungry and uses every
        # core for each forward pass, so two at once only make both slower.
        self._gliner_gate = asyncio.Semaphore(1)
        #: How long a request waits for the GLiNER slot before it is refused as busy.
        self.gliner_queue_seconds = float(os.getenv("REDACTION_GLINER_QUEUE_SECONDS", "30"))

    @property
    def slots(self) -> list[EngineSlot]:
        """Every engine slot, spaCy first, with gliner listed even when absent."""
        gliner = self._slots.get("gliner") or EngineSlot(
            name="gliner",
            analyzer=self._analyzer,
            languages=[],
            available=False,
            custom_labels=True,
        )
        return [self._slots["spacy"], gliner]

    def slot(self, name: str | None) -> EngineSlot:
        """The slot a request runs on. ``None`` is the default and is always served."""
        if name is None:
            return self._slots[self.default_engine]
        slot = self._slots.get(name)
        if slot is None or not slot.available:
            raise EngineUnavailableError(name)
        return slot

    def capabilities(self, engine: str | None = None) -> Capabilities:
        """What this instance can detect on the default (or the named) engine."""
        return self.capabilities_of(self.slot(engine))

    def capabilities_of(self, slot: EngineSlot) -> Capabilities:
        analyzer = slot.analyzer
        languages = self._languages if slot.name == "spacy" else slot.languages
        models = slot.models
        return self._capabilities(analyzer, languages, models)

    def _capabilities(
        self, analyzer: Analyzer, languages: list[str], models: dict[str, str]
    ) -> Capabilities:
        """What this instance can detect, asked of the analyzer rather than assumed.

        Originally a hardcoded list, which was wrong in the way that matters:
        Presidio had silently dropped every Italian recogniser (its language did
        not match the registry's) while ``/healthz`` went on advertising
        ``IT_FISCAL_CODE``. A capability report that cannot be wrong about the
        registry is worth more than one that reads well.
        """
        degraded = [language for language in languages if language not in models]
        registry = getattr(analyzer, "registry", None)
        recognizers = getattr(registry, "recognizers", None)
        if recognizers is not None:
            pattern_entities, model_entities = _partition_registry(recognizers)
            entities = sorted(set(pattern_entities) | set(model_entities))
        else:
            # An analyzer with no registry to inspect (a test double, or an
            # operator's own): fall back to the entity-name vocabulary.
            try:
                entities = sorted(analyzer.get_supported_entities())
            except Exception:
                logger.warning("could not read supported entities from the analyzer", exc_info=True)
                entities = []
            model_entities = sorted(e for e in entities if e.upper() in MODEL_BACKED_UPPER)
            pattern_entities = sorted(e for e in entities if e.upper() not in MODEL_BACKED_UPPER)
        return Capabilities(
            languages=list(languages),
            models=dict(models),
            entities=entities,
            pattern_entities=pattern_entities,
            model_entities=model_entities,
            degraded=degraded,
        )

    @staticmethod
    def family_entities(
        capabilities: Capabilities,
        pattern_matching: bool | None,
        ner: bool | None,
    ) -> list[str]:
        """Installed entity labels belonging to the enabled recognizer families."""
        patterns_enabled = pattern_matching is not False
        ner_enabled = ner is not False
        if patterns_enabled and ner_enabled:
            return list(capabilities.entities)
        selected: list[str] = []
        if patterns_enabled:
            selected.extend(capabilities.pattern_entities)
        if ner_enabled:
            selected.extend(capabilities.model_entities)
        return sorted(set(selected))

    def effective_entities(self, pattern_matching: bool | None, ner: bool | None) -> list[str]:
        """Installed entity labels after applying the requested family toggles."""
        return self.family_entities(self.capabilities(), pattern_matching, ner)

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
        engine: str | None = None,
        custom_labels: Mapping[str, str] | None = None,
    ) -> list[EntitySpan]:
        """Synchronous, single text. Called on a worker thread.

        ``custom_labels`` maps GLiNER label -> entity type and only means anything
        on the GLiNER engine; the caller has already dropped the ones the request
        did not ask for.
        """
        if not text:
            return []
        slot = self.slot(engine)
        extra = dict(custom_labels or {}) if slot.name == "gliner" else {}
        entities = entity_types or None
        if extra and entities is not None:
            # Presidio picks recognisers by entity, and a custom type is not one
            # of the GLiNER recogniser's supported entities: hand it the built-in
            # types the caller named (or any one of them when only custom types
            # were named) and filter to the request afterwards.
            custom_entities = set(extra.values())
            builtin = [t for t in entities if t.upper() not in custom_entities]
            entities = builtin or [next(iter(slot.ner_entities), "PERSON")]
        token = CUSTOM_LABELS.set(extra or None)
        try:
            results = slot.analyzer.analyze(
                text=text,
                language=language,
                entities=entities,
                score_threshold=score_threshold,
            )
        finally:
            CUSTOM_LABELS.reset(token)
        spans = [
            EntitySpan(
                start=result.start,
                end=result.end,
                entity_type=result.entity_type,
                # Presidio scores are already 0..1; clamped because the contract
                # says so and a recogniser returning 1.0000001 should not 422 a
                # whole batch.
                score=min(1.0, max(0.0, float(result.score))),
                engine=_origin(result, slot),
            )
            for result in results
            if not (extra and entity_types) or _wanted(result.entity_type, entity_types)
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
        engine: str | None = None,
        custom_labels: list[CustomLabel] | None = None,
    ) -> list[list[EntitySpan]]:
        """Analyse a batch off the event loop.

        One thread hop for the whole batch rather than one per text: the texts in
        a chat request are analysed back to back anyway, and N hops would add
        scheduling overhead to the slowest part of the request for no parallelism
        — the GIL is held by spaCy either way.
        """
        resolved = self.resolve_language(language)
        slot = self.slot(engine)
        work = functools.partial(
            self._analyse_batch,
            texts,
            language=resolved,
            score_threshold=score_threshold,
            entity_types=entity_types,
            engine=engine,
            custom_labels=self.label_map(custom_labels, entity_types),
        )
        if slot.name == "gliner":
            # Bounded: GLiNER runs one batch at a time, so a queue of long texts would
            # hold every caller until its client times out. A wait past the limit is a
            # clean "busy" the gateway treats as the engine being unavailable.
            try:
                await asyncio.wait_for(self._gliner_gate.acquire(), self.gliner_queue_seconds)
            except TimeoutError as error:
                raise EngineBusyError(slot.name) from error
            try:
                return await asyncio.to_thread(work)
            finally:
                self._gliner_gate.release()
        return await asyncio.to_thread(work)

    @staticmethod
    def label_map(
        custom_labels: list[CustomLabel] | None, entity_types: list[str] | None
    ) -> dict[str, str]:
        """GLiNER label -> upper-cased entity, for the labels this request wants.

        When the request enumerates entity types, a custom label whose entity is
        not among them is dropped: asking the model for it would cost inference
        time for spans that would be thrown away.
        """
        wanted = {t.upper() for t in entity_types} if entity_types is not None else None
        mapping: dict[str, str] = {}
        for custom in custom_labels or []:
            entity = custom.entity.upper()
            if wanted is None or entity in wanted:
                mapping[custom.label] = entity
        return mapping

    def _analyse_batch(
        self,
        texts: list[str],
        *,
        language: str,
        score_threshold: float,
        entity_types: list[str] | None,
        engine: str | None = None,
        custom_labels: Mapping[str, str] | None = None,
    ) -> list[list[EntitySpan]]:
        return [
            self.analyse_one(
                text,
                language=language,
                score_threshold=score_threshold,
                entity_types=entity_types,
                engine=engine,
                custom_labels=custom_labels,
            )
            for text in texts
        ]

    def calibrate(self) -> None:
        """Measure each available engine once, on a fixed synthetic text.

        Fills ``ms_per_token`` so ``/healthz`` reports what this box actually
        does rather than a number from a document. The first pass is a warm-up
        and is discarded. A failure leaves ``None``: a missing figure is honest, a
        crash at startup over a statistic is not.
        """
        tokens = len(_TOKEN.findall(CALIBRATION_TEXT))
        for slot in self.slots:
            if not slot.available or not self._languages:
                continue
            language = self.resolve_language("it" if "it" in slot.languages else slot.languages[0])
            try:
                for attempt in range(2):
                    started = time.perf_counter()
                    self.analyse_one(
                        CALIBRATION_TEXT,
                        language=language,
                        score_threshold=0.5,
                        entity_types=None,
                        engine=slot.name,
                    )
                    elapsed = time.perf_counter() - started
                    if attempt:
                        slot.ms_per_token = round(elapsed * 1000 / tokens, 3)
            except Exception:
                logger.warning("could not time the %s engine", slot.name, exc_info=True)


def _wanted(entity: str, requested: list[str]) -> bool:
    return entity.upper() in {t.upper() for t in requested}


def _origin(result: Any, slot: EngineSlot) -> Literal["pattern", "spacy", "gliner"]:
    """Which family produced a Presidio result: a pattern recogniser or the slot's NER."""
    metadata = getattr(result, "recognition_metadata", None) or {}
    name = str(metadata.get("recognizer_name", "")).lower()
    if "gliner" in name:
        return "gliner"
    if "spacy" in name:
        return "spacy"
    if name:
        return "pattern"
    # No metadata (a test double): fall back on the entity's kind.
    entity = str(result.entity_type).upper()
    model_backed = entity in MODEL_BACKED_UPPER or entity in {e.upper() for e in slot.ner_entities}
    if model_backed and entity not in PATTERN_ONLY_ENTITIES:
        return slot.name
    return "pattern"
