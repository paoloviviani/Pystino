"""Linkup, a web-search backend.

Read at source on 2026-09-11 from the vendor's own OpenAPI document,
``https://api.linkup.so/v1/openapi.json`` — not from their prose
documentation, which disagrees with it (their quickstart shows ``curl -G``,
which would make it a GET with a query string, while the specification defines
``/v1/search`` as POST-only with a required JSON body). Ground rule 2: what is
implemented here is the schema, and where the two disagree the schema wins.

Under the passthrough design (ADR 0071) this plugin is configuration, not
translation: it names the endpoint path and how the stored credential is
presented. Everything else about Linkup — the ``q``/``depth``/``outputType``
request fields, the ``results[].name`` shape, the fact that ``outputType`` is
forced to ``searchResults`` because their other two modes are a second model's
output arriving through a route that meters searches — is the caller's
business, and this deployment's caller is the chat, which owns the adapter.
Two facts from the schema are worth carrying in the adapter, and are recorded
in its docstring there rather than lost:

* ``depth`` is an enum of exactly ``deep``, ``fast``, ``flash``, ``standard``;
* ``outputType`` must be ``searchResults`` — ``sourcedAnswer`` and
  ``structured`` are a second model's output through a route that meters
  searches, with no tokens counted and nothing in the ledger to say a
  generation happened.

Linkup reports no cost on a search response at all — no ``cost``, ``credits``
or ``usage`` field in any of its four response schemas; the balance lives
behind a separate ``GET /v1/credits/balance`` returning a bare number with no
currency. Nothing here depends on it, and nothing should.
"""

from __future__ import annotations

from collections.abc import Mapping

from gateway.plugins.base import ProviderKind
from gateway.plugins.generic import GenericOpenAIPlugin


class LinkupSearchPlugin(GenericOpenAIPlugin):
    """Linkup's search API: snippets and ranked sources, billed per request."""

    name = "linkup"
    label = "Linkup (web search)"
    description = (
        "A web-search backend, not an inference endpoint. Searches are metered "
        "as a count of requests and are never priced: the caller speaks the "
        "vendor's own API through the gateway's passthrough."
    )
    kind = ProviderKind.SEARCH
    #: The server the OpenAPI document declares, plus the version segment the
    #: path carries. `search_path` is appended to it.
    default_base_url: str | None = "https://api.linkup.so/v1"
    #: Nothing here reports a cost, so there is nothing to assert and
    #: pass-through billing stays unselectable. That is doubly true for a
    #: search backend: a search has no price row to pass through *to*.
    reports_authoritative_cost = False

    search_path = "/search"

    def auth_headers(self, credential: str) -> Mapping[str, str]:
        """Bearer, which is the only security scheme on the operation."""
        return {"authorization": f"Bearer {credential}"}
