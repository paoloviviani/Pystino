"""Jina, a web-search backend.

Read at source on 2026-09-14 from the vendor's own API guide,
``https://docs.jina.ai/`` (Search API section), corroborated by the official
CLI's ``search_web`` (``https://github.com/jina-ai/cli/blob/main/jina_cli/api.py``)
and the rate table on ``https://jina.ai/reader`` — not from the ``curl``
example that motivated this plugin, which disagrees with them. The hint was a
GET with the query in the URL (``https://s.jina.ai/?q=…``); the documented
programmatic shape is a POST with a JSON body, and the schema wins (the same
rule that settled Linkup's ``curl -G`` against its OpenAPI document):

* ``POST https://s.jina.ai/``, bearer auth, ``Content-Type`` and ``Accept``
  both ``application/json``. GET forms exist (the open-source README searches
  as ``https://s.jina.ai/your+query`` and the reader page shows ``?q=``), but
  they put the query in the URL — which the unified route promises never to do
  ("Nothing a caller sends reaches a URL") — and neither documents the
  ``num`` count the unified body carries.
* The request body is ``{"q": …, "num": …}`` (``q`` required; ``num`` sets
  how many results come back, defaulting to five). The optional ``gl``,
  ``hl``, ``location`` and ``page`` fields are omitted: the unified route
  offers no locale or pagination, so the vendor runs its defaults.
* The answer is ``{"code": 200, "status": 20000, "data": […]}`` with one entry
  per result carrying ``title``, ``url``, ``description`` and ``content`` —
  where ``content`` is the full page text read through the Reader pipeline,
  not a snippet.
* ``X-Respond-With: no-content`` excludes that page text, leaving titles, URLs
  and descriptions only. The unified route always sends it: without it every
  search would haul whole pages across the wire only for the reader to drop
  them, and Jina bills its key a fixed token amount per search starting from
  10,000 — paying for content nobody reads. The passthrough never sends it,
  because a verbatim answer must stay the caller's own shape.
* Jina bills its own key per request in tokens; like every other backend here
  that is the vendor's business, not the ledger's. Searches are metered as a
  count of requests and are never priced. An operator on the EU-only
  deployment remotes this by pointing the base URL at ``https://eu.s.jina.ai/``;
  the path is configuration either way.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from gateway.plugins.base import ProviderKind
from gateway.plugins.generic import GenericOpenAIPlugin
from gateway.plugins.search import UnifiedSearchResult


class JinaSearchPlugin(GenericOpenAIPlugin):
    """Jina's search API: SERP entries with page text excluded by header."""

    name = "jina"
    label = "Jina (web search)"
    description = (
        "A web-search backend, not an inference endpoint. Searches are metered "
        "as a count of requests and are never priced: the caller speaks the "
        "gateway's unified shape and the plugin rebuilds Jina's own request."
    )
    kind = ProviderKind.SEARCH
    default_base_url: str | None = "https://s.jina.ai"
    reports_authoritative_cost = False

    #: The endpoint is the base URL itself: the CLI posts to it with a
    #: trailing slash, so the path the gateway appends is just ``/``.
    search_path = "/"

    def auth_headers(self, credential: str) -> Mapping[str, str]:
        """Bearer, which is the only security scheme the API guide names.

        A keyless call answers 401, so unlike the reader endpoint there is no
        anonymous tier to fall back to.
        """
        return {"authorization": f"Bearer {credential}"}

    def search_headers(self) -> Mapping[str, str]:
        """``X-Respond-With: no-content`` — titles, URLs and descriptions only.

        Kept out of ``auth_headers`` on purpose: those travel on the
        passthrough too, and a verbatim answer must stay the caller's own
        shape rather than one this gateway narrowed.
        """
        return {"X-Respond-With": "no-content"}

    def build_search_body(self, query: str, max_results: int) -> dict[str, Any]:
        """``q`` and ``num``, and nothing else.

        No locale or pagination: omitting them asks for Jina's defaults, which
        is what the unified route always runs — it offers no tier, so the
        ledger records none.
        """
        return {"q": query, "num": max_results}

    def read_search_results(self, payload: Any) -> list[UnifiedSearchResult]:
        """``data[].title``/``url``/``description``, best-effort.

        The description is preferred and the page text is the fallback, for
        the case where the vendor answered with content despite the header;
        without either the entry still counts — a link and a title searched —
        with an empty snippet. Entries without a usable URL are dropped,
        because a result that cannot be fetched is not a result.
        """
        data = payload.get("data") if isinstance(payload, dict) else None
        if not isinstance(data, list):
            return []
        out: list[UnifiedSearchResult] = []
        for entry in data:
            if not isinstance(entry, dict):
                continue
            url = entry.get("url")
            if not isinstance(url, str) or not url:
                continue
            title = entry.get("title")
            description = entry.get("description")
            content = entry.get("content")
            out.append(
                {
                    "title": title if isinstance(title, str) and title else "untitled",
                    "url": url,
                    "snippet": (
                        (description if isinstance(description, str) else "")
                        or (content if isinstance(content, str) else "")
                    ),
                }
            )
        return out
