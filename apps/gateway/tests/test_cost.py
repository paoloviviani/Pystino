"""Cost arithmetic.

Tested as pure functions with exact ``Decimal`` values, deliberately away from the
database: this is where a wrong answer becomes a wrong invoice, so it must be
asserted exactly rather than within a tolerance.
"""

from __future__ import annotations

import uuid
from datetime import timedelta
from decimal import Decimal

from gateway.accounting.cost import (
    MILLION,
    CostBreakdown,
    TokenCounts,
    compute_cost,
    select_price,
)
from gateway.models import ModelPrice
from gateway.types import utcnow


def price(
    *,
    input_rate: str = "1",
    output_rate: str = "2",
    cache_read: str | None = None,
    currency: str = "EUR",
    effective_from: object = None,
) -> ModelPrice:
    return ModelPrice(
        id=uuid.uuid4(),
        model_id=uuid.uuid4(),
        input_per_mtok=Decimal(input_rate),
        output_per_mtok=Decimal(output_rate),
        cache_read_per_mtok=Decimal(cache_read) if cache_read is not None else None,
        currency=currency,
        effective_from=effective_from or utcnow(),
    )


class TestTokenCounts:
    def test_total_is_prompt_plus_completion(self) -> None:
        assert TokenCounts(prompt=10, completion=5).total == 15

    def test_billable_prompt_excludes_cached(self) -> None:
        counts = TokenCounts(prompt=100, completion=0, cached_prompt=40)
        assert counts.billable_prompt == 60

    def test_from_usage_reads_openai_shape(self) -> None:
        counts = TokenCounts.from_usage(
            {
                "prompt_tokens": 30,
                "completion_tokens": 12,
                "total_tokens": 42,
                "prompt_tokens_details": {"cached_tokens": 10},
                "completion_tokens_details": {"reasoning_tokens": 4},
            }
        )
        assert (counts.prompt, counts.completion) == (30, 12)
        assert counts.cached_prompt == 10
        assert counts.reasoning == 4

    def test_from_usage_reads_flat_cache_key(self) -> None:
        """Some providers use a flat key instead of the details object."""
        counts = TokenCounts.from_usage(
            {"prompt_tokens": 50, "completion_tokens": 1, "cache_read_input_tokens": 20}
        )
        assert counts.cached_prompt == 20

    def test_cached_never_exceeds_prompt(self) -> None:
        """A provider reporting nonsense must not produce a negative charge."""
        counts = TokenCounts.from_usage(
            {"prompt_tokens": 10, "completion_tokens": 0, "cache_read_input_tokens": 999}
        )
        assert counts.cached_prompt == 10
        assert counts.billable_prompt == 0

    def test_from_usage_tolerates_missing_and_junk(self) -> None:
        assert TokenCounts.from_usage(None).total == 0
        assert TokenCounts.from_usage({}).total == 0
        assert TokenCounts.from_usage({"prompt_tokens": None}).prompt == 0
        assert TokenCounts.from_usage({"prompt_tokens": "17"}).prompt == 17
        assert TokenCounts.from_usage({"prompt_tokens": -5}).prompt == 0
        assert TokenCounts.from_usage({"prompt_tokens_details": "nope"}).cached_prompt == 0


