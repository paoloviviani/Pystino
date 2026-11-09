"""Contracts shared between the gateway and its out-of-process services."""

from llmp_shared.documents import ExtractionKind, ExtractionResponse, PageImage
from llmp_shared.redaction import (
    MAX_CUSTOM_LABELS,
    PLACEHOLDER_RE,
    SAME_LENGTH_MIN,
    CustomLabel,
    DetectionRequest,
    DetectionResponse,
    EntitySpan,
    PlaceholderMap,
    Restored,
    TextEdit,
    TextFindings,
    normalise_entity,
    opaque_placeholder,
    placeholder_for,
    same_length_placeholder,
)

__all__ = [
    "MAX_CUSTOM_LABELS",
    "PLACEHOLDER_RE",
    "SAME_LENGTH_MIN",
    "CustomLabel",
    "DetectionRequest",
    "DetectionResponse",
    "EntitySpan",
    "ExtractionKind",
    "ExtractionResponse",
    "PageImage",
    "PlaceholderMap",
    "Restored",
    "TextEdit",
    "TextFindings",
    "normalise_entity",
    "opaque_placeholder",
    "placeholder_for",
    "same_length_placeholder",
]
