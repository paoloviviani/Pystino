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
    EngineInfo,
    UnknownEngineError,
    available,
    describe,
    register,
    resolve,
)

__all__ = [
    "ENTRY_POINT_GROUP",
    "EngineInfo",
    "HttpDetectionRedactor",
    "NoOpRedactor",
    "RedactionOutcome",
    "RedactionUnavailableError",
    "Redactor",
    "TextRewriteStage",
    "UnknownEngineError",
    "available_engines",
    "build_redactor",
    "describe_engines",
    "iter_choice_text",
    "register_engine",
]

register(
    "noop",
    lambda _settings: NoOpRedactor(),
    label="Off (noop)",
    # Still recorded on every request as the 'noop' engine, so a past request
    # cannot later be mistaken for one that was screened.
    description=("Nothing is removed: prompts reach the provider exactly as the caller sent them."),
    redacts=False,
)
register(
    "http",
    HttpDetectionRedactor,
    label="Presidio (detection service)",
    # The contract is the one in llmp_shared.redaction; the engine is swappable
    # behind it, which is what makes this a description of a mechanism rather
    # than of Presidio.
    description=(
        "Calls an out-of-process detection service: entities are replaced with "
        "deterministic placeholders before the request leaves, and swapped back in "
        "the response."
    ),
    needs_endpoint=True,
)

# Re-exported under clearer names: inside this package "engine" is unambiguous,
# outside it "register" on its own is not.
register_engine = register
available_engines = available
describe_engines = describe


def build_redactor(settings: RedactionSettings) -> Redactor:
    """Construct the configured engine.

    Built-ins are ``noop`` and ``http``; anything else comes from an installed
    package advertising the ``llmp.redactors`` entry point
    (ADR 0026). An unknown name
    raises rather than falling back, because a gateway that believes redaction is
    on when it is not is the worst available outcome.
    """
    return resolve(settings.engine)(settings)