class TestComputeCost:
    def test_simple_case(self) -> None:
        # 1_000_000 in at 1/M and 500_000 out at 2/M -> 1 + 1 = 2
        breakdown = compute_cost(
            TokenCounts(prompt=1_000_000, completion=500_000),
            price(),
        )
        assert breakdown.input_cost == Decimal(1)
        assert breakdown.output_cost == Decimal(1)
        assert breakdown.total == Decimal(2)

    def test_exact_decimal_no_float_drift(self) -> None:
        """The whole reason money is Numeric: this must be exact."""
        breakdown = compute_cost(
            TokenCounts(prompt=3, completion=7),
            price(input_rate="0.1", output_rate="0.3"),
        )
        expected = (Decimal(3) * Decimal("0.1") + Decimal(7) * Decimal("0.3")) / MILLION
        assert breakdown.total == expected
        # And the value a float would have produced is *not* what we stored.
        assert str(breakdown.total) != str((3 * 0.1 + 7 * 0.3) / 1e6)

    def test_cached_tokens_are_not_double_charged(self) -> None:
        """Cached prompt tokens are a subset of prompt_tokens, not an extra."""
        counts = TokenCounts(prompt=1_000_000, completion=0, cached_prompt=400_000)
        breakdown = compute_cost(counts, price(input_rate="1", cache_read="0.25"))
        # 600k at 1/M + 400k at 0.25/M = 0.6 + 0.1
        assert breakdown.input_cost == Decimal("0.6")
        assert breakdown.cache_read_cost == Decimal("0.1")
        assert breakdown.total == Decimal("0.7")

    def test_without_cache_price_whole_prompt_billed_at_input_rate(self) -> None:
        """The conservative reading when no cache rate is configured."""
        counts = TokenCounts(prompt=1_000_000, completion=0, cached_prompt=400_000)
        breakdown = compute_cost(counts, price(input_rate="1"))
        assert breakdown.input_cost == Decimal(1)
        assert breakdown.cache_read_cost == Decimal(0)

    def test_reasoning_tokens_are_not_billed_twice(self) -> None:
        """Reasoning tokens are already inside completion_tokens."""
        with_reasoning = compute_cost(
            TokenCounts(prompt=0, completion=1_000_000, reasoning=800_000),
            price(output_rate="2"),
        )
        without = compute_cost(
            TokenCounts(prompt=0, completion=1_000_000),
            price(output_rate="2"),
        )
        assert with_reasoning.total == without.total == Decimal(2)

    def test_unpriced_model_costs_zero_rather_than_failing(self) -> None:
        breakdown = compute_cost(TokenCounts(prompt=100, completion=100), None)
        assert breakdown.total == Decimal(0)
        assert breakdown.currency == "EUR"

    def test_a_foreign_price_computes_in_its_own_currency(self) -> None:
        """ADR 0054: the breakdown is native, and conversion is the caller's.

        The refusal this test used to pin kept every community-catalogue model
        out of the ledger; the conversion now happens at the decision points,
        with the rate recorded on the usage row.
        """
        breakdown = compute_cost(
            TokenCounts(prompt=1, completion=1),
            price(currency="USD"),
        )
        assert breakdown.currency == "USD"

    def test_zero_tokens_cost_nothing(self) -> None:
        assert compute_cost(TokenCounts(), price()).total == Decimal(0)

    def test_breakdown_zero_helper(self) -> None:
        assert CostBreakdown.zero("EUR").total == Decimal(0)


class TestSelectPrice:
    def test_picks_most_recent_already_effective(self) -> None:
        now = utcnow()
        old = price(input_rate="1", effective_from=now - timedelta(days=10))
        current = price(input_rate="2", effective_from=now - timedelta(days=1))
        chosen = select_price([old, current], at=now)
        assert chosen is current

    def test_ignores_future_prices(self) -> None:
        """Scheduling a price change must not retroactively apply it."""
        now = utcnow()
        current = price(input_rate="1", effective_from=now - timedelta(days=1))
        future = price(input_rate="9", effective_from=now + timedelta(days=1))
        assert select_price([current, future], at=now) is current

    def test_historical_lookup_returns_the_price_of_the_day(self) -> None:
        """Re-pricing must never rewrite what a past request cost."""
        now = utcnow()
        old = price(input_rate="1", effective_from=now - timedelta(days=10))
        new = price(input_rate="5", effective_from=now - timedelta(days=2))
        assert select_price([old, new], at=now - timedelta(days=5)) is old

    def test_no_applicable_price_returns_none(self) -> None:
        now = utcnow()
        assert select_price([], at=now) is None
        assert select_price([price(effective_from=now + timedelta(days=1))], at=now) is None


