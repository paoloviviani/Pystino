"""What a search backend is, and the one thing it is forbidden to return.

Phase 2 of [docs/web-search-plan.md](../../../../../docs/web-search-plan.md).
A ``SearchPlugin`` is a ``ProviderPlugin`` that can also *run a search*: the
same registry, the same provider row, the same encrypted credential (ADR 0027),
and one extra pair of methods. It is not a second plugin system, and that is
deliberate — the local extractor already established that ``providers.plugin``
is the answer to "what kind of thing is behind this row", and a second
discriminator for one more case would be a second thing to keep in step.

Three ideas carry it.

**A search plugin returns a count, never a price.** ADR 0032's rule is that a
plugin returns facts and ``accounting/cost.py`` is the only code that
multiplies a count by a rate. Here it goes one step further: there is no rate
at all. The user's decision, in their words — *"I would avoid metering the
money here, given the complexity I see. let's meter around the number of
requests to the backend. this will allow anyway to reconcile externally later.
quotas also may be defined around number of requests."* So ``SearchOutcome``
has a ``searches`` count and two label strings and deliberately no money field,
and there is nothing a plugin can return that would let the gateway price a
search. Exa reports ``costDollars`` on every response; it is read into
``vendor_report`` so it can be *logged*, and it is not stored in a cost column
— a dollar figure in ``upstream_cost`` would be picked up by reconciliation and
reported as a charge against a price table that does not exist.

**The tier is what was asked for, not what came back.** ``own_search_tier``
records the depth or search type this gateway *requested*, because that is the
only version of it that is knowable before the call and the only one a vendor's
invoice is itemised by. Neither vendor echoes a tier back in a form worth
trusting over what we sent.

**One call is one search.** ``searches`` is not read out of a response body.
It is the number of HTTP requests made to the vendor, which is the unit every
one of them bills and dashboards by, and it is counted whether or not the
vendor liked the request — see ``routers/search.py`` for why a failed search
still counts.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable


class SearchConfigurationError(ValueError):
    """The request cannot be turned into a call this backend would accept.

    Raised by ``build_request`` for a tier the vendor does not have. Refused
    rather than silently substituted: ``auto`` falling back between engines is
    the one thing docs/web-search-plan.md says explicitly not to copy, and
    quietly downgrading ``deep`` to ``standard`` would be the same failure a
    layer down — a cheaper search served under the name of a dearer one, with
    the ledger recording the label we asked for and the vendor billing the one
    it ran.
    """


class SearchUnreadableError(ValueError):
    """The vendor answered 2xx with something this plugin cannot read.

    Separate from an HTTP error because the outcomes differ: an HTTP error is
    the vendor's own message and is passed through, while this one means the
    vendor changed a shape under us. Both settle the same ledger row, and both
    count the search — it was made either way.
    """


@dataclass(frozen=True, slots=True)
class SearchQuery:
    """What the caller asked for, normalised across vendors.

    Normalised rather than passed through, because the two vendors agree on
    almost no field name: Linkup wants ``q``/``depth``/``outputType`` and Exa
    wants ``query``/``type``/``numResults``/``contents``. A passthrough surface
    would make the caller write vendor-specific bodies against a route whose
    whole point is that the backend is an administrator's choice.

    ``extra`` is the escape hatch for the rest of a vendor's option set — date
    ranges, domain filters, categories — which is long, vendor-specific and
    grows without us. It is applied *under* the fields the plugin controls, so
    nothing in it can change the tier we reserved against or the output shape
    the reader expects.
    """

    query: str
    #: The depth or search type to ask for. Never None by the time a plugin
    #: sees it: the route fills it in from the model row, so that which tier a
    #: caller may run is a grant an administrator makes rather than a parameter
    #: a caller chooses (see ``routers/search.py``).
    tier: str
    max_results: int | None = None
    #: Whether to ask the vendor for page text as well as a snippet. Off by
    #: default: with Exa it is a separate charge on the same call, and with
    #: Linkup it is not on offer at all.
    include_content: bool = False
    include_domains: Sequence[str] = ()
    exclude_domains: Sequence[str] = ()
    extra: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class SearchResult:
    """One result, in the fields every backend actually has.

    Everything here is nullable except the URL, because the URL is the only
    field all of them promise. A result with no title is a real result; a
    result with no URL is not one.
    """

    url: str
    title: str | None = None
    #: The vendor's short description. Linkup returns only this; Exa returns it
    #: only if asked for text or highlights.
    snippet: str | None = None
    #: Page text, when ``include_content`` was asked for and the vendor
    #: returned some.
    content: str | None = None
    published_at: str | None = None
    author: str | None = None
    #: ``text`` or ``image``. Only Linkup returns images, and only when asked
    #: for them; an image hit carries a URL and a name and nothing else.
    #: Carried rather than dropped because a caller who switched images on
    #: should get them, and rather than folded into the text results because
    #: they are not pages.
    media_type: str = "text"

    # There is deliberately no relevance score. Neither vendor returns one:
    # Linkup's result schema has `name`, `url`, `content`, `favicon`, `type`
    # and nothing else, and Exa's has no `score` field at all — only
    # `highlightScores`, which scores highlights rather than results. A field
    # nothing could ever populate would read as "this backend does not rank",
    # which is the opposite of true.


@dataclass(frozen=True, slots=True)
class SearchOutcome:
    """The results, and the facts the ledger meters by.

    ``searches`` is the count that reaches ``observe_own_search``. It is a
    field rather than ``len(results)`` because they answer different questions:
    a search that found nothing was still a search, and was still billed by the
    vendor.

    ``vendor_report`` is whatever the vendor said about what this call cost it,
    verbatim and unconverted — Exa's ``costDollars`` object is the case it
    exists for. **Nothing prices from it.** It is logged, not stored: a figure
    of the vendor's in a cost column would be read by ``_reconciliation`` as a
    counterparty charge, and this gateway has no search price table for it to
    disagree with.
    """

    results: Sequence[SearchResult]
    searches: int
    tier: str
    vendor_report: Mapping[str, Any] = field(default_factory=dict)


@runtime_checkable
class SearchPlugin(Protocol):
    """A counterparty plugin that can also run a web search.

    Structural, and checked with ``isinstance`` at the route: a provider row
    naming a plugin that is not one of these is refused with a message naming
    the plugin, rather than failing later with an ``AttributeError`` that reads
    like a gateway fault.

    Implementations also satisfy ``ProviderPlugin`` — they are registered in
    the same registry, resolved by the same ``providers.plugin`` column, and
    authenticate through the same ``auth_headers``. What makes them a search
    backend is ``kind == ProviderKind.SEARCH`` plus the six members here.
    """

    #: The registry name, which is also what lands in
    #: ``usage_records.own_search_backend``. Declared here as well as on
    #: ``ProviderPlugin`` because this protocol is what the route holds, and a
    #: label written into the ledger should be typed where it is read.
    name: str
    #: The path appended to the provider row's base URL. On the plugin rather
    #: than in the route because where a counterparty puts an endpoint is the
    #: plugin's knowledge (ADR 0032) — and unlike ``/ocr``, which is one path
    #: two counterparties happen to agree on, these two already disagree:
    #: Linkup's version segment is in its base URL and Exa's is not.
    search_path: str
    #: Every tier this backend offers, cheapest first where the vendor orders
    #: them. Named in the error a bad tier produces, so an operator who typed
    #: one into a model row reads the list of real ones back.
    #:
    #: **A wrong tier is caught on the first search, not on save.** The admin
    #: API does not validate ``upstream_model`` against this, because that
    #: field is free text for every other kind of model and a validator that
    #: fired only for search rows would be a rule in one place with its reason
    #: in another. The cost is one 400 that could have been earlier; the
    #: refusal happens before the reservation, so nothing is spent or counted.
    tiers: tuple[str, ...]
    #: What a model row that names no tier gets. Rarely reached — the admin API
    #: requires a non-empty ``upstream_model`` — so this is the answer for a
    #: row written some other way, not a default anyone configures.
    default_tier: str

    def build_request(self, query: SearchQuery) -> dict[str, Any]:
        """The vendor's request body for *query*.

        Raises ``SearchConfigurationError`` for a tier this vendor does not
        have. Never guesses one: see that exception's docstring.
        """
        ...

    def read_results(
        self, payload: dict[str, Any] | None, *, query: SearchQuery
    ) -> SearchOutcome:
        """The vendor's response, read into facts.

        Given the query as well as the payload because the tier that reaches
        the ledger is the one we *asked* for, and neither vendor returns it.

        Raises ``SearchUnreadableError`` when the body is not in a shape this
        plugin recognises. Returning an empty result list instead would record
        a successful search that found nothing, which is a different event and
        one an operator would never investigate.
        """
        ...


def normalise_tier(tier: str | None, plugin: SearchPlugin) -> str:
    """The tier to ask for, given what a model row says.

    One place rather than in each plugin's ``build_request``, because the
    fallback is a policy decision (an unconfigured model searches at the
    vendor's cheapest sensible default) rather than a vendor fact.
    """
    wanted = (tier or "").strip()
    return wanted or plugin.default_tier


def tier_catalogue(plugin: SearchPlugin, descriptions: Mapping[str, str]) -> dict[str, Any]:
    """This backend's tiers, as a catalogue the importer already reads.

    A search backend publishes no ``/models`` endpoint, so Discover against one
    would fetch a URL the operator never typed and get a 404 — the exact
    failure ``LocalExtractorPlugin.builtin_catalogue`` exists to prevent. What
    it *does* have is a fixed enum of depths, and a "model" on a search
    provider is a depth: ``upstream_model`` carries the tier, and granting a
    group one model rather than another is how an administrator decides which
    depths they may run.

    **Unpriced, and importable anyway.** Every entry omits ``pricing``, and
    that is not a gap: the meter for a search is a request count
    (``LimitMetric.OWN_SEARCH_REQUESTS``), so a rate would multiply nothing —
    what bounds it is the request ceiling, granted per group like any other
    access. The importer keys on the ``Search`` tag here and creates the model
    with no price row, which is why a search tier shows in Discover with no
    "cannot import" reason while an unpriced *chat* model — where a missing
    rate really would be a quiet giveaway — still shows one. The two look
    alike in the catalogue payload and are opposite in the ledger; the tag is
    what tells them apart.
    """
    return {
        "data": [
            {
                "id": tier,
                "description": descriptions.get(tier, ""),
                # What `_kind_of` reads. A search and a chat completion have
                # identical modalities — text in, text out — so the tag is the
                # only thing that can tell them apart, exactly as it is for OCR.
                "tags": ["Search"],
                "input_modalities": ["text"],
                "output_modalities": ["text"],
            }
            for tier in plugin.tiers
        ]
    }
