"""Linkup, a web-search backend.

Read at source on 2026-09-11 from the vendor's own OpenAPI document,
``https://api.linkup.so/v1/openapi.json`` — not from their prose
documentation, which disagrees with it (their quickstart shows ``curl -G``,
which would make it a GET with a query string, while the specification defines
``/v1/search`` as POST-only with a required JSON body). Ground rule 2: what is
implemented here is the schema, and where the two disagree the schema wins.

What that document says, and what this plugin therefore does:

* ``POST https://api.linkup.so/v1/search``, ``Authorization: Bearer`` — the
  only security scheme on the operation.
* Three required fields: ``q``, ``depth`` and ``outputType``. ``depth`` is an
  enum of exactly ``deep``, ``fast``, ``flash``, ``standard``; ``outputType``
  is ``searchResults``, ``sourcedAnswer`` or ``structured``.
* ``searchResults`` returns ``{"results": [...]}`` where a text hit is
  ``{"name", "url", "content", "favicon", "type": "text"}`` and an image hit is
  ``{"name", "url", "type": "image"}`` — note ``name``, not ``title``, and that
  an image hit carries no content at all.

Four things worth knowing before changing it.

**Only ``searchResults`` is used, and the other two output types are refused
rather than offered.** ``sourcedAnswer`` returns prose the vendor's own model
wrote and ``structured`` returns whatever a caller-supplied JSON schema asked
for; both are a second model's output arriving through a route that meters
searches, with no tokens counted and nothing in the ledger to say a generation
happened. If either is ever wanted it is a decision, not a parameter, so
``outputType`` is forced after ``extra`` is merged and cannot be overridden.

**Linkup reports no cost on a search response at all.** There is no ``cost``,
``credits`` or ``usage`` field in any of its four response schemas; the balance
lives behind a separate ``GET /v1/credits/balance`` returning a bare number
with no currency. So ``vendor_report`` is empty here, and that is a fact about
the vendor rather than a gap in this plugin.

**Full page text is refused, not silently dropped.** Linkup returns snippets;
its ``content`` field is an extract, and there is no option that makes it
return the page. Turning a result into text a model can read means fetching and
rendering the page, which is the headless browser in
``deploy/compose/docker-compose.playwright.yml`` and is not wired to this
route. A caller who asks for content therefore gets a 400 naming the reason —
returning a snippet labelled as content would be the worse answer, because
nothing downstream could tell the difference.

**The rate limit is unverified.** An earlier note in this project recorded "10
QPS org-wide". That number is *not* in the OpenAPI document: the specification
describes a 429 for "rate limit exceeded or insufficient credits" and defines
no rate-limit response header and no numeric limit anywhere. It may well be
true and it is not established here, so nothing in this code depends on it.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from gateway.plugins.base import ProviderKind
from gateway.plugins.generic import GenericOpenAIPlugin
from gateway.plugins.search import (
    SearchConfigurationError,
    SearchOutcome,
    SearchQuery,
    SearchResult,
    SearchUnreadableError,
    normalise_tier,
    tier_catalogue,
)

#: The enum from the vendor's schema, cheapest and fastest first. Their own
#: descriptions: `flash` ranks sources in a few hundred milliseconds, `fast` is
#: "the recommended default for most agentic applications", `standard` runs one
#: pass of agentic search, and `deep` runs several iterations. The order is the
#: order a console should offer them in, and it is also the order of increasing
#: spend — `deep` is ten times `flash`.
_DEPTHS: tuple[str, ...] = ("flash", "fast", "standard", "deep")


class LinkupSearchPlugin(GenericOpenAIPlugin):
    """Linkup's search API: snippets and ranked sources, billed per request."""

    name = "linkup"
    label = "Linkup (web search)"
    description = (
        "A web-search backend, not an inference endpoint. Searches are metered "
        "as a count of requests and are never priced: the models behind this "
        "provider are search depths, and which depth a caller may run is a "
        "grant rather than a request parameter."
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
    tiers = _DEPTHS
    #: The vendor declares no default for a required enum, so this is ours: the
    #: middle of the four rather than the dearest. A default that cost ten
    #: times the cheapest option would be a bill nobody chose.
    default_tier = "standard"

    def auth_headers(self, credential: str) -> Mapping[str, str]:
        """Bearer, which is the only scheme on the operation."""
        return {"authorization": f"Bearer {credential}"}

    def builtin_catalogue(self) -> dict[str, Any] | None:
        """The four depths, with the vendor's own descriptions of them."""
        return tier_catalogue(
            self,
            {
                "flash": "Lowest latency: ranked sources and snippets in a few "
                "hundred milliseconds.",
                "fast": "Higher-quality one-shot retrieval in around a second. "
                "Linkup's own recommended default.",
                "standard": "One pass of agentic search, for multi-topic and "
                "multi-source queries.",
                "deep": "Several search iterations, for coverage and multi-hop "
                "questions. Around ten times the cost of flash.",
            },
        )

    def build_request(self, query: SearchQuery) -> dict[str, Any]:
        tier = normalise_tier(query.tier, self)
        if tier not in self.tiers:
            raise SearchConfigurationError(
                f"Linkup has no search depth {tier!r}. It offers: "
                f"{', '.join(self.tiers)}."
            )
        if query.include_content:
            raise SearchConfigurationError(
                "Linkup returns snippets rather than page text, so this backend "
                "cannot satisfy 'include_content'. Use a backend that fetches "
                "pages, or drop the option and read the snippets."
            )

        # The caller's own options first, so the four fields below always win.
        # `extra` reaching `outputType` or `depth` would change the shape the
        # reader expects and the tier the reservation was made against.
        payload: dict[str, Any] = dict(query.extra)
        payload["q"] = query.query
        payload["depth"] = tier
        payload["outputType"] = "searchResults"
        if query.max_results is not None:
            payload["maxResults"] = query.max_results
        if query.include_domains:
            payload["includeDomains"] = list(query.include_domains)
        if query.exclude_domains:
            payload["excludeDomains"] = list(query.exclude_domains)
        return payload

    def read_results(
        self, payload: dict[str, Any] | None, *, query: SearchQuery
    ) -> SearchOutcome:
        if not isinstance(payload, dict) or not isinstance(payload.get("results"), list):
            raise SearchUnreadableError(
                "Linkup answered without a 'results' array, which is the one "
                "field its searchResults schema requires."
            )

        results: list[SearchResult] = []
        for entry in payload["results"]:
            if not isinstance(entry, dict):
                continue
            url = entry.get("url")
            if not isinstance(url, str) or not url:
                # A result with no URL is not a result. Skipped rather than
                # raising: one malformed entry should not lose the other nine.
                continue
            media = "image" if entry.get("type") == "image" else "text"
            content = entry.get("content")
            results.append(
                SearchResult(
                    url=url,
                    title=_text(entry.get("name")),
                    # Their `content` is an extract, so it is the snippet. It is
                    # deliberately *not* also copied into `content`: this
                    # backend cannot return a page, and a snippet in the field
                    # that means "the page" would be indistinguishable from one.
                    snippet=_text(content) if media == "text" else None,
                    media_type=media,
                )
            )

        return SearchOutcome(
            results=results,
            # One call, one search. Not `len(results)`: Linkup bills the
            # request, and a search that found nothing was still a search.
            searches=1,
            tier=normalise_tier(query.tier, self),
            # Empty, and not by omission — see the module docstring.
            vendor_report={},
        )


def _text(value: Any) -> str | None:
    """A non-empty string, or nothing. Empty strings read as absent."""
    return value if isinstance(value, str) and value else None
