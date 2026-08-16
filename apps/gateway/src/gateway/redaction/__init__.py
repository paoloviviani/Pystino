"""Pluggable redaction / guardrail layer."""

from __future__ import annotations

from gateway.config import RedactionSettings
from gateway.redaction.base import (
    RedactionOutcome,
    Redactor,
    TextRewriteStage,
    iter_choice_text,
)
from gateway.redaction.http import HttpDetectionRedactor, RedactionUnavailableError
from gateway.redaction.noop import NoOpRedactor
from gateway.redaction.registry import (
    ENTRY_POINT_GROUP,
    UnknownEngineError,
    available,
    register,
    resolve,
)

__all__ = [
    "ENTRY_POINT_GROUP",
    "HttpDetectionRedactor",
    "NoOpRedactor",
    "RedactionOutcome",
    "RedactionUnavailableError",
    "Redactor",
    "TextRewriteStage",
    "UnknownEngineError",
    "available_engines",
    "build_redactor",
    "iter_choice_text",
    "register_engine",
]

register("noop", lambda _settings: NoOpRedactor())
register("http", HttpDetectionRedactor)

# Re-exported under clearer names: inside this package "engine" is unambiguous,
# outside it "register" on its own is not.
register_engine = register
available_engines = available


def build_redactor(settings: RedactionSettings) -> Redactor:
    """Construct the configured engine.

    Built-ins are ``noop`` and ``http``; anything else comes from an installed
    package advertising the ``llmp.redactors`` entry point
    ([0026](../../../../docs/adr/0026-pluggable-detection.md)). An unknown name
    raises rather than falling back, because a gateway that believes redaction is
    on when it is not is the worst available outcome.
    """
    return resolve(settings.engine)(settings)
