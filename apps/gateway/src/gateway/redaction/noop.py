"""The redaction engine that ships in Phase 1: it does nothing, visibly.

Deliberately not "no redaction configured". It is a named engine that is recorded
on every usage row as ``redaction_engine='noop'``, so a row cannot be mistaken
later for one that was actually screened.
"""

from __future__ import annotations

from typing import Any

from gateway.config import EffectivePolicy
from gateway.models import ApiSurface
from gateway.redaction.base import RedactionOutcome, Redactor
from gateway.sse.pipeline import StreamStage, passthrough


class NoOpRedactor(Redactor):
    """Passes everything through unchanged.

    The response stage is the identity generator rather than a
    :class:`~gateway.redaction.base.TextRewriteStage` with an identity transform:
    there is no reason to pay for buffering and JSON round-tripping per frame
    when nothing will be rewritten.
    """

    name = "noop"

    async def redact_request(
        self, messages: list[dict[str, Any]], *, policy: EffectivePolicy | None = None
    ) -> RedactionOutcome:
        return RedactionOutcome(messages=messages, engine=self.name)

    def response_stage(
        self, outcome: RedactionOutcome, *, surface: ApiSurface = ApiSurface.CHAT_COMPLETIONS
    ) -> StreamStage:
        return passthrough

    async def redact_response_text(self, text: str, outcome: RedactionOutcome) -> str:
        return text