class TestPerPagePricing:
    """OCR is charged by the page, which is a unit and not a millionth of one.

    Same shape as per-image pricing (ADR 0030), tested to the same standard:
    this is the arithmetic that turns a document into an invoice line.
    """

    @staticmethod
    def ocr_price(
        *, per_page: str | None, input_rate: str = "0", output_rate: str = "0"
    ) -> ModelPrice:
        return ModelPrice(
            id=uuid.uuid4(),
            model_id=uuid.uuid4(),
            input_per_mtok=Decimal(input_rate),
            output_per_mtok=Decimal(output_rate),
            per_page=Decimal(per_page) if per_page is not None else None,
            currency="EUR",
            effective_from=utcnow(),
        )

    def test_pages_are_charged_at_the_page_rate(self) -> None:
        breakdown = compute_cost(TokenCounts(pages=12), self.ocr_price(per_page="0.001"))
        assert breakdown.page_cost == Decimal("0.012")
        assert breakdown.total == Decimal("0.012")

    def test_a_page_is_not_divided_by_a_million(self) -> None:
        """The failure this test exists for: reusing the token divisor here
        would charge a millionth of the real price and look plausible."""
        breakdown = compute_cost(TokenCounts(pages=1), self.ocr_price(per_page="1"))
        assert breakdown.page_cost == Decimal(1)
        assert breakdown.page_cost != Decimal(1) / MILLION

    def test_an_unpriced_page_charges_nothing_rather_than_guessing(self) -> None:
        """An unpriced model reserves nothing and records zero — visibly, so the
        console's unpriced warning is what catches it, not a number invented
        here."""
        breakdown = compute_cost(TokenCounts(pages=200), self.ocr_price(per_page=None))
        assert breakdown.page_cost == Decimal(0)
        assert breakdown.total == Decimal(0)

    def test_pages_and_tokens_are_both_charged_when_both_are_priced(self) -> None:
        """An OCR model that also returns generated text has incurred both, and
        leaving either out records a real charge as zero."""
        price_row = self.ocr_price(per_page="0.01", input_rate="1", output_rate="2")
        breakdown = compute_cost(
            TokenCounts(prompt=1_000_000, completion=1_000_000, pages=3), price_row
        )
        assert breakdown.page_cost == Decimal("0.03")
        assert breakdown.input_cost == Decimal(1)
        assert breakdown.output_cost == Decimal(2)
        assert breakdown.total == Decimal("3.03")

    def test_a_page_charge_converts_with_everything_else(self) -> None:
        """ADR 0054 scales every component by one rate, so the parts keep
        summing to the total after conversion."""
        breakdown = compute_cost(TokenCounts(pages=10), self.ocr_price(per_page="0.10"))
        converted = breakdown.scaled(Decimal("0.5"), "EUR")
        assert converted.page_cost == Decimal("0.500")
        assert converted.total == converted.page_cost


class TestOcrUsageReader:
    """Its own reader, because the field is named differently *and* means
    something else — the argument the Anthropic reader already makes."""

    def test_reads_the_reported_page_count(self) -> None:
        counts = TokenCounts.from_ocr_usage({"pages_processed": 7, "credits": 3})
        assert counts.pages == 7
        # No tokens on this surface: inventing some would bill one request twice.
        assert counts.total == 0

    def test_falls_back_to_the_local_count_when_nobody_reported_one(self) -> None:
        """Local extraction has no counterparty, so the count comes from the
        document itself."""
        assert TokenCounts.from_ocr_usage(None, pages=4).pages == 4
        assert TokenCounts.from_ocr_usage({}, pages=4).pages == 4

    def test_a_reported_count_wins_over_the_local_one(self) -> None:
        """The counterparty is authoritative about what it charged for."""
        assert TokenCounts.from_ocr_usage({"pages_processed": 9}, pages=2).pages == 9

    def test_no_count_anywhere_is_zero_not_one(self) -> None:
        """Zero pages charges nothing. Defaulting to one would invent a charge
        for a request whose size nobody could establish."""
        assert TokenCounts.from_ocr_usage(None).pages == 0


