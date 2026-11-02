"""What an Anthropic reply costs, however it arrives.

Ground rule 2: a wrong answer here is a wrong invoice. The same Anthropic usage
reaches the ledger by two routes (the native ``/v1/messages`` passthrough and
chat completions translated to OpenAI's shape), and the two prompt conventions
are opposite, so the properties asserted are the ones a double count or a
dropped slice would break:

* the token buckets are the same whichever surface carried the usage;
* the prompt is exactly uncached + cache read + cache write, counted once;
* the cost is the sum of each bucket at its own price, thinking tokens being
  part of ``output_tokens`` and therefore billed as output.

Prices are Opus 5.5's from the price table in ``anthropic.yaml`` (USD per Mtok:
input 4, 5-minute write 5, cache read 0.20, output 20).
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any

import pytest
from gateway.accounting.cost import CACHE_WRITE_1H_FACTOR, TokenCounts, compute_cost
from gateway.models import ModelPrice
from gateway.translate.anthropic_chat import usage_to_openai

PRICE = ModelPrice(
    input_per_mtok=Decimal(4),
    output_per_mtok=Decimal(20),
    cache_read_per_mtok=Decimal("0.20"),
    cache_write_per_mtok=Decimal(5),
    currency="USD",
)


def by_hand(*, uncached: int, read: int, write_5m: int, write_1h: int, output: int) -> Decimal:
    return (
        Decimal(uncached) * 4
        + Decimal(read) * Decimal("0.20")
        + Decimal(write_5m) * 5
        + Decimal(write_1h) * 8  # the 1h write price: $8 on Opus 5.5
        + Decimal(output) * 20
    ) / 1_000_000


def usage(uncached: int, read: int, write_5m: int, write_1h: int, output: int) -> dict[str, Any]:
    return {
        "input_tokens": uncached,
        "cache_read_input_tokens": read,
        "cache_creation_input_tokens": write_5m + write_1h,
        "output_tokens": output,
        "cache_creation": {
            "ephemeral_5m_input_tokens": write_5m,
            "ephemeral_1h_input_tokens": write_1h,
        },
    }


GRID = [
    (50, 0, 0, 0, 30),
    (50, 1000, 0, 0, 30),
    (50, 0, 200, 0, 30),
    (50, 1000, 200, 0, 30),
    (7, 0, 0, 3000, 12),
    (7, 4000, 100, 3000, 12),
    (0, 9000, 0, 0, 1),
    (123_456, 654_321, 12_345, 6_789, 4_321),
]


@pytest.mark.parametrize(("uncached", "read", "w5", "w1", "output"), GRID)
class TestTheTwoSurfacesAgree:
    def test_buckets_are_identical_through_both_readers(
        self, uncached: int, read: int, w5: int, w1: int, output: int
    ) -> None:
        raw = usage(uncached, read, w5, w1, output)
        native = TokenCounts.from_anthropic_usage(raw)
        via_chat = TokenCounts.from_usage(usage_to_openai(raw))
        assert via_chat == native

    def test_the_prompt_is_counted_once(
        self, uncached: int, read: int, w5: int, w1: int, output: int
    ) -> None:
        """A double count would show here: if ``prompt_tokens`` already
        included the cache fields and they were added again, or the cache
        fields were left out of it, the prompt would not be this sum."""
        openai = usage_to_openai(usage(uncached, read, w5, w1, output))
        assert openai["prompt_tokens"] == uncached + read + w5 + w1
        counts = TokenCounts.from_usage(openai)
        assert counts.prompt == uncached + read + w5 + w1
        # the three disjoint slices add back up to the prompt, no more no less
        assert counts.billable_prompt + counts.cached_prompt + counts.cache_write == counts.prompt
        assert counts.billable_prompt == uncached

    def test_cost_is_each_bucket_at_its_own_price(
        self, uncached: int, read: int, w5: int, w1: int, output: int
    ) -> None:
        raw = usage(uncached, read, w5, w1, output)
        expected = by_hand(uncached=uncached, read=read, write_5m=w5, write_1h=w1, output=output)
        for counts in (
            TokenCounts.from_anthropic_usage(raw),
            TokenCounts.from_usage(usage_to_openai(raw)),
        ):
            assert compute_cost(counts, PRICE).total == expected


def test_the_one_hour_factor_matches_the_published_prices() -> None:
    """$8 / $5 on Opus 5.5, $20 / $12.50 on Fable 5.1, $2 / $1.25 on Haiku 4.5."""
    assert (
        CACHE_WRITE_1H_FACTOR
        == Decimal(8) / 5
        == Decimal(20) / Decimal("12.5")
        == Decimal(2) / Decimal("1.25")
    )


def test_thinking_tokens_are_billed_once_as_output() -> None:
    """Anthropic counts thinking in ``output_tokens``; the translated usage
    shows them as ``reasoning_tokens`` for visibility, and the cost must not add
    them a second time."""
    raw = usage(10, 0, 0, 0, 1000)
    raw["output_tokens_details"] = {"thinking_tokens": 600}
    openai = usage_to_openai(raw)
    assert openai["completion_tokens"] == 1000
    assert openai["completion_tokens_details"] == {"reasoning_tokens": 600}
    counts = TokenCounts.from_usage(openai)
    assert counts.reasoning == 600
    assert compute_cost(counts, PRICE).total == (Decimal(10) * 4 + Decimal(1000) * 20) / 1_000_000


def test_without_a_cache_price_the_slices_fall_back_to_input_not_to_free() -> None:
    bare = ModelPrice(input_per_mtok=Decimal(4), output_per_mtok=Decimal(20), currency="USD")
    counts = TokenCounts.from_usage(usage_to_openai(usage(50, 1000, 200, 0, 30)))
    assert compute_cost(counts, bare).total == (Decimal(1250) * 4 + Decimal(30) * 20) / 1_000_000


def test_the_wrong_reader_is_visibly_wrong() -> None:
    """The failure this file guards against, demonstrated: Anthropic's usage read
    the OpenAI way drops every cached token from the prompt."""
    raw = usage(50, 1000, 200, 0, 30)
    wrong = TokenCounts.from_usage(raw)  # no prompt_tokens key at all
    assert wrong.prompt == 0
    assert wrong != TokenCounts.from_anthropic_usage(raw)
