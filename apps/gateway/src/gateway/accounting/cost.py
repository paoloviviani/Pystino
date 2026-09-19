"""Cost computation.

Exact Decimal arithmetic throughout, and two billing decisions written down
because they are the kind of thing that silently produces wrong invoices:

**Cached prompt tokens are a subset of prompt tokens, not an addition.** Every
provider that reports a cache-read count includes those tokens in
``prompt_tokens`` as well. Billing both fields at full rate double-charges the
cached portion, so the uncached remainder is billed at the input rate and the
cached portion at the cache-read rate. When no cache-read price is configured the
whole prompt is billed at the input rate, which is the conservative reading.

**Reasoning tokens are already inside completion_tokens.** OpenAI-compatible
providers report them as a breakdown of the completion, not as an extra. They are
persisted for visibility and deliberately not billed again.

**Anthropic counts the prompt the other way round, and it matters.** OpenAI's
``prompt_tokens`` *includes* the cached tokens; Anthropic's ``input_tokens``
*excludes* them — the prompt is ``input_tokens + cache_creation_input_tokens +
cache_read_input_tokens``. Feeding Anthropic's numbers through the OpenAI reader
would subtract the cache read from a figure that never contained it and
undercharge every cached request, so each surface gets its own reader rather
than one tolerant function that guesses (ADR 0030).

**Cache writes are billed, at a premium.** A cache-creation token is charged
above the input rate — the whole point being that it pays for itself on later
reads. It is a third disjoint slice of the prompt, alongside the uncached
remainder and the cache reads.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime
from decimal import Decimal
from typing import Any

from gateway.models import ModelPrice
from gateway.types import as_decimal, utcnow

MILLION = Decimal(1_000_000)


class CurrencyMismatch(Exception):
    """A price is denominated in something other than the billing currency.

    Superseded by ADR 0054, which converts at the decision and aggregation
    points using the daily ECB rate — kept only as the exception a stale caller
    might still reference. Nothing raises it any more.
    """


@dataclass(frozen=True, slots=True)
class TokenCounts:
    prompt: int = 0
    completion: int = 0
    cached_prompt: int = 0
    reasoning: int = 0
    #: Prompt tokens written to a provider-side cache, billed above the input
    #: rate. A disjoint slice of ``prompt``, like ``cached_prompt``.
    cache_write: int = 0
    #: Images produced. Only ever non-zero on the image route, where many models
    #: are priced per image rather than per token (ADR 0030).
    images: int = 0
    #: Pages read. Only ever non-zero on the OCR route. Taken from the
    #: counterparty's `usage_info.pages_processed` where it reports one, and from
    #: the document itself when extraction happened locally — never inferred
    #: from the length of the text, which would be a billing figure with no
    #: source.
    pages: int = 0
    #: Server-side web searches the provider ran and charged for (ADR 0058).
    #: Read only from what the provider reports — never from counting
    #: `server_tool_use` blocks in the response, because a search that errors
    #: produces a block and is explicitly not billed.
    searches: int = 0

    @property
    def total(self) -> int:
        return self.prompt + self.completion

    @property
    def billable_prompt(self) -> int:
        """Prompt tokens charged at the full input rate.

        The uncached, non-written remainder: cache reads and cache writes are
        each billed at their own rate, and charging them here too would bill the
        same token twice.
        """
        return max(0, self.prompt - self.cached_prompt - self.cache_write)

    @classmethod
    def from_usage(cls, usage: dict[str, Any] | None) -> TokenCounts:
        """Read a Chat Completions ``usage`` object.

        Tolerant by necessity: providers disagree on where the cache and
        reasoning breakdowns live, and several omit them entirely.
        """
        if not usage:
            return cls()

        prompt = _as_int(usage.get("prompt_tokens"))
        completion = _as_int(usage.get("completion_tokens"))

        cached, cache_write = _cache_slices(usage, usage.get("prompt_tokens_details"), prompt)

        completion_details = usage.get("completion_tokens_details") or {}
        reasoning = (
            _as_int(completion_details.get("reasoning_tokens"))
            if isinstance(completion_details, dict)
            else 0
        )

        return cls(
            prompt=prompt,
            completion=completion,
            cached_prompt=cached,
            cache_write=cache_write,
            reasoning=reasoning,
            searches=_search_requests(usage),
        )

    @classmethod
    def from_responses_usage(cls, usage: dict[str, Any] | None) -> TokenCounts:
        """Read a Responses API ``usage`` object.

        The same convention as Chat Completions — ``input_tokens`` includes the
        cached tokens — under different names. The nested detail objects are
        optional: the reference provider returns the three totals flat and
        nothing else.
        """
        if not usage:
            return cls()

        prompt = _as_int(usage.get("input_tokens"))
        completion = _as_int(usage.get("output_tokens"))

        cached, cache_write = _cache_slices(usage, usage.get("input_tokens_details"), prompt)

        out_details = usage.get("output_tokens_details")
        reasoning = (
            _as_int(out_details.get("reasoning_tokens")) if isinstance(out_details, dict) else 0
        )

        return cls(
            prompt=prompt,
            completion=completion,
            cached_prompt=cached,
            cache_write=cache_write,
            reasoning=reasoning,
            searches=_search_requests(usage),
        )

    @classmethod
    def from_anthropic_usage(cls, usage: dict[str, Any] | None) -> TokenCounts:
        """Read an Anthropic Messages ``usage`` object.

        **The inverse convention**, and the reason this is not the Chat
        Completions reader with different key names: Anthropic's
        ``input_tokens`` is the uncached remainder, *not* the whole prompt. The
        prompt is the sum of the three slices, and each is billed at its own
        rate. Reading it the OpenAI way would undercharge every cached request
        and, once the cache read exceeded the remainder, charge nothing at all
        for the prompt.
        """
        if not usage:
            return cls()

        uncached = _as_int(usage.get("input_tokens"))
        cache_write = _as_int(usage.get("cache_creation_input_tokens"))
        cache_read = _as_int(usage.get("cache_read_input_tokens"))

        return cls(
            prompt=uncached + cache_write + cache_read,
            completion=_as_int(usage.get("output_tokens")),
            cached_prompt=cache_read,
            cache_write=cache_write,
            # Documented on this surface, and verified against the live schema
            # on 2026-09-08: `usage.server_tool_use.web_search_requests`.
            searches=_search_requests(usage),
        )

    @classmethod
    def from_image_usage(cls, usage: dict[str, Any] | None, *, images: int = 0) -> TokenCounts:
        """Read an image-generation ``usage`` object, if there is one.

        Optional on every provider that returns it, and absent entirely on the
        per-image-priced models — which is why ``images`` is carried separately
        rather than inferred from the token counts.
        """
        # `replace` rather than a field-by-field rebuild: this listed every
        # field it knew about, so each new billable unit added to TokenCounts
        # was silently dropped here. The same trap `_with_units` fell into.
        return replace(cls.from_responses_usage(usage), images=max(0, images))

    @classmethod
    def from_ocr_usage(cls, usage: dict[str, Any] | None, *, pages: int = 0) -> TokenCounts:
        """Read an OCR ``usage_info`` object.

        Its own reader rather than a tolerant one, for the reason the Anthropic
        reader exists: the field is named differently *and* means something else.
        ``usage_info.pages_processed`` is the billable count on this surface —
        Cortecs and Mistral both report it — and there is no ``prompt_tokens``
        to read at all, so a reader that went looking for one would return zero
        counts for a request that really cost money.

        ``pages`` is the fallback for the local extractor, which has no
        counterparty and therefore no reported usage: the page count comes from
        the document. Never from the length of the extracted text, which would
        be a billing figure with no source (ADR 0053's rule, one surface over).

        ``usage_info.credits`` — what Cortecs also reports here — is
        deliberately never read. Credits are not money: no published exchange
        rate says what one is worth, so folding it into `TokenCounts` would
        hand `compute_cost` a number it would multiply by a EUR rate anyway,
        producing a wrong invoice that looks precise. Billing on this surface
        stays on `pages_processed` alone; if a verified credit-to-currency rate
        ever turns up, it is a new field on `ReportedCost`, not a value read
        into this reader.
        """
        reported = 0
        if usage:
            reported = _as_int(usage.get("pages_processed"))
        return cls(pages=max(0, reported or pages))



def _search_requests(usage: dict[str, Any]) -> int:
    """Server-side web searches reported in a ``usage`` object.

    One spelling, in one place, read on **every** surface — and that needs
    justifying against this module's own rule that the surfaces must not share
    a tolerant parser.

    The rule is about *meaning*. ``prompt_tokens`` and ``input_tokens`` differ
    in what they include, so merging them produces wrong numbers.
    ``server_tool_use.web_search_requests`` means exactly one thing wherever it
    appears: how many searches the counterparty ran and will invoice. Anthropic
    documents it on the Messages surface; a router that proxies Anthropic
    through an OpenAI-shaped response passes it through unchanged, and reading
    it there too costs nothing and bills correctly. This is the
    ``_CACHE_WRITE_KEYS`` case — one quantity, one meaning — not the prompt
    case.

    What is deliberately *not* read: OpenAI's own web search. It is billed per
    call, but its documentation states no usage field for the count, and its
    rate varies by ``search_context_size``. A count inferred from
    ``web_search_call`` output items would bill the calls that failed and
    would still not know the rate — the same refusal as ``usage_info.credits``
    on the OCR surface, for the same reason: a figure in an unverified unit is
    worse than none.
    """
    tool_use = usage.get("server_tool_use")
    if not isinstance(tool_use, dict):
        return 0
    return max(0, _as_int(tool_use.get("web_search_requests")))


#: Every spelling of "cache write tokens" seen in the wild, in the order they
#: are tried. All live inside ``prompt_tokens_details`` on the OpenAI-shaped
#: surfaces; Anthropic's flat ``cache_creation_input_tokens`` is read by its own
#: reader, which has to treat the prompt total differently anyway.
#:
#: Tolerating several spellings here is **not** the same mistake as tolerating
#: several prompt conventions, which the module docstring above refuses. These
#: keys differ only in how one provider spells a quantity every provider means
#: identically. The conventions differ in *meaning* — whether the cached tokens
#: are already inside ``prompt_tokens`` — and merging those produces numbers that
#: are wrong rather than merely differently named.
#:
#: Observed 2026-08-17 against the live reference provider, which routes to
#: several backends and passes each one's spelling through untouched:
#:
#: * ``created_cache_tokens``  — vLLM-backed models
#: * ``cache_write_tokens``    — Nebius; also OpenRouter, and LiteLLM reports it
#:                               from moonshot/deepseek/kimi
#: * ``cache_creation_tokens`` — Nebius returns this *as well*, hence "first
#:                               match wins" rather than a sum
#:
#: See docs/cache-accounting-findings.md for the raw evidence.
_CACHE_WRITE_KEYS = (
    "cache_write_tokens",
    "cache_creation_tokens",
    "created_cache_tokens",
    "cache_creation_input_tokens",
)

#: Cache reads. ``cached_tokens`` is the OpenAI standard and by far the common
#: case; the flat Anthropic-style key is accepted because proxies pass it through
#: onto an OpenAI-shaped response.
_CACHE_READ_KEYS = ("cached_tokens", "cache_read_tokens", "cache_read_input_tokens")


def _first_int(source: Any, keys: tuple[str, ...]) -> int:
    """The first of *keys* present and non-zero.

    First match rather than a sum: a provider that reports the same count under
    two names — Nebius sends both ``cache_write_tokens`` and
    ``cache_creation_tokens`` — would otherwise be billed twice for one slice.
    """
    if not isinstance(source, dict):
        return 0
    for key in keys:
        if (value := _as_int(source.get(key))) > 0:
            return value
    return 0


def _cache_slices(usage: dict[str, Any], details: Any, prompt: int) -> tuple[int, int]:
    """``(cache_read, cache_write)`` for an OpenAI-shaped usage object.

    Both are looked for in ``prompt_tokens_details`` first and then at the top
    level, because proxies flatten. Both are clamped so that together they never
    exceed the prompt: the three slices are disjoint subsets of it, and
    ``billable_prompt`` subtracts both, so a provider reporting a write count
    that is *not* inside its prompt total would otherwise drive the input charge
    to zero. Clamping loses a little precision in a case no provider is known to
    produce; not clamping loses money in it.
    """
    read = _first_int(details, _CACHE_READ_KEYS) or _first_int(usage, _CACHE_READ_KEYS)
    write = _first_int(details, _CACHE_WRITE_KEYS) or _first_int(usage, _CACHE_WRITE_KEYS)

    read = min(read, prompt)
    # The read is the slice we are most confident about — it is the standardised
    # field — so the write yields to it rather than the other way round.
    write = min(write, max(0, prompt - read))
    return read, write


def _as_int(value: Any) -> int:
    if isinstance(value, bool) or value is None:
        return 0
    if isinstance(value, int):
        return max(0, value)
    try:
        return max(0, int(value))
    except (TypeError, ValueError):
        return 0


@dataclass(frozen=True, slots=True)
class CostBreakdown:
    input_cost: Decimal
    output_cost: Decimal
    cache_read_cost: Decimal
    currency: str
    cache_write_cost: Decimal = Decimal(0)
    image_cost: Decimal = Decimal(0)
    #: Per page read, on the OCR surface. A component rather than folded
    #: into the input cost, so a reader can see which half of an OCR charge
    #: was pages and which was tokens.
    page_cost: Decimal = Decimal(0)
    #: Per server-side web search. A component of its own because it is the one
    #: charge on this list that is a *surcharge* on an otherwise ordinary
    #: request: a reader looking at a chat model's bill needs to see that part
    #: of it was not tokens at all.
    search_cost: Decimal = Decimal(0)

    @property
    def total(self) -> Decimal:
        return (
            self.input_cost
            + self.output_cost
            + self.cache_read_cost
            + self.cache_write_cost
            + self.image_cost
            + self.page_cost
            + self.search_cost
        )

    def scaled(self, rate: Decimal, currency: str) -> CostBreakdown:
        """The same breakdown, converted into another currency.

        Used at exactly the two points ADR 0054 allows conversion: the quota
        estimate compared against a billing-currency limit, and the recorded
        billing-currency cost. Every component scales by the same rate, so the
        sum of the parts keeps equalling the total.
        """
        return CostBreakdown(
            input_cost=self.input_cost * rate,
            output_cost=self.output_cost * rate,
            cache_read_cost=self.cache_read_cost * rate,
            cache_write_cost=self.cache_write_cost * rate,
            image_cost=self.image_cost * rate,
            page_cost=self.page_cost * rate,
            search_cost=self.search_cost * rate,
            currency=currency,
        )

    @classmethod
    def zero(cls, currency: str) -> CostBreakdown:
        return cls(
            input_cost=Decimal(0),
            output_cost=Decimal(0),
            cache_read_cost=Decimal(0),
            currency=currency,
        )


def select_price(prices: list[ModelPrice], *, at: datetime | None = None) -> ModelPrice | None:
    """The price in force at *at*.

    Prices are append-only and effective-dated, so this is "the most recent row
    that had already taken effect". Rows dated in the future are ignored, which
    is what makes scheduling a price change safe.
    """
    moment = at or utcnow()
    applicable = [price for price in prices if price.effective_from <= moment]
    if not applicable:
        return None
    return max(applicable, key=lambda price: price.effective_from)


def compute_cost(
    counts: TokenCounts,
    price: ModelPrice | None,
    *,
    fallback_currency: str = "EUR",
) -> CostBreakdown:
    """Cost of one request, in the **price's own currency**.

    A model with no price yields zero cost rather than an error: an operator who
    has not priced a model yet should still be able to serve it, and the usage row
    records ``price_id = NULL`` so the gap is visible in reporting rather than
    hidden in a rounding.

    ADR 0054 removes the currency refusal this function used to carry: a price
    in USD is computed in USD, and the caller converts to the billing currency
    at the decision and aggregation points, with the rate recorded on the usage
    row. Refusing here would have kept every community-catalogue model out of
    the ledger entirely.
    """
    if price is None:
        return CostBreakdown.zero(fallback_currency)

    input_rate = as_decimal(price.input_per_mtok)
    output_rate = as_decimal(price.output_per_mtok)

    # Each slice is priced only when a rate exists for it. An unpriced slice
    # falls back into the input rate rather than being billed at zero: charging
    # nothing for tokens the provider charged us for is the more expensive
    # mistake, and it is invisible until the invoice arrives.
    billable = counts.billable_prompt
    cache_cost = Decimal(0)
    write_cost = Decimal(0)

    if counts.cached_prompt:
        if price.cache_read_per_mtok is not None:
            cache_cost = (
                Decimal(counts.cached_prompt) * as_decimal(price.cache_read_per_mtok) / MILLION
            )
        else:
            billable += counts.cached_prompt

    if counts.cache_write:
        if price.cache_write_per_mtok is not None:
            write_cost = (
                Decimal(counts.cache_write) * as_decimal(price.cache_write_per_mtok) / MILLION
            )
        else:
            billable += counts.cache_write

    input_cost = Decimal(billable) * input_rate / MILLION
    output_cost = Decimal(counts.completion) * output_rate / MILLION

    # Per-image pricing sits alongside the token rates rather than replacing
    # them: a token-priced image model reports usage and charges nothing here,
    # and a per-image model reports no usage and charges nothing per token.
    image_cost = Decimal(0)
    if counts.images and price.per_image is not None:
        image_cost = Decimal(counts.images) * as_decimal(price.per_image)

    # Per-page pricing, for the same reason and on the same terms: an OCR
    # counterparty charges by the page, and a page is not a million of anything.
    # A model priced both ways is charged both ways — an OCR call that also
    # returns generated text has really incurred both.
    page_cost = Decimal(0)
    if counts.pages and price.per_page is not None:
        page_cost = Decimal(counts.pages) * as_decimal(price.per_page)

    # Per search, and the only one of the three that lands on a request the
    # caller thinks of as an ordinary completion. A model without the rate
    # charges nothing for searches the provider *did* invoice — which is the
    # under-billing this unit exists to end, and why the searches are recorded
    # on the row whether or not a rate existed to price them.
    search_cost = Decimal(0)
    if counts.searches and price.per_search is not None:
        search_cost = Decimal(counts.searches) * as_decimal(price.per_search)

    return CostBreakdown(
        input_cost=input_cost,
        output_cost=output_cost,
        cache_read_cost=cache_cost,
        cache_write_cost=write_cost,
        image_cost=image_cost,
        page_cost=page_cost,
        search_cost=search_cost,
        currency=price.currency.upper(),
    )
