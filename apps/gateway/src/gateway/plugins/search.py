"""What makes a plugin a search backend — as little as possible.

The gateway's job on a search is a passthrough: authenticate, count, forward
the body verbatim, return the answer verbatim (ADR 0071). It does not
translate, so the translation vocabulary this module once carried — a common
query type, a normalised result type, per-vendor ``build_request`` and
``read_results`` — is gone. What a backend must still tell the gateway is the
minimum the passthrough cannot infer:

* ``search_path`` — where on the vendor the endpoint lives, appended to the
  provider row's base URL. On the plugin rather than in the route because
  where a counterparty puts an endpoint is the plugin's knowledge (ADR 0032),
  and these two already disagree: Linkup's version segment is in its base URL
  and Exa's is not.
* ``auth_headers`` — how the stored credential is presented. Linkup takes a
  bearer; Exa names an ``x-api-key`` header. Both accept the other, and each
  sends the one its own schema names first.

Everything else about each vendor — which depth words mean what, which
response fields hold titles — lives in the caller that assembles the request.
In this deployment that is the chat, which owns one adapter per backend and
reads the vendor's own response shapes.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Protocol, runtime_checkable


@runtime_checkable
class SearchPlugin(Protocol):
    """A counterparty plugin that can also run a web search.

    Structural, and checked with ``isinstance`` at the route: a provider row
    naming a plugin that is not one of these is refused with a message naming
    the plugin, rather than failing later with an ``AttributeError`` that reads
    like a gateway fault.

    Implementations also satisfy ``ProviderPlugin`` — registered in the same
    registry, resolved by the same ``providers.plugin`` column. What makes
    them a search backend is ``kind == ProviderKind.SEARCH`` plus the two
    members here.
    """

    #: The registry name, which is also what lands in
    #: ``usage_records.own_search_backend``.
    name: str
    #: The path appended to the provider row's base URL. See the module
    #: docstring for why it lives on the plugin.
    search_path: str

    def auth_headers(self, credential: str) -> Mapping[str, str]:
        """How the stored credential is presented to this vendor."""
        ...

    def builtin_catalogue(self) -> dict[str, Any] | None:
        """Absent for search backends: there is no model list to read.

        A tier was once published here so the catalogue importer could list
        the depths; the passthrough grants per backend instead, and the
        backend's anchor row is created with the provider. Returning ``None``
        keeps Discover honest about a vendor with no ``/models`` endpoint.
        """
        return None
