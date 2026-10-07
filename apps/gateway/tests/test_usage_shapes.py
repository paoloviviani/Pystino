"""Reading each surface's usage object.

Pure functions over the shapes the providers actually send, tested apart from
the routes because this is where a wrong answer becomes a wrong invoice rather
than an error anybody would notice.

The load-bearing case is Anthropic's inverted prompt convention. Everything
else here is naming.
"""

from __future__ import annotations

from decimal import Decimal

import pytest
from gateway.accounting.cost import CostBreakdown, TokenCounts, compute_cost
from gateway.models import ModelPrice


def price(**kwargs: object) -> ModelPrice:
    defaults: dict[str, object] = {
        "input_per_mtok": Decimal("1"),
        "output_per_mtok": Decimal("2"),
        "currency": "EUR",
    }
    defaults.update(kwargs)
    return ModelPrice(**defaults)  # type: ignore[arg-type]


class TestChatCompletions:
    def test_reads_the_openai_names(self) -> None:
        counts = TokenCounts.from_usage(
            {"prompt_tokens": 100, "completion_tokens": 30, "total_tokens": 130}
        )
        assert (counts.prompt, counts.completion) == (100, 30)

    def test_cached_tokens_are_inside_the_prompt(self) -> None:
        """OpenAI's convention: `prompt_tokens` includes the cached ones."""
        counts = TokenCounts.from_usage(
            {
                "prompt_tokens": 100,
                "completion_tokens": 0,
                "prompt_tokens_details": {"cached_tokens": 40},
            }
        )
        assert counts.prompt == 100
        assert counts.cached_prompt == 40
        assert counts.billable_prompt == 60


class TestResponses:
    def test_reads_the_responses_names(self) -> None:
        counts = TokenCounts.from_responses_usage(
            {"input_tokens": 100, "output_tokens": 30, "total_tokens": 130}
        )
        assert (counts.prompt, counts.completion) == (100, 30)

    def test_the_detail_objects_are_optional(self) -> None:
        """The reference provider returns the three totals flat and nothing else."""
        counts = TokenCounts.from_responses_usage({"input_tokens": 7, "output_tokens": 2})
        assert (counts.cached_prompt, counts.reasoning) == (0, 0)

    def test_reasoning_and_cache_details_are_read_when_present(self) -> None:
        counts = TokenCounts.from_responses_usage(
            {
                "input_tokens": 100,
                "output_tokens": 50,
                "input_tokens_details": {"cached_tokens": 25},
                "output_tokens_details": {"reasoning_tokens": 40},
            }
        )
        assert counts.cached_prompt == 25
        assert counts.reasoning == 40
        # Reasoning is a breakdown of the completion, not an addition to it.
        assert counts.completion == 50


class TestAnthropic:
    def test_the_prompt_is_the_sum_of_three_slices(self) -> None:
        """The whole reason this reader exists.

        Anthropic's `input_tokens` is the *uncached remainder*. Reading it as
        the prompt — the way every other surface works — understates the
        prompt by whatever was cached.
        """
        counts = TokenCounts.from_anthropic_usage(
            {
                "input_tokens": 600,
                "cache_creation_input_tokens": 100,
                "cache_read_input_tokens": 300,
                "output_tokens": 50,
            }
        )
        assert counts.prompt == 1000
        assert counts.cached_prompt == 300
        assert counts.cache_write == 100
        # 1000 - 300 read - 100 written
        assert counts.billable_prompt == 600

    def test_the_openai_reader_would_have_undercharged(self) -> None:
        """Pinned as a regression: the two conventions are opposites.

        Not a test of `from_usage` being wrong — it is right for its own
        surface. It is a test that using it here would be, and by how much.
        """
        usage = {
            "input_tokens": 600,
            "cache_read_input_tokens": 300,
            "output_tokens": 50,
        }
        correct = TokenCounts.from_anthropic_usage(usage)
        # `from_usage` finds no `prompt_tokens`, so it reads a prompt of zero
        # and clamps the cache read to it.
        wrong = TokenCounts.from_usage(usage)
        assert correct.billable_prompt == 600
        assert wrong.billable_prompt == 0

    def test_an_uncached_request_reads_straight_through(self) -> None:
        counts = TokenCounts.from_anthropic_usage({"input_tokens": 42, "output_tokens": 7})
        assert (counts.prompt, counts.completion, counts.cached_prompt) == (42, 7, 0)


