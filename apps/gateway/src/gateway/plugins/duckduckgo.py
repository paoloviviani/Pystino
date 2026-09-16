"""DuckDuckGo, a keyless web-search backend.

Endpoint and parse facts, re-derived live 2026-09-16 from this deployment's
own egress: ``POST https://html.duckduckgo.com/html/`` with a form-encoded
``q`` answers HTTP 200 with eleven ``result__a`` anchors and eleven
``result__snippet`` anchors. The Instant Answer API is not an alternative —
it answers 200 with empty abstracts for ordinary web queries (an answers
API, not a search API, so it cannot back a results list). Two page-shape
facts the parse below depends on, both read off that live page: organic
hrefs arrive as bare absolute HTTPS URLs — the ``/l/?uddg=`` redirect wrap
is kept as a fallback, since the wrapping varies by locale and UA — and the
first slot can be a ``duckduckgo.com``-hosted ad (``y.js?ad_domain=…``),
which the off-vendor host filter drops rather than returning as a result.
Bot detection (``anomaly-modal`` / ``challenge-form``, previously observed
as HTTP 202 on flagged egress) is retained as a degradation path; it is not
what this network returns today, and the day it does the route answers a
named 502 rather than an empty result list. The sibling lite host is the
same engine at a different path and the opposite story here: on the same
day, from the same address, ``lite.duckduckgo.com/lite/`` answered the 202
challenge while this host answered 200 — and it cannot be a second
``base_url_options`` host anyway, because its path differs (``/lite/``) and
the path is the plugin's constant.

Two more endpoint facts, read at source rather than guessed:

* **SafeSearch does not travel.** The endpoint takes no safesearch field —
  the community-consensus scraper (``ddgs`` 9.16.0, its DuckDuckGo HTML
  engine) receives the parameter and drops it unread — so a search gets
  DuckDuckGo's own server-side default, community-documented as moderate.
  Sending a parameter the endpoint ignores would claim a control the vendor
  does not offer.
* **Ad slots are vendor-hosted.** The first anchor can be a
  ``duckduckgo.com/y.js?ad_domain=…`` ad; the consensus scraper filters
  exactly that href prefix, and the off-vendor-host rule below drops it the
  same way — an ad's click URL is the vendor's, not a result.

The protocol fit is the deliberate exception this backend exists to record
(``plugins/search.py`` names it): every other backend speaks JSON POST, and
this one speaks form POST and answers HTML. The route therefore honors a
form capability per plugin — ``build_search_form`` /
``read_search_results_text`` / ``is_search_challenge`` — and keeps
``post_json`` for the existing three. The media type is vendor knowledge, so
it lives here (ADR 0032); the transport (``upstream.post_form``) is generic.

Four things an operator is entitled to know before assigning this backend to
a group:

* **Queries leave the deployment.** The query text and the server's IP go to
  DuckDuckGo, a US company, with no key and no account — and therefore no
  contract. There is no redaction exemption for this backend: the unified
  route screens the query under the effective policy first, exactly as for
  every other backend.
* **The kill switch is the policy.** There is no credential to revoke on a
  keyless backend; unassigning the group's search policy (or deactivating
  the provider) stops the egress. ``requires_api_key`` is ``False`` so the
  console offers creation without a key.
* **A challenge is a 502, not an empty answer.** Where DuckDuckGo serves
  bot detection instead of results, returning zero hits would read as "no
  results found", which is the wrong fact. The route degrades it to a named
  502 — counted, like every other vendor failure, never refunded.
* **No second implementation.** The chat-side direct scrape was reverted;
  search goes through the gateway, full stop — this plugin is the only DDG
  scrape, so there is nothing to keep in step with it.
"""

from __future__ import annotations

import html as html_lib
import re
from collections.abc import Mapping
from typing import Any
from urllib.parse import unquote, urljoin, urlparse

from gateway.plugins.base import ProviderKind
from gateway.plugins.generic import GenericOpenAIPlugin
from gateway.plugins.search import UnifiedSearchResult

#: A desktop UA: without one the endpoint answers a different (or no) page.
_USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0 Safari/537.36"
)

#: Bound the buffered page; a results page is tens of KB, a challenge ~14KB.
MAX_RESPONSE_CHARS = 1_000_000
MAX_SNIPPET_CHARS = 500

_ANCHOR_PATTERN = re.compile(
    r'<a\b[^>]*class="result__a"[^>]*href="([^"]+)"[^>]*>([\s\S]*?)</a>',
    re.IGNORECASE,
)
_SNIPPET_PATTERN = re.compile(
    r'<a\b[^>]*class="result__snippet"[^>]*>([\s\S]*?)</a>',
    re.IGNORECASE,
)
_TAG_PATTERN = re.compile(r"<[^>]+>")
_WHITESPACE_PATTERN = re.compile(r"\s+")
_REDIRECT_PARAM_PATTERN = re.compile(r"[?&]uddg=([^&]+)")


def _clean_text(fragment: str) -> str:
    """Strip tags, decode entities, collapse whitespace — titles and snippets
    are HTML fragments, and the unified shape carries text."""
    return _WHITESPACE_PATTERN.sub(" ", html_lib.unescape(_TAG_PATTERN.sub(" ", fragment))).strip()


