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
from gateway.plugins.base import ProviderKind, ReportedCost, ServedBy, bearer_headers

#: Where a counterparty that reports the serving endpoint in the *body* puts it.
#: OpenRouter does this. Read here rather than in the recorder, which is where it
#: used to live.
_BODY_ENDPOINT_KEYS = ("provider", "served_by")


class GenericOpenAIPlugin:
    """An OpenAI-compatible provider with no vendor quirks worth naming."""

    name = "generic"
    label = "OpenAI-compatible"
    description = (
        "Forwards requests unchanged. Billed from the configured prices, never from the "
        "provider's own figure."
    )
    kind = ProviderKind.PROVIDER
    # It reports no cost at all, so there is nothing to assert. This is what
    # keeps pass-through billing unselectable for a nameless endpoint.
    reports_authoritative_cost = False
    # Endpoints answering to this type range from a cloud API to a laptop's
    # Ollama; there is no URL worth guessing.
    default_base_url: str | None = None
    # No documented endpoint pairs, so the console offers no endpoint choice:
    # the type's endpoint, if any, is `default_base_url` alone.
    base_url_options: tuple[tuple[str, str], ...] = ()

    # -- shaping a request --------------------------------------------------

    def auth_headers(self, credential: str) -> Mapping[str, str]:
        return bearer_headers(credential)

    def prepare_payload(self, payload: dict[str, Any], *, surface: ApiSurface) -> dict[str, Any]:
        """Ask for usage on a streamed chat completion, and change nothing else.

        Without this a streamed response carries no token counts at all and
        accounting records zero for every streaming request — so for an endpoint
        that says nothing unusual about itself, asking is the safe default. The
        counterparties that dislike being asked say so, and say it in their own
        plugin: this used to be ``providers.forward_stream_options``, defaulting
        true for exactly this reason (ADR 0028).

        Merged rather than replaced, so a stream option the client set survives.
        Only the chat surface: ``/v1/responses`` carries usage without being
        asked, and ``/v1/messages`` has no such parameter.
        """
        if surface is ApiSurface.CHAT_COMPLETIONS and payload.get("stream"):
            options = dict(payload.get("stream_options") or {})
            options["include_usage"] = True
            payload["stream_options"] = options
        return payload

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
        """Nothing. An endpoint with no name cannot have its numbers believed.

        Some OpenAI-compatible endpoints do put a figure in ``usage.cost``, and
        for a while an operator could declare its unit on the provider row.
        That was removed with ``providers.upstream_cost_unit``, because the
        declaration was the wrong shape for the knowledge: a number labelled
        ``cost`` is micro-EUR from one counterparty and credits from another, and
        nothing in the payload says which. Asking an operator to know is asking
        them to be the plugin.

        So this reports nothing, and reading such a figure is a plugin — twenty
        lines and a name, registered under ``llmp.providers``. That is a higher
        bar than typing a unit into a form, deliberately: a wrong unit here is a
        reconciliation report off by a factor of a million, which reads as a
        provider overcharging rather than as a configuration mistake.
        """
        return None

    def builtin_catalogue(self) -> dict[str, Any] | None:
        """A catalogue this plugin can answer from itself, if it has one.

        ``None`` for every counterparty on the internet: what it offers is a
        question only it can answer, and the gateway asks over HTTP. A plugin
        that *is* the thing being served knows the answer already, and asking
        the network would mean asking a service that has no such endpoint —
        which is what produced a 404 in the console the first time someone
        pressed Discover on the local extractor.

        The return value is a catalogue payload in the shape
        ``parse_catalogue`` reads, deliberately, so a built-in answer goes
        through exactly the same parsing, pricing and kind detection as a
        fetched one rather than a shortcut nobody tests.

        A narrow first step towards the ``catalogue()`` ADR 0032 describes —
        which is about replacing the import script — and not that feature.
        """
        return None

    def catalogue_tag_all(self) -> str | None:
        """No tag filter here, so no "everything" spelling is measured.

        Inherited by every OpenAI-compatible counterparty (Exa, Jina, Linkup,
        Mistral, Nebius, OpenAI, OpenRouter, Tensorix): one shared ``None``
        rather than nine identical copies, so a future tag-filtering
        counterparty overrides in exactly one place per plugin.
        """
        return None
