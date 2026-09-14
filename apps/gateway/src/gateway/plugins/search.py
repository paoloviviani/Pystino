"""What makes a plugin a search backend — the minimum plus one translation.

The gateway's job on a search used to be a passthrough only: authenticate,
count, forward the body verbatim, return the answer verbatim (ADR 0071). It
did not translate, so the translation vocabulary this module once carried — a
common query type, a normalised result type, per-vendor ``build_request`` and
``read_results`` — was gone. It is back, deliberately smaller, for the one
reason a passthrough cannot serve: a billing group's search policy
(``groups.search_model_id``) names *which* backend runs, and a backend the
gateway picks is one whose dialect the caller cannot be expected to speak.
``POST /v1/search`` therefore sends one shape and receives one shape, and
the body and answer translations plus the call's own headers are the whole
of what a backend contributes beyond the passthrough:

* ``build_search_body`` — the vendor's own request for a query and a count.
  Always the vendor's default depth: the unified route offers no tier, so the
  ledger records none, and a ceiling counted in requests bounds volume, not
  spend. A caller that needs a dearer tier names the backend on the
  passthrough, where the tier is the caller's own words.
* ``read_search_results`` — the vendor's own answer into title, URL and
  snippet, best-effort. Entries without a usable URL are dropped, not
  repaired: a result that cannot be fetched is not a result.

What a backend must still tell the gateway for the passthrough is unchanged:

* ``search_path`` — where on the vendor the endpoint lives, appended to the
  provider row's base URL. On the plugin rather than in the route because
  where a counterparty puts an endpoint is the plugin's knowledge (ADR 0032),
  and these two already disagree: Linkup's version segment is in its base URL
  and Exa's is not.
* ``auth_headers`` — how the stored credential is presented. Linkup takes a
  bearer; Exa names an ``x-api-key`` header. Both accept the other, and each
  sends the one its own schema names first.
* ``search_headers`` — what else the unified call carries besides the
  credential. Jina needs ``X-Respond-With: no-content`` there so its answer
  arrives as titles and URLs rather than whole pages; Linkup and Exa need
  nothing. It is separate from ``auth_headers`` because those travel on the
  passthrough too, and a header that reshapes the answer must never leak into
  a route that promises the vendor's answer verbatim.

Everything fancier — date ranges, domain filters, page text — stays on the
passthrough, where the vendor's own shape survives verbatim.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Protocol, TypedDict, runtime_checkable


class UnifiedSearchResult(TypedDict):
    """One result in the gateway's own shape: the intersection every backend
    can supply. ``snippet`` may be empty — a backend that returns links and
    titles and no text still searched — but ``url`` is always usable, because
    entries without one are dropped by the reader rather than repaired."""

    title: str
    url: str
    snippet: str


@runtime_checkable
class SearchPlugin(Protocol):
    """A counterparty plugin that can also run a web search.

    Structural, and checked with ``isinstance`` at the route: a provider row
    naming a plugin that is not one of these is refused with a message naming
    the plugin, rather than failing later with an ``AttributeError`` that reads
    like a gateway fault.

    Implementations also satisfy ``ProviderPlugin`` — registered in the same
    registry, resolved by the same ``providers.plugin`` column. What makes
    them a search backend is ``kind == ProviderKind.SEARCH`` plus the members
    here.
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

    def search_headers(self) -> Mapping[str, str]:
        """What else the unified search call carries besides the credential.

        Sent only by the unified route, never by the passthrough: a header
        here may reshape the vendor's answer, and a verbatim route must not
        narrow what the caller asked for.
        """
        ...

    def build_search_body(self, query: str, max_results: int) -> dict[str, Any]:
        """The vendor's own request for this query and count, at its default depth."""
        ...

    def read_search_results(self, payload: Any) -> list[UnifiedSearchResult]:
        """The vendor's own answer into ``title``/``url``/``snippet`` dicts."""
        ...

    def builtin_catalogue(self) -> dict[str, Any] | None:
        """Absent for search backends: there is no model list to read.

        A tier was once published here so the catalogue importer could list
        the depths; the passthrough grants per backend instead, and the
        backend's anchor row is created with the provider. Returning ``None``
        keeps Discover honest about a vendor with no ``/models`` endpoint.
        """
        return None