def _target_url(href: str) -> str | None:
    """The result link out of a ``result__a`` anchor href.

    DDG wraps the target in a ``/l/?uddg=<urlencoded-target>`` redirect; a
    bare absolute URL (or a protocol-relative one) is used as-is. Anything
    else is not a result — and anything not HTTPS is refused rather than
    repaired, because the gateway must not downgrade a reader to cleartext.
    """
    unescaped = href.replace("&amp;", "&").replace("&AMP;", "&")
    redirect = _REDIRECT_PARAM_PATTERN.search(unescaped)
    try:
        if redirect:
            url = urlparse(unquote(redirect.group(1)))
            return url.geturl() if url.scheme == "https" else None
        absolute = urlparse(urljoin("https://duckduckgo.com", unescaped))
        if absolute.scheme == "https" and absolute.hostname != "duckduckgo.com":
            return absolute.geturl()
        return None
    except ValueError:
        return None


def parse_duckduckgo_results(html: str, max_results: int) -> list[UnifiedSearchResult]:
    """Minimal parse of the HTML results page: every ``result__a`` anchor in
    order, each paired with the first ``result__snippet`` that follows it
    before the next result. Tolerant by design — DDG restyles this page — so
    an unparseable page yields no hits rather than an exception."""
    capped = min(10, max(1, max_results))
    anchors = [
        (match.start(), match.group(1), _clean_text(match.group(2)))
        for match in _ANCHOR_PATTERN.finditer(html)
    ]
    snippets = [
        (match.start(), _clean_text(match.group(1))) for match in _SNIPPET_PATTERN.finditer(html)
    ]
    hits: list[UnifiedSearchResult] = []
    for index, (_, href, title) in enumerate(anchors):
        if len(hits) >= capped:
            break
        url = _target_url(href)
        if url is None:
            continue
        next_result = anchors[index + 1][0] if index + 1 < len(anchors) else float("inf")
        snippet = next(
            (text for position, text in snippets if anchors[index][0] < position < next_result),
            "",
        )
        hits.append(
            {
                "title": title or "untitled",
                "url": url,
                "snippet": snippet[:MAX_SNIPPET_CHARS],
            }
        )
    return hits


def is_duckduckgo_challenge(html: str) -> bool:
    """Whether the page is bot detection rather than results."""
    return "anomaly-modal" in html or "challenge-form" in html


class DuckDuckGoSearchPlugin(GenericOpenAIPlugin):
    """DuckDuckGo's HTML endpoint: keyless form search, HTML answer."""

    name = "duckduckgo"
    label = "DuckDuckGo (web search)"
    description = (
        "A keyless web-search backend, not an inference endpoint. No credential is "
        "stored and queries leave the deployment to DuckDuckGo with no contract. "
        "Experimental: it scrapes DuckDuckGo's non-JS HTML page, whose structure can "
        "change without notice, and automated or datacenter use can be answered with "
        "a bot challenge instead of results — the search then fails loudly, counted. "
        "Searches are metered as a count of requests and are never priced."
    )
    kind = ProviderKind.SEARCH
    default_base_url: str | None = "https://html.duckduckgo.com"
    reports_authoritative_cost = False

    #: The console reads this (``describe()``) to offer creation without a
    #: key: a keyless backend that demanded one would be uncreatable except
    #: with a dummy credential, which would then look like a secret worth
    #: rotating. Absent means a key is required, which is every other plugin.
    requires_api_key = False

    search_path = "/html/"

    def auth_headers(self, credential: str) -> Mapping[str, str]:
        """Nothing: there is no credential to present, and one stored on the
        row by mistake must not travel to a vendor that never asked for it."""
        return {}

    def search_headers(self) -> Mapping[str, str]:
        """The desktop UA the endpoint needs, plus the locale. Kept out of
        ``auth_headers`` on purpose: those travel on the passthrough too, and
        this backend has no passthrough (its answer is HTML, not JSON)."""
        return {"user-agent": _USER_AGENT, "accept-language": "en"}

    def build_search_body(self, query: str, max_results: int) -> dict[str, Any]:
        """``q`` and nothing else — the vendor's whole request shape.

        Never sent as JSON: the unified route prefers ``build_search_form``
        below whenever a plugin offers it, and the passthrough refuses this
        backend outright (its answer is HTML, so there is no verbatim JSON
        to return). Kept so the structural protocol holds for every search
        plugin, and so the one field the route screens is named in one place.
        """
        return {"q": query}

    def build_search_form(self, query: str, max_results: int) -> dict[str, str]:
        """The form fields POSTed to ``/html/``: ``q`` and nothing else.

        No count travels: the endpoint takes none, and always answers with
        its own page of results — the reader below truncates to
        ``max_results``, which the route already bounds at ten. No safesearch
        either: the field the vendor ignores is not sent (see the module
        docstring); a search runs at DuckDuckGo's server-side default.
        """
        return {"q": query}

    def read_search_results(self, payload: Any) -> list[UnifiedSearchResult]:
        """Absent for this backend: it never answers JSON.

        The unified route reads ``read_search_results_text`` off the raw
        reply instead. Returning ``[]`` here rather than raising keeps a
        caller that reaches this reader through the JSON path generic — an
        empty answer, not a gateway fault — while documenting that the path
        is never taken.
        """
        return []

    def read_search_results_text(self, text: str, max_results: int) -> list[UnifiedSearchResult]:
        """The HTML page into title/URL/snippet dicts, best-effort.

        Capped to the first megabyte before parsing — a results page is tens
        of KB — and entries without a usable HTTPS URL are dropped, because
        a result that cannot be fetched is not a result.
        """
        return parse_duckduckgo_results(text[:MAX_RESPONSE_CHARS], max_results)

    def is_search_challenge(self, text: str) -> bool:
        """Whether the reply is bot detection rather than results."""
        return is_duckduckgo_challenge(text)
