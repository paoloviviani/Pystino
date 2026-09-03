"""OpenRouter: a router with its own billing unit."""

from __future__ import annotations

from decimal import Decimal
from typing import Any

from gateway.plugins.base import ProviderKind, ReportedCost
from gateway.plugins.generic import GenericOpenAIPlugin


class OpenRouterPlugin(GenericOpenAIPlugin):
    """OpenRouter: one API key, a model catalogue, a sub-provider chosen per request.

    A **router**, not a provider: the model does not determine what serves it,
    and the response body names the sub-provider that did (the ``provider``
    key — the reason ``_BODY_ENDPOINT_KEYS`` exists in the generic plugin,
    which this reads through unchanged).

    It also reports a charge, in **credits** — its own billing unit, deducted
    per request. The number is authoritative in exactly the sense
    ``ReportedCost.authoritative`` means: it is what the counterparty actually
    charged, so a deployment may bill pass-through from it. It is *not* a
    currency conversion of anything: the unit travels with the figure and is
    never converted, per the rule the ReportedCost docstring states.
    """

    name = "openrouter"
    label = "OpenRouter (router)"
    description = (
        "One key, many sub-providers: the response names which one served, and "
        "reports the charge in OpenRouter credits."
    )
    kind = ProviderKind.ROUTER
    reports_authoritative_cost = True
    default_base_url: str | None = "https://openrouter.ai/api/v1"

    def read_reported_cost(self, usage: dict[str, Any] | None) -> ReportedCost | None:
        if not usage:
            return None
        raw = usage.get("cost")
        # `True` is an int in Python and is not a cost.
        if raw is None or isinstance(raw, bool):
            return None
        try:
            # Via `str`, so a float never imports its binary rounding into a
            # Decimal that ends up in a money column.
            amount = Decimal(str(raw))
        except (ArithmeticError, TypeError, ValueError):
            return None
        if amount < 0:
            return None

        details = usage.get("cost_details")
        return ReportedCost(
            amount=amount,
            # Their billing unit, not a currency. Never converted: the
            # pass-through decision is "bill what they charged", and what they
            # charged is credits.
            currency="credits",
            authoritative=True,
            details=dict(details) if isinstance(details, dict) else {},
        )
