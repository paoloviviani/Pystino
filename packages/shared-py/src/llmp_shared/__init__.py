"""Contracts shared between the gateway and its out-of-process services."""

from llmp_shared.redaction import (
    PLACEHOLDER_RE,
    DetectionRequest,
    DetectionResponse,
    EntitySpan,
    PlaceholderMap,
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
    "PlaceholderMap",
    "TextFindings",
    "normalise_entity",
    "opaque_placeholder",
    "placeholder_for",
]
