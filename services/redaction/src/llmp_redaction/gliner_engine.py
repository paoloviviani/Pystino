"""GLiNER as a second NER engine, picked per request.

GLiNER is a zero-shot span model: it is asked for *labels* (plain English phrases)
at inference, which is what lets an admin add an entity type such as "project code
name" without retraining anything. Two pieces live here:

* :class:`PerRequestGLiNERRecognizer`, Presidio's own ``GLiNERRecognizer`` taught to
  add the labels of the current request. The labels travel in a ``ContextVar`` set
  around one ``analyze`` call, so two requests never see each other's labels and the
  shared model object stays stateless.
* the label map: GLiNER label -> entity type, from ``REDACTION_GLINER_LABELS``.

The model is loaded once, at startup, only when the image has it (``REDACTION_GLINER_MODEL``
is set by the Dockerfile's ``GLINER_MODEL`` build argument).
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from contextvars import ContextVar
from typing import Any

# Labels of the request being analysed: GLiNER label -> entity type.
CUSTOM_LABELS: ContextVar[Mapping[str, str] | None] = ContextVar(
    "gliner_custom_labels", default=None
)

DEFAULT_GLINER_MODEL = "urchade/gliner_multi_pii-v1"

# GLiNER label (what the model is asked for) -> entity type (what the gateway
# sees). The label text matters to a zero-shot model, so it is plain English
# whatever the document language.
DEFAULT_GLINER_LABELS = {
    "person": "PERSON",
    "address": "LOCATION",
    "city": "LOCATION",
    "organization": "ORGANIZATION",
    "date of birth": "DATE_TIME",
    "health insurance number": "HEALTH_INSURANCE_ID",
    "medical condition": "MEDICAL_CONDITION",
    "medication": "MEDICATION",
}


def configured_labels() -> dict[str, str]:
    """``REDACTION_GLINER_LABELS``: ``label`` or ``label=ENTITY``, comma separated.

    A bare label maps to its upper-cased, underscored form (``medication`` ->
    ``MEDICATION``). Unset means :data:`DEFAULT_GLINER_LABELS`.
    """
    raw = os.getenv("REDACTION_GLINER_LABELS", "").strip()
    if not raw:
        return dict(DEFAULT_GLINER_LABELS)
    labels: dict[str, str] = {}
    for part in raw.split(","):
        label, _, entity = part.partition("=")
        label = label.strip()
        if not label:
            continue
        labels[label] = entity.strip().upper() or label.upper().replace(" ", "_")
    return labels or dict(DEFAULT_GLINER_LABELS)


def configured_model() -> str:
    """The GLiNER checkpoint baked into the image; empty when the image has none."""
    return os.getenv("REDACTION_GLINER_MODEL", "").strip()


def make_recognizer_class() -> type:
    """The recogniser class, built on first use so importing this module needs no Presidio."""
    from presidio_analyzer.predefined_recognizers import GLiNERRecognizer

    class PerRequestGLiNERRecognizer(GLiNERRecognizer):
        """Presidio's GLiNER recogniser plus the labels of the current request."""

        def __init__(self, *args: Any, shared_model: Any = None, **kwargs: Any) -> None:
            # Presidio loads the model inside its constructor, so a model shared
            # between languages has to be known before that call.
            self._shared_model = shared_model
            super().__init__(*args, **kwargs)

        def load(self) -> None:
            if self._shared_model is not None:
                self.gliner = self._shared_model
            else:
                super().load()

        def analyze(self, text: str, entities: list[str], nlp_artifacts: Any = None) -> list[Any]:
            extra = CUSTOM_LABELS.get()
            if not extra:
                return super().analyze(text, entities, nlp_artifacts)
            # Instance state is shared between requests, so the extra labels are
            # added on a shallow copy for this call only. Presidio hands the
            # recogniser the entities it was *selected* for: a custom entity is
            # not one of its supported entities, so it is let through here.
            clone = _with_labels(self, extra)
            wanted = list(entities or []) + [e for e in extra.values() if e not in (entities or [])]
            return GLiNERRecognizer.analyze(clone, text, wanted, nlp_artifacts)

    return PerRequestGLiNERRecognizer


def _with_labels(recognizer: Any, extra: Mapping[str, str]) -> Any:
    import copy

    clone = copy.copy(recognizer)
    mapping = dict(recognizer.model_to_presidio_entity_mapping)
    mapping.update(extra)
    clone.model_to_presidio_entity_mapping = mapping
    clone.gliner_labels = list(mapping)
    # `copy.copy` keeps the same loaded model: the weights are never duplicated.
    return clone


def build_recognizers(languages: list[str], model_name: str) -> list[Any]:
    """One recogniser per language, sharing a single loaded model.

    The checkpoint is multilingual, so loading it once per language would multiply
    a gigabyte of weights by the number of languages for nothing.
    """
    from presidio_analyzer.chunkers import CharacterBasedTextChunker

    recognizer_class = make_recognizer_class()
    mapping = configured_labels()
    threshold = float(os.getenv("REDACTION_GLINER_THRESHOLD", "0.5"))
    # Characters per chunk: Presidio's default (250) is many forward passes on a
    # long document, and the model reads about 384 words.
    chunk_size = int(os.getenv("REDACTION_GLINER_CHUNK_CHARS", "1200"))
    recognizers: list[Any] = []
    for language in languages:
        recognizer = recognizer_class(
            shared_model=recognizers[0].gliner if recognizers else None,
            supported_language=language,
            entity_mapping=mapping,
            model_name=model_name,
            threshold=threshold,
            map_location="cpu",
            text_chunker=CharacterBasedTextChunker(
                chunk_size=chunk_size, chunk_overlap=max(chunk_size // 6, 1)
            ),
        )
        recognizers.append(recognizer)
    return recognizers
