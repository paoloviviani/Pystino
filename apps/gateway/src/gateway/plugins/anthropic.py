"""Anthropic's own API, which authenticates unlike everything else.

This plugin exists because a column was removed. ``providers.auth_scheme`` had
exactly two values, and its second one — ``x_api_key`` — was added when
Anthropic's own API turned out to reject a bearer token on ``/v1/messages``
(ADR 0030). Dropping the column without a home for that answer would have
removed the capability, so migration 0010 translates the value into a plugin
choice: a row with ``auth_scheme = 'x_api_key'`` becomes a row with
``plugin = 'anthropic'``, and nothing about how it authenticates changes.

Two things it is **not**, and both are deliberate:

* **Not a pricing integration.** ``read_reported_cost`` returns nothing, because
  Anthropic does not report a charge in its usage object — it reports tokens, and
  the money is ours to compute from the price rows. That is the same position the
  generic plugin is in, and it is the correct one rather than an unfinished one.
* **Not the direct-provider reference implementation.** A ``catalogue()`` that
  reads Anthropic's model list and its published prices is still to come, and
  when it does it belongs here. What is here now is the auth behaviour the column
  used to carry, no more.

The token conventions it needs are already right: ``/v1/messages`` reports
``input_tokens`` *excluding* cached tokens where OpenAI's ``prompt_tokens``
*includes* them, and ``cache_creation_input_tokens`` is one of the four spellings
of cache writes. Both live in ``accounting/cost.py``, dispatched by surface, so
this plugin delegates rather than reimplementing — see the warning in
``CLAUDE.md`` about not merging those two readers.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from gateway.accounting.cost import TokenCounts
from gateway.models import ApiSurface
from gateway.plugins.base import ProviderKind, ReportedCost, ServedBy

#: The version header Anthropic's API requires on every request. Pinned here
#: rather than sent from a setting, because a plugin knowing its counterparty's
#: protocol version is the whole idea — and because ``extra_headers`` on the
#: provider row still wins, so a deployment can move to a newer one without
#: waiting for a release. Not independently re-verified against their current
#: documentation in this change: it is the long-standing stable value, and it is
#: what the header was set to by hand before this plugin existed.
API_VERSION = "2023-06-01"


class AnthropicPlugin:
    """Anthropic's first-party API: ``x-api-key``, and tokens we price ourselves."""

    name = "anthropic"
    label = "Anthropic (direct)"
    description = (
        "Anthropic's own API: authenticates with x-api-key, not a bearer token. "
        "Billed from the configured prices."
    )
    # A provider, not a router: the model determines what serves it.
    kind = ProviderKind.PROVIDER
    # One endpoint, but not pre-filled: it is api.anthropic.com's own paths and
    # versioning, served by a different surface than the OpenAI-shaped one this
    # field configures. Typing it is a decision, not a recap.
    default_base_url: str | None = None
    # It reports tokens, not a charge. Nothing to assert, so pass-through
    # billing is not selectable for it.
    reports_authoritative_cost = False

    # -- shaping a request --------------------------------------------------

    def auth_headers(self, credential: str) -> Mapping[str, str]:
        """``x-api-key``, plus the required version header.

        The reason this is per counterparty rather than per route: a *router*
        serving Anthropic's shape at ``/v1/messages`` wants bearer there, and
        gets it, because it uses its own plugin.
        """
        return {"x-api-key": credential, "anthropic-version": API_VERSION}

    def prepare_payload(self, payload: dict[str, Any], *, surface: ApiSurface) -> dict[str, Any]:
        """Unchanged. ``/v1/messages`` streams usage without being asked.

        Anthropic's streaming protocol carries usage in ``message_start`` and
        ``message_delta`` frames as a matter of course, so there is no
        ``stream_options`` to add and nothing to negotiate.
        """
        return payload

    # -- reading a response: facts only ------------------------------------

    def read_usage(self, usage: dict[str, Any] | None, *, surface: ApiSurface) -> TokenCounts:
        """Dispatched by surface, because the surface decides the convention.

        A provider row pointing here would normally only serve ``/v1/messages``,
        but the dispatch is by surface rather than assumed: reading an OpenAI-shaped
        usage object with the Anthropic reader would treat ``prompt_tokens`` as
        excluding cached tokens and double-count every cache hit.
        """
        if surface is ApiSurface.MESSAGES:
            return TokenCounts.from_anthropic_usage(usage)
        if surface in (ApiSurface.RESPONSES, ApiSurface.IMAGES):
            return TokenCounts.from_responses_usage(usage)
        return TokenCounts.from_usage(usage)

    def read_served_by(
        self, payload: dict[str, Any] | None, headers: Mapping[str, str]
    ) -> ServedBy | None:
        """Nothing. For a provider the serving endpoint is the provider."""
        return None

    def read_reported_cost(self, usage: dict[str, Any] | None) -> ReportedCost | None:
        """Nothing. Anthropic reports tokens; the charge is ours to compute."""
        return None

    def builtin_catalogue(self) -> dict[str, Any] | None:
        """Nothing: ask the counterparty what it serves."""
        return None
