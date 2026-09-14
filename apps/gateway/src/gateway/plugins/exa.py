"""Exa, a web-search backend.

Under the passthrough design (ADR 0071) this plugin is configuration, not
translation: it names the endpoint path and how the stored credential is
presented. Everything else about Exa — the ``query``/``type``/``numResults``
request fields, the ``results[].title``/``summary`` response shape — is the
caller's business, and this deployment's caller is the chat, which owns the
adapter. The facts the adapter needs were read at source on 2026-09-11 from
``https://api.exa.ai/openapi.json`` (``info.version`` 2.0.0), and one of them
is a trap worth recording here rather than losing:

* A *different* copy of that document — the one a documentation-rendering
  fetch follows — still serves the old three-value search-type enum
  ``neural``/``keyword``/``auto``. Implementing against that would produce a
  400 on every request at the dearer tiers. The live document is
  authoritative: ``type`` is ``instant``, ``fast``, ``auto``, ``deep-lite``,
  ``deep``, ``deep-reasoning``, defaulting to ``auto``.
* Without ``contents``, Exa returns links and titles and no text whatsoever —
  text is a separate charge on the same call, so an adapter wanting passages
  asks for ``contents: {"text": true}`` deliberately.
* The response carries ``costDollars``, whose own description says it is not
  an invoice record. It is read, if at all, to be logged and reconciled
  against — never billed from.

Exa has moved to per-call payment (an ``x402`` challenge on an unfunded key),
which the gateway passes through verbatim like any other vendor answer.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from gateway.plugins.base import ProviderKind
from gateway.plugins.generic import GenericOpenAIPlugin
from gateway.plugins.search import UnifiedSearchResult


class ExaSearchPlugin(GenericOpenAIPlugin):
    """Exa's search API: ranked pages, with text as a separate charge."""

    name = "exa"
    label = "Exa (web search)"
    description = (
        "A web-search backend, not an inference endpoint. Searches are metered "
        "as a count of requests and are never priced: the caller speaks the "
        "vendor's own API through the gateway's passthrough."
    )
    kind = ProviderKind.SEARCH
    default_base_url: str | None = "https://api.exa.ai"
    reports_authoritative_cost = False

    search_path = "/search"

    def auth_headers(self, credential: str) -> Mapping[str, str]:
        """``x-api-key``. Exa also accepts a bearer token; this sends the
        header its schema names first, so a key that works with their own
        examples works here."""
        return {"x-api-key": credential}

    def build_search_body(self, query: str, max_results: int) -> dict[str, Any]:
        """``query`` and ``numResults``, and nothing else.

        No ``type``: omitting it asks for Exa's default (``auto``), which is
        what the unified route always runs — it offers no tier, so the ledger
        records none. No ``contents`` either: page text is a separate charge
        on the same call, and a unified answer of titles, links and snippets
        must not smuggle one in.
        """
        return {"query": query, "numResults": max_results}

    def read_search_results(self, payload: Any) -> list[UnifiedSearchResult]:
        """``results[].title``/``url``/``summary``, best-effort.

        The summary is preferred and the text is the fallback; without either
        the entry still counts — a link and a title searched — with an empty
        snippet. Entries without a usable URL are dropped, because a result
        that cannot be fetched is not a result.
        """
        results = payload.get("results") if isinstance(payload, dict) else None
        if not isinstance(results, list):
            return []
        out: list[UnifiedSearchResult] = []
        for entry in results:
            if not isinstance(entry, dict):
                continue
            url = entry.get("url")
            if not isinstance(url, str) or not url:
                continue
            title = entry.get("title")
            summary = entry.get("summary")
            text = entry.get("text")
            out.append(
                {
                    "title": title if isinstance(title, str) and title else "untitled",
                    "url": url,
                    "snippet": (
                        (summary if isinstance(summary, str) else "")
                        or (text if isinstance(text, str) else "")
                    ),
                }
            )
        return out
