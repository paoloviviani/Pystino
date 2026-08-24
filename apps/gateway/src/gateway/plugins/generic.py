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
from decimal import Decimal
from typing import Any

from gateway.accounting.cost import UPSTREAM_COST_UNITS, TokenCounts
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
    # An operator declaring a unit is not the vendor asserting a charge.
    reports_authoritative_cost = False

    def __init__(self, reported_cost_unit: str | None = None) -> None:
        """*reported_cost_unit* is an operator's declaration, from the provider row.

        Some OpenAI-compatible endpoints put a number in ``usage.cost`` and
        nothing says what unit it is in, so an operator may declare one. That
        makes the figure *readable*; it does not make it **authoritative**.

        The distinction is the whole point of the flag: an operator saying "read
        this as micro-EUR" is not the vendor asserting "this is what we charged
        you". So a generic provider can never be put in `provider_reported`
        billing mode — that needs a plugin that knows the counterparty well
        enough to make the claim.
        """
        self._unit = reported_cost_unit

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
        """``usage.cost``, but only if an operator declared what unit it is in.

        Deliberately not a tolerant search for anything that looks like a
        charge: a number a vendor happens to label ``cost`` is not one, and
        treating it as such is how a figure gets billed from that should not
        have been. With no declared unit this reports nothing at all.
        """
        if not usage or not self._unit:
            return None
        scale = UPSTREAM_COST_UNITS.get(self._unit)
        if scale is None:
            return None

        raw = usage.get("cost")
        # `True` is an int in Python and is not a cost.
        if raw is None or isinstance(raw, bool):
            return None
        try:
            # Via `str`, so a float never imports its binary rounding into a
            # column that holds money.
            amount = Decimal(str(raw))
        except (ArithmeticError, TypeError, ValueError):
            return None
        if amount < 0:
            return None

        factor, currency = scale
        details = usage.get("cost_details")
        return ReportedCost(
            amount=amount * factor,
            currency=currency,
            # Never. See __init__: a declared unit is not a vendor's assertion.
            authoritative=False,
            details=dict(details) if isinstance(details, dict) else {},
        )
