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
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Any

from gateway.models import ModelPrice
from gateway.types import as_decimal, utcnow

MILLION = Decimal(1_000_000)


class CurrencyMismatch(Exception):
    """A price is denominated in something other than the billing currency.

    Deliberately fatal rather than converted. An exchange rate applied silently
    produces invoices that look right and are wrong; refusing forces an operator
    to fix the catalogue.
    """

    def __init__(self, price_currency: str, billing_currency: str, model_name: str) -> None:
        self.price_currency = price_currency
        self.billing_currency = billing_currency
        super().__init__(
            f"model {model_name!r} is priced in {price_currency} but the gateway bills in "
            f"{billing_currency}; fix the price or set GATEWAY_BILLING_CURRENCY"
        )


@dataclass(frozen=True, slots=True)
class TokenCounts:
    prompt: int = 0
    completion: int = 0
    cached_prompt: int = 0
    reasoning: int = 0

    @property
    def total(self) -> int:
        return self.prompt + self.completion

    @property
    def billable_prompt(self) -> int:
        """Prompt tokens charged at the full input rate."""
        return max(0, self.prompt - self.cached_prompt)

    @classmethod
    def from_usage(cls, usage: dict[str, Any] | None) -> TokenCounts:
        """Read an OpenAI-compatible ``usage`` object.

        Tolerant by necessity: providers disagree on where the cache and
        reasoning breakdowns live, and several omit them entirely.
        """
        if not usage:
            return cls()

        prompt = _as_int(usage.get("prompt_tokens"))
        completion = _as_int(usage.get("completion_tokens"))

        details = usage.get("prompt_tokens_details") or {}
        cached = _as_int(details.get("cached_tokens")) if isinstance(details, dict) else 0
        # Anthropic-style and some proxies use a flat key instead.
        if not cached:
            cached = _as_int(usage.get("cache_read_input_tokens"))

        completion_details = usage.get("completion_tokens_details") or {}
        reasoning = (
            _as_int(completion_details.get("reasoning_tokens"))
            if isinstance(completion_details, dict)
            else 0
        )

        return cls(
            prompt=prompt,
            completion=completion,
            cached_prompt=min(cached, prompt),
            reasoning=reasoning,
        )


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

    @property
    def total(self) -> Decimal:
        return self.input_cost + self.output_cost + self.cache_read_cost

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
    billing_currency: str,
    model_name: str = "",
) -> CostBreakdown:
    """Cost of one request.

    A model with no price yields zero cost rather than an error: an operator who
    has not priced a model yet should still be able to serve it, and the usage row
    records ``price_id = NULL`` so the gap is visible in reporting rather than
    hidden in a rounding.
    """
    if price is None:
        return CostBreakdown.zero(billing_currency)

    if price.currency.upper() != billing_currency.upper():
        raise CurrencyMismatch(price.currency, billing_currency, model_name or "<unknown>")

    input_rate = as_decimal(price.input_per_mtok)
    output_rate = as_decimal(price.output_per_mtok)

    if price.cache_read_per_mtok is not None and counts.cached_prompt:
        cache_rate = as_decimal(price.cache_read_per_mtok)
        input_cost = Decimal(counts.billable_prompt) * input_rate / MILLION
        cache_cost = Decimal(counts.cached_prompt) * cache_rate / MILLION
    else:
        # No cache pricing configured: charge the whole prompt at the input rate.
        input_cost = Decimal(counts.prompt) * input_rate / MILLION
        cache_cost = Decimal(0)

    output_cost = Decimal(counts.completion) * output_rate / MILLION

    return CostBreakdown(
        input_cost=input_cost,
        output_cost=output_cost,
        cache_read_cost=cache_cost,
        currency=price.currency.upper(),
    )
