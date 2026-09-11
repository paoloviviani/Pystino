"""Exa, a web-search backend.

Read at source on 2026-09-11 from ``https://api.exa.ai/openapi.json``
(``info.version`` 2.0.0). That matters more here than usual, because a
*different* copy of that document — the one a documentation-rendering fetch
follows — still serves the old three-value search-type enum
``neural``/``keyword``/``auto``. Implementing against that would produce a 400
on every request at the dearer tiers and, worse, a ledger label
(``own_search_tier``) naming a mode that no longer exists. The live document is
authoritative and this plugin follows it.

What it says:

* ``POST https://api.exa.ai/search``. Two accepted schemes; ``x-api-key`` is
  the primary one and is what this sends.
* ``query`` is the only required field. ``type`` is an enum of ``instant``,
  ``fast``, ``auto``, ``deep-lite``, ``deep``, ``deep-reasoning``, defaulting
  to ``auto``. ``numResults`` is 1 to 100, default 10.
* A result carries ``title``, ``url``, ``publishedDate``, ``author``, ``id``,
  ``image``, ``favicon``, ``text``, ``highlights``, ``highlightScores``,
  ``summary``, ``subpages``, ``entities``, ``extras`` — and **no per-result
  score**.
* The response carries ``costDollars``: ``{"total", "search": {"neural",
  "keyword"}, "summary", "contents": {"text", "highlights", "summary"}}``.

Three things worth knowing.

**``costDollars`` is read and never billed from.** The decision for this whole
feature is to count requests rather than meter money, so this figure exists
here only to be *logged* — the route writes one line naming the amount so an
operator reconciling an Exa invoice has something to compare against, and
nothing writes it to ``upstream_cost``. Exa's own schema is unusually clear
that this would be wrong anyway: the field's description says "Estimated total
dollar cost… This response value is not an invoice record", and the parent
object's says "Billing is computed from usage counters rather than this
response object". Two independent reasons for the same restraint.

**Without ``contents``, Exa returns links and titles and no text whatsoever.**
It is a separate charge on the same call, which is why ``include_content`` is
off by default — and why a default search through this backend returns results
with a null snippet. That is the vendor's shape, not a bug, and it is the
reason ``include_content`` exists as a request field rather than being assumed.

**``resolvedSearchType`` is not read.** It is marked deprecated in the schema,
whose own note says current responses may return an empty string and that
clients should not branch on it. ``own_search_tier`` therefore records what we
*asked* for, which is also the only version of it that exists before the call.
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

#: The live enum, cheapest first where the vendor's own pricing orders them —
#: `instant` at $7 per 1,000 against `deep-reasoning` at $15 is the measurement
#: docs/web-search-plan.md records, and is why a request-count ceiling bounds
#: volume rather than spend.
_TYPES: tuple[str, ...] = (
    "instant",
    "fast",
    "auto",
    "deep-lite",
    "deep",
    "deep-reasoning",
)

#: Exa's own maximum for `numResults` on a public key. Bounded here as well as
#: in the request schema because the two limits mean different things: the
#: schema stops a typo, and this stops a `backend_options` entry sneaking past
#: it — over ten results is charged per result.
_MAX_RESULTS = 100


class ExaSearchPlugin(GenericOpenAIPlugin):
    """Exa's search API: ranked pages, with text as a separate charge."""

    name = "exa"
    label = "Exa (web search)"
    description = (
        "A web-search backend, not an inference endpoint. Searches are metered "
        "as a count of requests and are never priced: the models behind this "
        "provider are search modes, and which mode a caller may run is a grant "
        "rather than a request parameter."
    )
    kind = ProviderKind.SEARCH
    default_base_url: str | None = "https://api.exa.ai"
    reports_authoritative_cost = False

    search_path = "/search"
    tiers = _TYPES
    #: The vendor's own declared default, and their description of it —
    #: "balanced… recommended for most applications".
    default_tier = "auto"

    def auth_headers(self, credential: str) -> Mapping[str, str]:
        """``x-api-key``. Exa also accepts a bearer token; this sends the
        header its schema names first, so a key that works with their own
        examples works here."""
        return {"x-api-key": credential}

    def builtin_catalogue(self) -> dict[str, Any] | None:
        """The six search modes, with the vendor's own descriptions of them."""
        return tier_catalogue(
            self,
            {
                "instant": "Minimum response time, for real-time use.",
                "fast": "High quality with reduced latency, for interactive "
                "workflows.",
                "auto": "Balanced quality and speed. Exa's own default.",
                "deep-lite": "Lightweight research with synthesis, at roughly "
                "four seconds.",
                "deep": "Comprehensive multi-step research with synthesis.",
                "deep-reasoning": "Deep research with stronger reasoning, for "
                "complex analysis. Around twice the cost of instant.",
            },
        )

    def build_request(self, query: SearchQuery) -> dict[str, Any]:
        tier = normalise_tier(query.tier, self)
        if tier not in self.tiers:
            raise SearchConfigurationError(
                f"Exa has no search type {tier!r}. It offers: {', '.join(self.tiers)}."
            )

        payload: dict[str, Any] = dict(query.extra)
        payload["query"] = query.query
        payload["type"] = tier
        if query.max_results is not None:
            payload["numResults"] = min(query.max_results, _MAX_RESULTS)
        if query.include_domains:
            payload["includeDomains"] = list(query.include_domains)
        if query.exclude_domains:
            payload["excludeDomains"] = list(query.exclude_domains)
        if query.include_content:
            # Merged into whatever `extra` asked for rather than replacing it,
            # so a caller who configured `livecrawl` or `maxAgeHours` keeps it
            # — but `text` is ours, because it is what the reader reads.
            contents = dict(payload.get("contents") or {})
            contents["text"] = True
            payload["contents"] = contents
        return payload

    def read_results(
        self, payload: dict[str, Any] | None, *, query: SearchQuery
    ) -> SearchOutcome:
        if not isinstance(payload, dict) or not isinstance(payload.get("results"), list):
            raise SearchUnreadableError(
                "Exa answered without a 'results' array, which is the one field "
                "its search response schema requires."
            )

        results: list[SearchResult] = []
        for entry in payload["results"]:
            if not isinstance(entry, dict):
                continue
            url = entry.get("url")
            if not isinstance(url, str) or not url:
                continue
            text = _text(entry.get("text"))
            results.append(
                SearchResult(
                    url=url,
                    title=_text(entry.get("title")),
                    # A summary is what Exa wrote about the page and highlights
                    # are passages it picked out; either is a snippet. The page
                    # text is not — putting it here would make a 5,000-character
                    # body look like a one-line description.
                    snippet=_text(entry.get("summary")) or _highlight(entry.get("highlights")),
                    content=text,
                    published_at=_text(entry.get("publishedDate")),
                    author=_text(entry.get("author")),
                )
            )

        report = payload.get("costDollars")
        return SearchOutcome(
            results=results,
            searches=1,
            tier=normalise_tier(query.tier, self),
            # Verbatim and unconverted. Read so it can be logged; see the
            # module docstring for the two reasons it is not billed from.
            vendor_report={"costDollars": report} if isinstance(report, dict) else {},
        )


def _text(value: Any) -> str | None:
    return value if isinstance(value, str) and value else None


def _highlight(value: Any) -> str | None:
    """The first highlight, if there are any.

    Only the first: the rest are more passages from the same page, and joining
    them would produce a "snippet" several paragraphs long.
    """
    if isinstance(value, list):
        for item in value:
            if isinstance(item, str) and item:
                return item
    return None
