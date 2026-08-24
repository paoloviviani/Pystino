"""The OpenAI-compatible default.

What the gateway did before plugins existed, unchanged and now named. Any
endpoint that speaks the OpenAI shape and says nothing unusual about itself gets
this: a *provider*, because the model determines what serves it, reporting no
cost of its own.

It delegates to the readers in ``accounting/cost.py`` rather than reimplementing
them, so the seam exists without the arithmetic moving. Moving it would be a
second change wearing the same commit.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from gateway.accounting.cost import TokenCounts
from gateway.models import ApiSurface
from gateway.plugins.base import ProviderKind, ReportedCost, ServedBy

#: Where a counterparty that reports the serving endpoint in the *body* puts it.
#: OpenRouter does this. Read here rather than in the recorder, which is where it
#: used to live.
_BODY_ENDPOINT_KEYS = ("provider", "served_by")


class GenericOpenAIPlugin:
    """An OpenAI-compatible provider with no vendor quirks worth naming."""

    name = "generic"
    kind = ProviderKind.PROVIDER

    def read_usage(self, usage: dict[str, Any] | None, *, surface: ApiSurface) -> TokenCounts:
        # One reader per surface, because the surfaces disagree about where usage
        # lives *and* what its keys mean — see the module docstring of
        # accounting/cost.py for why these must not be merged.
        if surface is ApiSurface.MESSAGES:
            return TokenCounts.from_anthropic_usage(usage)
        if surface in (ApiSurface.RESPONSES, ApiSurface.IMAGES):
            return TokenCounts.from_responses_usage(usage)
        return TokenCounts.from_usage(usage)

    def read_served_by(
        self, payload: dict[str, Any] | None, headers: Mapping[str, str]
    ) -> ServedBy | None:
        if not payload:
            return None
        for key in _BODY_ENDPOINT_KEYS:
            if isinstance(value := payload.get(key), str) and value:
                return ServedBy(endpoint=value)
        return None

    def read_reported_cost(self, usage: dict[str, Any] | None) -> ReportedCost | None:
        """Nothing. A plain provider bills on its invoice, not in its responses.

        Deliberately not a tolerant search for anything called ``cost``: a
        number a vendor happens to label that way is not a charge, and treating
        it as one is how a figure gets billed from that should not have been.
        """
        return None