class TestCachePricing:
    def test_each_slice_is_billed_at_its_own_rate(self) -> None:
        counts = TokenCounts(
            prompt=1_000_000, completion=0, cached_prompt=300_000, cache_write=100_000
        )
        breakdown = compute_cost(
            counts,
            price(
                input_per_mtok=Decimal("10"),
                cache_read_per_mtok=Decimal("1"),
                cache_write_per_mtok=Decimal("12.5"),
            ),
        )
        # 600k at 10, 300k at 1, 100k at 12.5
        assert breakdown.input_cost == Decimal("6")
        assert breakdown.cache_read_cost == Decimal("0.3")
        assert breakdown.cache_write_cost == Decimal("1.25")
        assert breakdown.total == Decimal("7.55")

    def test_an_unpriced_slice_falls_back_to_the_input_rate(self) -> None:
        """Never billed at zero.

        Charging nothing for tokens the provider charged us for is the more
        expensive mistake, and it stays invisible until the invoice lands.
        """
        counts = TokenCounts(prompt=1_000_000, cached_prompt=300_000, cache_write=100_000)
        breakdown = compute_cost(counts, price(input_per_mtok=Decimal("10")))
        assert breakdown.cache_read_cost == Decimal(0)
        assert breakdown.cache_write_cost == Decimal(0)
        assert breakdown.input_cost == Decimal("10")

    def test_the_three_slices_never_overlap(self) -> None:
        """A cached token must not also be billed as an uncached one."""
        counts = TokenCounts(prompt=100, cached_prompt=40, cache_write=10)
        assert counts.billable_prompt + counts.cached_prompt + counts.cache_write == 100


class TestImagePricing:
    def test_a_per_image_model_charges_by_the_picture(self) -> None:
        counts = TokenCounts.from_image_usage(None, images=3)
        breakdown = compute_cost(counts, price(per_image=Decimal("0.04")))
        assert breakdown.image_cost == Decimal("0.12")
        assert breakdown.total == Decimal("0.12")

    def test_a_token_priced_image_model_charges_by_the_token(self) -> None:
        counts = TokenCounts.from_image_usage(
            {"input_tokens": 1_000_000, "output_tokens": 0}, images=2
        )
        breakdown = compute_cost(counts, price(input_per_mtok=Decimal("5")))
        assert breakdown.input_cost == Decimal("5")
        assert breakdown.image_cost == Decimal(0)

    def test_a_model_priced_both_ways_charges_both(self) -> None:
        """Not a contradiction: a provider can meter the prompt and the picture."""
        counts = TokenCounts.from_image_usage({"input_tokens": 1_000_000}, images=2)
        breakdown = compute_cost(
            counts,
            price(input_per_mtok=Decimal("5"), per_image=Decimal("0.01")),
        )
        assert breakdown.total == Decimal("5.02")

    def test_an_unpriced_image_model_costs_zero_rather_than_failing(self) -> None:
        counts = TokenCounts.from_image_usage(None, images=4)
        assert compute_cost(counts, None) == CostBreakdown.zero("EUR")

    def test_images_are_not_tokens(self) -> None:
        """`total` counts tokens; four pictures and no tokens is a total of zero."""
        assert TokenCounts.from_image_usage(None, images=4).total == 0


@pytest.mark.parametrize(
    "reader",
    [TokenCounts.from_usage, TokenCounts.from_responses_usage, TokenCounts.from_anthropic_usage],
)
def test_every_reader_tolerates_nothing(reader: object) -> None:
    assert reader(None) == TokenCounts()  # type: ignore[operator]
    assert reader({}) == TokenCounts()  # type: ignore[operator]
