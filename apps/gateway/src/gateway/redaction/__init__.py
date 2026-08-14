"""Pluggable redaction / guardrail layer."""

from __future__ import annotations

from gateway.config import RedactionSettings
from gateway.redaction.base import (
    RedactionOutcome,
    Redactor,
    TextRewriteStage,
    has_finish_reason,
    iter_choice_text,
    set_choice_text,
)
from gateway.redaction.noop import NoOpRedactor

__all__ = [
    "NoOpRedactor",
    "RedactionOutcome",
    "Redactor",
    "TextRewriteStage",
    "build_redactor",
    "has_finish_reason",
    "iter_choice_text",
    "set_choice_text",
]


def build_redactor(settings: RedactionSettings) -> Redactor:
    """Construct the configured engine.

    Adding Presidio in Phase 2 means adding one branch here and one module; the
    request path, the response path and the placeholder scheme do not change.
    """
    match settings.engine:
        case "noop":
            return NoOpRedactor()
        case "http":
            raise NotImplementedError(
                "the HTTP detection engine arrives in Phase 2 together with the "
                "Presidio service in services/redaction. Set "
                "GATEWAY_REDACTION__ENGINE=noop for now — it is refused rather "
                "than silently downgraded, so nobody can believe redaction is on "
                "when it is not."
            )
