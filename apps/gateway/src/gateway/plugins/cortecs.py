"""Cortecs: the router reference implementation.

Everything here was **measured against the live API**, not read from
documentation — dates and evidence in
[docs/cache-accounting-findings.md](../../../../docs/cache-accounting-findings.md)
and ADR 0032. The point of
writing a plugin rather than more `if` statements is that each of these becomes a
declared behaviour with a test beside it.

It is a **router**: 53 of the 108 models it offers list more than one
sub-provider, and `gpt-oss-120b` lists eight. Which one serves a request is its
choice, made per request, and observed to be sticky rather than random.

Three findings drive the code:

* **The serving sub-provider is named only in a response header.**
  ``x-cortecs-provider``, with ``x-cortecs-model`` beside it. Not in the body,
  not in the stream frames. Before this plugin, ``upstream_provider`` was null
  for every request through it — the recorder looked for a body field, which is
  where a *different* router puts it.
* **Cost is reported in integer micro-EUR**, as ``usage.cost`` plus a
  ``cost_details`` breakdown, and is authoritative: it is what they charge, and
  a reconciliation against three sub-providers matched their listed prices to
  within their own rounding.
* **It rounds each cost component up to a whole micro-EUR.** 44.89 becomes 45,
  7.03 becomes 8. Bounded, always upward, and proportionally large on small
  requests — about 25% on a 40-token one. Recorded rather than corrected: it is
  their charge, and adjusting it would be inventing a number.

A fourth finding shapes the request rather than the reading: it **warns against
sending undocumented parameters**, and reports usage on a streamed response
whether or not it is asked to. So ``prepare_payload`` adds nothing.
"""

from __future__ import annotations

from collections.abc import Mapping
from decimal import Decimal
from typing import Any

from gateway.accounting.cost import TokenCounts
from gateway.models import ApiSurface
from gateway.plugins.base import ProviderKind, ReportedCost, ServedBy, bearer_headers

#: Micro-EUR. Derived, then checked four ways against catalogue rates — a
#: 1152-token prompt at 0.117/Mtok was reported as 135, and 134.78 is what the
#: arithmetic gives.
_COST_SCALE = Decimal("0.000001")
_COST_CURRENCY = "EUR"

#: The header carrying the sub-provider that actually served the request, and the
#: model it served. Lower-case; the lookup also tries title case because header
#: casing is not normalised everywhere.
_ENDPOINT_HEADER = "x-cortecs-provider"
_MODEL_HEADER = "x-cortecs-model"


def _header(headers: Mapping[str, str], name: str) -> str | None:
    value = headers.get(name) or headers.get(name.title()) or headers.get(name.upper())
    return str(value) if value else None


class CortecsRouterPlugin:
    """A router that quotes one price per model and chooses the endpoint itself."""

    name = "cortecs"
    label = "Cortecs (router)"
    description = (
        "Chooses a sub-provider per request and names it in a response header. Reports its "
        "own cost, so either that or the configured prices can be the charge."
    )
    kind = ProviderKind.ROUTER
    # Reconciled against their listed prices on three sub-providers,
    # agreeing to within their own rounding. It is what they charge.
    reports_authoritative_cost = True
    #: The counterparty's public endpoint. Where a provider row starts when this
    #: type is chosen, so configuring Cortecs is typing a name and a key.
    default_base_url: str | None = "https://api.cortecs.ai/v1"

    # -- shaping a request --------------------------------------------------

    def auth_headers(self, credential: str) -> Mapping[str, str]:
        """Bearer, on every route including ``/v1/messages``.

        Worth stating because the obvious guess is wrong: Anthropic's own API
        rejects a bearer token on that route, and a router serving Anthropic's
        shape does not inherit Anthropic's auth (ADR 0030).
        """
        return bearer_headers(credential)

    def prepare_payload(self, payload: dict[str, Any], *, surface: ApiSurface) -> dict[str, Any]:
        """Unchanged — deliberately, and this is a behaviour change from the column.

        Cortecs reports usage on the last chunk of a stream whether or not
        ``stream_options.include_usage`` is sent — checked against the live API —
        so sending it buys nothing. What it may cost is routing breadth: their
        documentation warns that undocumented parameters "can cause requests to
        fail or limit the providers able to process them". Whether it actually
        narrows the pool is **unverified**, because they do not name the serving
        sub-provider in stream frames, so ``scripts/check_cortecs_stream_options.py``
        reports that part as unknown.

        Not sending it is therefore the choice with no downside and an unquantified
        upside. Rows migrated from ``forward_stream_options = true`` stop sending
        it at upgrade, which migration 0010 says out loud rather than leaving to be
        noticed.
        """
        return payload

    # -- reading a response: facts only ------------------------------------

    def read_usage(self, usage: dict[str, Any] | None, *, surface: ApiSurface) -> TokenCounts:
        """Cortecs speaks each surface's own convention, so the shared readers apply.

        The cache-token spellings it passes through from its sub-providers —
        ``created_cache_tokens`` from vLLM backends, ``cache_write_tokens`` and
        ``cache_creation_tokens`` from Nebius, two of them in one response — are
        already handled in ``accounting/cost.py``, which is where the disjoint
        three-slice prompt model lives. Duplicating that here would be a second
        place to get it wrong.
        """
        if surface is ApiSurface.MESSAGES:
            return TokenCounts.from_anthropic_usage(usage)
        if surface in (ApiSurface.RESPONSES, ApiSurface.IMAGES):
            return TokenCounts.from_responses_usage(usage)
        return TokenCounts.from_usage(usage)

    def read_served_by(
        self, payload: dict[str, Any] | None, headers: Mapping[str, str]
    ) -> ServedBy | None:
        endpoint = _header(headers, _ENDPOINT_HEADER)
        served_model = _header(headers, _MODEL_HEADER)
        if endpoint is None and served_model is None:
            return None
        return ServedBy(endpoint=endpoint, model=served_model)

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
            amount=amount * _COST_SCALE,
            currency=_COST_CURRENCY,
            # It is what they charge: reconciled against their listed prices on
            # three different sub-providers, agreeing to within their rounding.
            authoritative=True,
            details=dict(details) if isinstance(details, dict) else {},
        )

    def builtin_catalogue(self) -> dict[str, Any] | None:
        """Nothing: ask the counterparty what it serves."""
        return None
