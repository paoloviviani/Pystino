"""Contracts shared between the gateway and its out-of-process services."""

from llmp_shared.documents import ExtractionKind, ExtractionResponse, PageImage
from llmp_shared.redaction import (
    PLACEHOLDER_RE,
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
)

__all__ = [
    "PLACEHOLDER_RE",
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
]