class TestPerSearchPricing:
    """Provider-side web search is charged per search, on top of tokens.

    The third non-token unit and the awkward one: a page and an image arrive on
    a request that is *about* pages or images, while a search arrives on an
    ordinary completion. Tested to the same standard as the other two, plus the
    two properties that are specific to it — that the rate is per one search
    and not per thousand, and that an unpriced search still leaves a count
    behind (ADR 0058).
    """

    @staticmethod
    def search_price(
        *, per_search: str | None, input_rate: str = "1", output_rate: str = "2"
    ) -> ModelPrice:
        return ModelPrice(
            id=uuid.uuid4(),
            model_id=uuid.uuid4(),
            input_per_mtok=Decimal(input_rate),
            output_per_mtok=Decimal(output_rate),
            per_search=Decimal(per_search) if per_search is not None else None,
            currency="EUR",
            effective_from=utcnow(),
        )

    def test_searches_are_charged_at_the_search_rate(self) -> None:
        # $10 per 1,000 searches, entered as the price of one.
        breakdown = compute_cost(TokenCounts(searches=3), self.search_price(per_search="0.01"))
        assert breakdown.search_cost == Decimal("0.03")

    def test_a_search_is_not_divided_by_a_million(self) -> None:
        breakdown = compute_cost(TokenCounts(searches=1), self.search_price(per_search="1"))
        assert breakdown.search_cost == Decimal(1)
        assert breakdown.search_cost != Decimal(1) / MILLION

    def test_searches_are_charged_on_top_of_the_tokens(self) -> None:
        """The distinguishing property of this unit.

        A page-priced model usually charges nothing per token. A searching chat
        model charges for both, every time, and billing only the tokens is the
        under-invoicing this exists to end.
        """
        breakdown = compute_cost(
            TokenCounts(prompt=1_000_000, completion=1_000_000, searches=5),
            self.search_price(per_search="0.01"),
        )
        assert breakdown.input_cost == Decimal(1)
        assert breakdown.output_cost == Decimal(2)
        assert breakdown.search_cost == Decimal("0.05")
        assert breakdown.total == Decimal("3.05")

    def test_an_unpriced_search_charges_nothing_rather_than_guessing(self) -> None:
        """Same rule as an unpriced model: no invented rate.

        The count still reaches the ledger — see the recorder — which is what
        makes this findable before the provider's invoice arrives rather than
        after.
        """
        breakdown = compute_cost(TokenCounts(searches=40), self.search_price(per_search=None))
        assert breakdown.search_cost == Decimal(0)
        assert breakdown.total == Decimal(0)

    def test_no_searches_costs_nothing_on_a_priced_model(self) -> None:
        """A model that *can* search must not charge for one that did not happen."""
        breakdown = compute_cost(
            TokenCounts(prompt=0, completion=0), self.search_price(per_search="0.01")
        )
        assert breakdown.search_cost == Decimal(0)

    def test_a_search_charge_converts_with_everything_else(self) -> None:
        breakdown = compute_cost(TokenCounts(searches=10), self.search_price(per_search="0.10"))
        converted = breakdown.scaled(Decimal("0.5"), "EUR")
        assert converted.search_cost == Decimal("0.500")
        assert converted.total == converted.search_cost


class TestSearchUsageReader:
    """Where the count comes from, and where it deliberately does not.

    ``usage.server_tool_use.web_search_requests`` — verified against
    Anthropic's live schema on 2026-09-08. Read on every surface that can
    carry it, which is the ``_CACHE_WRITE_KEYS`` case (one quantity, one
    meaning, several places it can appear) and not the prompt-convention case
    this module refuses to generalise.
    """

    def test_the_anthropic_surface_reports_it(self) -> None:
        counts = TokenCounts.from_anthropic_usage(
            {
                "input_tokens": 105,
                "output_tokens": 6039,
                "server_tool_use": {"web_search_requests": 4},
            }
        )
        assert counts.searches == 4
        assert counts.prompt == 105

    def test_a_router_passing_it_through_an_openai_shape_is_read_too(self) -> None:
        """Same key, same meaning. A router that proxies Anthropic reports it here."""
        counts = TokenCounts.from_usage(
            {
                "prompt_tokens": 10,
                "completion_tokens": 20,
                "server_tool_use": {"web_search_requests": 2},
            }
        )
        assert counts.searches == 2

    def test_the_responses_surface_too(self) -> None:
        counts = TokenCounts.from_responses_usage(
            {"input_tokens": 1, "output_tokens": 2, "server_tool_use": {"web_search_requests": 7}}
        )
        assert counts.searches == 7

    def test_no_tool_use_object_means_no_searches(self) -> None:
        assert TokenCounts.from_usage({"prompt_tokens": 1}).searches == 0
        assert TokenCounts.from_anthropic_usage({"input_tokens": 1}).searches == 0

    def test_a_malformed_tool_use_object_is_not_a_crash(self) -> None:
        """Providers send surprising things; a usage row must still be written."""
        assert TokenCounts.from_usage({"server_tool_use": "yes"}).searches == 0
        assert (
            TokenCounts.from_usage({"server_tool_use": {"web_search_requests": None}}).searches == 0
        )
        assert (
            TokenCounts.from_usage({"server_tool_use": {"web_search_requests": -3}}).searches == 0
        )

    def test_an_image_request_keeps_its_units_through_the_reader(self) -> None:
        """The reader used to rebuild the dataclass field by field, so every unit
        added after it was written was silently dropped on this surface."""
        counts = TokenCounts.from_image_usage(
            {"input_tokens": 5, "server_tool_use": {"web_search_requests": 1}}, images=2
        )
        assert counts.images == 2
        assert counts.searches == 1
