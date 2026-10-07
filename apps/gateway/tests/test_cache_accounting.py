"""Prompt-cache accounting: reading the slices, and billing them.

Ground rule 3 territory — a wrong answer here is a wrong invoice. The specific
bug these pin down is documented in docs/cache-accounting-findings.md: providers
spell "cache write tokens" at least four different ways, the Chat Completions
reader looked for none of them, and those tokens were therefore billed at the
full input rate. That is an overcharge of roughly 20x on Gemini, whose cache
writes are far *cheaper* than its input, and an undercharge on Anthropic, whose
are dearer. Wrong in both directions depending on the provider.

The payloads below are not invented. They are what the reference provider
actually returned on 2026-08-17, routed to three different backends.
"""

from __future__ import annotations

from decimal import Decimal

import httpx
import pytest
from conftest import FakeUpstream, Seeded
from gateway.accounting.cost import TokenCounts, compute_cost
from gateway.models import (
    GroupModelAccess,
    ModelDef,
    ModelKind,
    ModelPrice,
    Provider,
    UsageRecord,
)
from gateway.plugins.cortecs import CortecsRouterPlugin
from gateway.plugins.generic import GenericOpenAIPlugin
from gateway.reporting import GroupBy, ReportFilter, build_report, resolve_period
from gateway.types import format_money_prose
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession


async def latest_record(session: AsyncSession) -> UsageRecord:
    return (
        (
            await session.execute(
                select(UsageRecord).order_by(UsageRecord.created_at.desc()).limit(1)
            )
        )
        .scalars()
        .one()
    )


def price(
    *,
    input_rate: str = "1",
    output_rate: str = "2",
    cache_read: str | None = None,
    cache_write: str | None = None,
) -> ModelPrice:
    return ModelPrice(
        input_per_mtok=Decimal(input_rate),
        output_per_mtok=Decimal(output_rate),
        cache_read_per_mtok=Decimal(cache_read) if cache_read else None,
        cache_write_per_mtok=Decimal(cache_write) if cache_write else None,
        currency="EUR",
    )


class TestEverySpellingOfCacheWrite:
    """One quantity, four names. Missing any of them bills it at the input rate."""

    @pytest.mark.parametrize(
        "key",
        [
            "cache_write_tokens",  # Nebius; OpenRouter; LiteLLM sees it from deepseek/kimi
            "cache_creation_tokens",  # Nebius sends this too; LiteLLM's normalised name
            "created_cache_tokens",  # vLLM-backed models
            "cache_creation_input_tokens",  # Anthropic's name, on an OpenAI-shaped body
        ],
    )
    def test_the_write_slice_is_found(self, key: str) -> None:
        counts = TokenCounts.from_usage(
            {
                "prompt_tokens": 1000,
                "completion_tokens": 10,
                "prompt_tokens_details": {"cached_tokens": 200, key: 300},
            }
        )
        assert counts.cache_write == 300
        assert counts.cached_prompt == 200
        # The three slices are disjoint subsets of the prompt.
        assert counts.billable_prompt == 500

    def test_two_names_for_the_same_slice_are_not_added_up(self) -> None:
        """Nebius returns `cache_write_tokens` *and* `cache_creation_tokens`.

        Summing them would bill the same tokens twice, and the arithmetic would
        look plausible right up to the invoice.
        """
        counts = TokenCounts.from_usage(
            {
                "prompt_tokens": 1000,
                "completion_tokens": 1,
                "prompt_tokens_details": {
                    "cached_tokens": 0,
                    "cache_write_tokens": 400,
                    "cache_creation_tokens": 400,
                },
            }
        )
        assert counts.cache_write == 400

    def test_a_flattened_usage_object_still_works(self) -> None:
        """Proxies hoist the details to the top level."""
        counts = TokenCounts.from_usage(
            {"prompt_tokens": 900, "completion_tokens": 5, "cache_write_tokens": 100}
        )
        assert counts.cache_write == 100

    def test_absent_means_zero_not_a_guess(self) -> None:
        counts = TokenCounts.from_usage({"prompt_tokens": 500, "completion_tokens": 5})
        assert counts.cache_write == 0
        assert counts.billable_prompt == 500


class TestClamping:
    """The slices must never exceed the prompt they are slices of."""

    def test_a_write_larger_than_the_prompt_is_clamped(self) -> None:
        counts = TokenCounts.from_usage(
            {
                "prompt_tokens": 100,
                "completion_tokens": 1,
                "prompt_tokens_details": {"cache_write_tokens": 999},
            }
        )
        assert counts.cache_write == 100
        assert counts.billable_prompt == 0

    def test_the_read_wins_when_the_two_would_overflow(self) -> None:
        """`cached_tokens` is the standardised field, so it is the trusted one.

        Without this, `billable_prompt` would floor at zero and the input charge
        would silently vanish for the whole request.
        """
        counts = TokenCounts.from_usage(
            {
                "prompt_tokens": 1000,
                "completion_tokens": 1,
                "prompt_tokens_details": {"cached_tokens": 900, "cache_write_tokens": 500},
            }
        )
        assert counts.cached_prompt == 900
        assert counts.cache_write == 100
        assert counts.billable_prompt == 0

    def test_billable_prompt_is_never_negative(self) -> None:
        counts = TokenCounts(prompt=10, cached_prompt=8, cache_write=8)
        assert counts.billable_prompt == 0


class TestTheObservedPayloads:
    """Verbatim from the live API, so a provider change breaks a test."""

    def test_a_vllm_backed_cache_miss_then_hit(self) -> None:
        first = TokenCounts.from_usage(
            {
                "completion_tokens": 2,
                "prompt_tokens": 1152,
                "total_tokens": 1154,
                "prompt_tokens_details": {"cached_tokens": 0, "created_cache_tokens": 1024},
            }
        )
        assert (first.cached_prompt, first.cache_write) == (0, 1024)
        assert first.billable_prompt == 128

        second = TokenCounts.from_usage(
            {
                "completion_tokens": 2,
                "prompt_tokens": 1152,
                "total_tokens": 1154,
                "prompt_tokens_details": {"cached_tokens": 768, "created_cache_tokens": 256},
            }
        )
        assert (second.cached_prompt, second.cache_write) == (768, 256)
        assert second.billable_prompt == 128

    def test_a_nebius_payload_with_both_names_and_extra_modalities(self) -> None:
        counts = TokenCounts.from_usage(
            {
                "completion_tokens": 1,
                "prompt_tokens": 1161,
                "total_tokens": 1162,
                "completion_tokens_details": {
                    "audio_tokens": 0,
                    "reasoning_tokens": 0,
                    "image_tokens": 0,
                },
                "prompt_tokens_details": {
                    "audio_tokens": 0,
                    "cached_tokens": 0,
                    "video_tokens": 0,
                    "cache_write_tokens": 0,
                    "cache_creation_tokens": 0,
                },
            }
        )
        assert (counts.cached_prompt, counts.cache_write) == (0, 0)
        assert counts.billable_prompt == 1161


class TestResponsesSurface:
    def test_it_reads_writes_too(self) -> None:
        """Same convention as Chat Completions, different key names."""
        counts = TokenCounts.from_responses_usage(
            {
                "input_tokens": 800,
                "output_tokens": 20,
                "input_tokens_details": {"cached_tokens": 100, "cache_write_tokens": 200},
            }
        )
        assert (counts.cached_prompt, counts.cache_write) == (100, 200)
        assert counts.billable_prompt == 500


class TestTheTwoConventionsStillDisagree:
    def test_anthropic_adds_its_slices_and_openai_does_not(self) -> None:
        """The distinction the tolerant reader must NOT erase.

        Both bodies describe the same request. Anthropic's `input_tokens`
        excludes the cache slices, so the prompt is the sum; OpenAI's includes
        them. Reading one as the other undercharges by the cached amount.
        """
        anthropic = TokenCounts.from_anthropic_usage(
            {
                "input_tokens": 128,
                "cache_creation_input_tokens": 256,
                "cache_read_input_tokens": 768,
                "output_tokens": 2,
            }
        )
        openai = TokenCounts.from_usage(
            {
                "prompt_tokens": 1152,
                "completion_tokens": 2,
                "prompt_tokens_details": {"cached_tokens": 768, "cache_write_tokens": 256},
            }
        )
        assert anthropic.prompt == openai.prompt == 1152
        assert anthropic.billable_prompt == openai.billable_prompt == 128


class TestBilling:
    def test_a_write_is_charged_at_its_own_rate(self) -> None:
        counts = TokenCounts(prompt=1_000_000, completion=0, cached_prompt=0, cache_write=400_000)
        breakdown = compute_cost(
            counts,
            price(input_rate="10", cache_write="20"),
        )
        # 600k at 10/Mtok + 400k at 20/Mtok
        assert breakdown.input_cost == Decimal(6)
        assert breakdown.cache_write_cost == Decimal(8)
        assert breakdown.total == Decimal(14)

    def test_gemini_shaped_pricing_is_no_longer_a_20x_overcharge(self) -> None:
        """The bug, in the direction nobody expects.

        Gemini prices a cache write far *below* input — 0.034 against 0.673 per
        Mtok. Billing the written slice at the input rate overcharges it ~20x.
        """
        counts = TokenCounts(prompt=1_000_000, completion=0, cache_write=1_000_000)
        correct = compute_cost(counts, price(input_rate="0.673", cache_write="0.034"))
        as_if_unread = compute_cost(
            TokenCounts(prompt=1_000_000, completion=0),
            price(input_rate="0.673", cache_write="0.034"),
        )
        assert correct.total == Decimal("0.034")
        assert as_if_unread.total == Decimal("0.673")
        assert as_if_unread.total / correct.total > 19

    def test_an_unpriced_write_falls_back_to_the_input_rate(self) -> None:
        """Which is what the reference provider itself does.

        Its vLLM-backed models report `created_cache_tokens` and charge them at
        the input rate, because those models have no cache-write price. Billing
        them at zero would give the tokens away.
        """
        counts = TokenCounts(prompt=1_000_000, completion=0, cache_write=400_000)
        breakdown = compute_cost(counts, price(input_rate="10"))
        assert breakdown.cache_write_cost == Decimal(0)
        assert breakdown.input_cost == Decimal(10)

    def test_reads_and_writes_are_priced_separately(self) -> None:
        counts = TokenCounts(
            prompt=1_000_000, completion=0, cached_prompt=500_000, cache_write=200_000
        )
        breakdown = compute_cost(
            counts,
            price(input_rate="10", cache_read="1", cache_write="20"),
        )
        assert breakdown.input_cost == Decimal(3)  # 300k at 10
        assert breakdown.cache_read_cost == Decimal("0.5")  # 500k at 1
        assert breakdown.cache_write_cost == Decimal(4)  # 200k at 20


class TestUpstreamReportedCost:
    """The counterparty's own figure: read by its plugin, never billed from here.

    This moved. It used to be ``read_upstream_cost(usage, unit)``, with the unit
    declared by an operator on the provider row. The unit is the plugin's to know
    (ADR 0032): nothing in a payload says whether ``cost: 136`` is micro-EUR or
    credits, and asking an operator to know was asking them to be the plugin.
    """

    def test_micro_eur_is_scaled(self) -> None:
        # The observed payload: 136 micro-EUR for one small request.
        reported = CortecsRouterPlugin().read_reported_cost({"cost": 136})
        assert reported is not None
        assert (reported.amount, reported.currency) == (Decimal("0.000136"), "EUR")

    def test_it_is_authoritative_which_is_what_makes_it_billable(self) -> None:
        """The flag is the whole difference between recording and billing.

        Reconciled against their listed prices on three sub-providers, so the
        plugin asserts it — which is what lets a deployment select pass-through
        billing against it and nothing else.
        """
        reported = CortecsRouterPlugin().read_reported_cost({"cost": 136})
        assert reported is not None and reported.authoritative is True

    def test_a_nameless_endpoint_reports_nothing(self) -> None:
        """The generic plugin reads no cost at all, whatever the payload says.

        A number a vendor happens to label ``cost`` is not a charge in a known
        unit, and this used to be settleable by typing a unit into a form. A
        wrong unit there is a reconciliation report off by a factor of a million,
        which reads as a provider overcharging rather than as a typo.
        """
        assert GenericOpenAIPlugin().read_reported_cost({"cost": 136}) is None

    def test_a_provider_that_reports_nothing(self) -> None:
        plugin = CortecsRouterPlugin()
        assert plugin.read_reported_cost({"prompt_tokens": 10}) is None
        assert plugin.read_reported_cost(None) is None

    def test_a_float_does_not_import_binary_rounding(self) -> None:
        """Via `str`, so 0.1 is a tenth and not 0.1000000000000000055511151231."""
        reported = CortecsRouterPlugin().read_reported_cost({"cost": 0.1})
        assert reported is not None and reported.amount == Decimal("0.0000001")

    def test_nonsense_is_refused(self) -> None:
        plugin = CortecsRouterPlugin()
        assert plugin.read_reported_cost({"cost": "gratis"}) is None
        assert plugin.read_reported_cost({"cost": -5}) is None
        # `True` is an int in Python; it is not a cost.
        assert plugin.read_reported_cost({"cost": True}) is None

    def test_the_breakdown_is_kept_verbatim(self) -> None:
        """Their shape, not ours, because normalising it would lose the evidence."""
        details = {"prompt_cost": 45, "cache_read_cost": 21, "completion_cost": 1}
        reported = CortecsRouterPlugin().read_reported_cost({"cost": 67, "cost_details": details})
        assert reported is not None and reported.details == details


class TestItReachesTheLedger:
    """Reading and billing correctly is no use if the row does not keep it.

    `cache_write_tokens` was computed and then dropped on the floor: there was
    no column for it, so a cached request could not be explained after the fact
    and the ledger could not be repriced.
    """

    async def test_the_write_count_and_the_reported_cost_are_stored(
        self,
        client: httpx.AsyncClient,
        seeded: Seeded,
        session: AsyncSession,
        fake_upstream: FakeUpstream,
    ) -> None:
        # An explicit UPDATE, not a mutation of `seeded.provider`: that object
        # belongs to the fixture's own session, so assigning to it here would
        # look like it worked and persist nothing.
        #
        # `plugin="cortecs"` is what makes the reported figure readable at all.
        # It used to be `upstream_cost_unit="micro_eur"`, and the fake upstream's
        # payload is the reference router's, so the unit is the same either way.
        await session.execute(
            update(Provider).where(Provider.id == seeded.provider.id).values(plugin="cortecs")
        )
        await session.commit()

        fake_upstream.set_json(
            {
                "id": "c1",
                "object": "chat.completion",
                "choices": [{"index": 0, "message": {"role": "assistant", "content": "ok"}}],
                "usage": {
                    "prompt_tokens": 1152,
                    "completion_tokens": 2,
                    "total_tokens": 1154,
                    "prompt_tokens_details": {"cached_tokens": 768, "created_cache_tokens": 256},
                    "cost": 67,
                    "cost_details": {"prompt_cost": 45, "cache_read_cost": 21},
                },
            }
        )
        response = await client.post(
            "/v1/chat/completions",
            json={"model": seeded.model.name, "messages": [{"role": "user", "content": "hi"}]},
            headers=seeded.auth,
        )
        assert response.status_code == 200

        record = await latest_record(session)
        assert record.cached_prompt_tokens == 768
        assert record.cache_write_tokens == 256
        assert record.upstream_cost == Decimal("0.000067")
        assert record.upstream_cost_currency == "EUR"

    async def test_a_provider_with_no_declared_unit_stores_no_reported_cost(
        self,
        client: httpx.AsyncClient,
        seeded: Seeded,
        session: AsyncSession,
        fake_upstream: FakeUpstream,
    ) -> None:
        """The default. Our own `cost` is unaffected either way."""
        fake_upstream.set_json(
            {
                "id": "c2",
                "object": "chat.completion",
                "choices": [{"index": 0, "message": {"role": "assistant", "content": "ok"}}],
                "usage": {"prompt_tokens": 10, "completion_tokens": 2, "cost": 999999},
            }
        )
        response = await client.post(
            "/v1/chat/completions",
            json={"model": seeded.model.name, "messages": [{"role": "user", "content": "hi"}]},
            headers=seeded.auth,
        )
        assert response.status_code == 200

        record = await latest_record(session)
        assert record.upstream_cost is None
        assert record.upstream_cost_currency is None


class TestFiguresInProse:
    """A figure written into a sentence is rounded to milli-units.

    The ledger's twelve decimal places are real and are kept — in the structured
    field beside the prose, which is what the console reveals when an
    administrator asks for exact figures. A *sentence* carrying
    `0.003849000000 EUR` is a sentence nobody reads, and a report whose
    disclosures are unreadable might as well not have them.
    """

    def test_it_rounds_to_milli_units(self) -> None:
        assert format_money_prose(Decimal("0.003849000000"), "EUR") == "0.004 EUR"
        assert format_money_prose(Decimal("12.3456"), "EUR") == "12.346 EUR"

    def test_it_rounds_half_up_rather_than_truncating(self) -> None:
        """Truncation always understates a bill."""
        assert format_money_prose(Decimal("0.0015"), "EUR") == "0.002 EUR"
        assert format_money_prose(Decimal("0.0014999"), "EUR") == "0.001 EUR"

    def test_a_real_amount_never_rounds_to_nothing(self) -> None:
        """The trap. "It reported 0.000 EUR" would say it charged nothing."""
        assert format_money_prose(Decimal("0.0000004"), "EUR") == "< 0.001 EUR"
        # The inequality flips for a credit: nearer zero than the smallest unit
        # shown is *greater* than minus that unit.
        assert format_money_prose(Decimal("-0.0000001"), "EUR") == "> -0.001 EUR"

    def test_a_true_zero_is_zero(self) -> None:
        assert format_money_prose(Decimal("0"), "EUR") == "0.000 EUR"

    def test_it_carries_the_currency_it_was_given(self) -> None:
        """No conversion, here least of all — this is the reconciliation note."""
        assert format_money_prose(Decimal("2"), "USD") == "2.000 USD"

    def test_a_float_never_touches_it(self) -> None:
        """Via `as_decimal`, so 0.1 is a tenth and not 0.1000000000000000055."""
        assert format_money_prose("0.1", "EUR") == "0.100 EUR"


class TestReconciliationIsPerProvider:
    """The bug: a total is not a comparison.

    Summing every request's cost and weighing it against the sum of *reported*
    costs adds requests whose provider reports a figure to requests whose
    provider does not. The two sides then describe different sets of requests,
    and the result reads as an enormous discrepancy when nothing is wrong — the
    fake upstream's spend against one real provider's invoice.
    """

    async def test_only_comparable_requests_are_compared(
        self,
        client: httpx.AsyncClient,
        seeded: Seeded,
        session: AsyncSession,
        fake_upstream: FakeUpstream,
    ) -> None:
        await session.execute(
            update(Provider).where(Provider.id == seeded.provider.id).values(plugin="cortecs")
        )
        await session.commit()

        # One request the provider priced, and one it did not.
        for reported in (True, False):
            usage: dict[str, object] = {"prompt_tokens": 1000, "completion_tokens": 100}
            if reported:
                usage["cost"] = 500
            fake_upstream.set_json(
                {
                    "id": "c",
                    "object": "chat.completion",
                    "choices": [{"index": 0, "message": {"role": "assistant", "content": "x"}}],
                    "usage": usage,
                }
            )
            assert (
                await client.post(
                    "/v1/chat/completions",
                    json={
                        "model": seeded.model.name,
                        "messages": [{"role": "user", "content": "hi"}],
                    },
                    headers=seeded.auth,
                )
            ).status_code == 200

        report = await build_report(
            session,
            ReportFilter(period=resolve_period("", None, None, "UTC"), group_by=GroupBy.TOTAL),
            currency="EUR",
            timezone="UTC",
        )

        assert len(report.reconciliation) == 1
        drift = report.reconciliation[0]
        assert drift.provider == seeded.provider.name
        # One of the two requests, not both: the unreported one is excluded from
        # *both* sides rather than from one.
        assert drift.requests == 1
        assert drift.upstream_cost == Decimal("0.000500")
        assert drift.cost < report.totals.cost
        # And the misleading figure is gone from the summary entirely.
        assert not hasattr(report.totals, "upstream_cost")

    async def test_a_provider_that_reports_nothing_does_not_appear(
        self,
        client: httpx.AsyncClient,
        seeded: Seeded,
        session: AsyncSession,
        fake_upstream: FakeUpstream,
    ) -> None:
        """No plugin that reads a cost, so there is nothing to reconcile and no row."""
        fake_upstream.set_json(
            {
                "id": "c",
                "object": "chat.completion",
                "choices": [{"index": 0, "message": {"role": "assistant", "content": "x"}}],
                "usage": {"prompt_tokens": 10, "completion_tokens": 2, "cost": 999},
            }
        )
        await client.post(
            "/v1/chat/completions",
            json={"model": seeded.model.name, "messages": [{"role": "user", "content": "hi"}]},
            headers=seeded.auth,
        )
        report = await build_report(
            session,
            ReportFilter(period=resolve_period("", None, None, "UTC"), group_by=GroupBy.TOTAL),
            currency="EUR",
            timezone="UTC",
        )
        assert report.reconciliation == []
        assert not any("reported" in note for note in report.disclosures)

    async def test_the_disclosure_names_the_provider_and_its_scope(
        self,
        client: httpx.AsyncClient,
        seeded: Seeded,
        session: AsyncSession,
        fake_upstream: FakeUpstream,
    ) -> None:
        """Unqualified, the note reads as covering the whole report."""
        await session.execute(
            update(Provider).where(Provider.id == seeded.provider.id).values(plugin="cortecs")
        )
        await session.commit()
        fake_upstream.set_json(
            {
                "id": "c",
                "object": "chat.completion",
                "choices": [{"index": 0, "message": {"role": "assistant", "content": "x"}}],
                "usage": {"prompt_tokens": 10, "completion_tokens": 2, "cost": 42},
            }
        )
        await client.post(
            "/v1/chat/completions",
            json={"model": seeded.model.name, "messages": [{"role": "user", "content": "hi"}]},
            headers=seeded.auth,
        )
        report = await build_report(
            session,
            ReportFilter(period=resolve_period("", None, None, "UTC"), group_by=GroupBy.TOTAL),
            currency="EUR",
            timezone="UTC",
        )
        note = next(n for n in report.disclosures if seeded.provider.name in n)
        assert "not comparable to the totals" in note
        assert "1 request(s)" in note
        # Milli-units in prose, never the ledger's twelve places. This sentence
        # used to read "it reported 0.000042000000 EUR against 0.000041...".
        assert "000000" not in note

    async def test_a_provider_that_bills_in_another_currency_is_reported_in_it(
        self,
        client: httpx.AsyncClient,
        seeded: Seeded,
        session: AsyncSession,
        fake_upstream: FakeUpstream,
    ) -> None:
        """If the upstream bills dollars, the report says dollars.

        Several units are supported and none are converted: an exchange rate
        applied silently produces invoices that look right and are wrong. So the
        reported figure keeps the unit it arrived in, and the grouping is by
        provider *and* currency — summing a dollar figure into a euro total would
        be exactly the arithmetic this refuses to do.
        """
        # One provider reporting micro-EUR, a second reporting dollars, both
        # pointed at the same upstream so only the plugin differs.
        await session.execute(
            update(Provider).where(Provider.id == seeded.provider.id).values(plugin="cortecs")
        )
        dollars = Provider(
            name="dollar-shop",
            base_url=seeded.provider.base_url,
            plugin="dollar-shop",
        )
        session.add(dollars)
        await session.flush()
        model = ModelDef(
            name="priced-in-dollars",
            upstream_model="vendor/x",
            provider_id=dollars.id,
            kind=ModelKind.CHAT,
        )
        session.add(model)
        await session.flush()
        session.add(GroupModelAccess(group_id=seeded.group.id, model_id=model.id))
        await session.commit()

        for name, cost in ((seeded.model.name, 500), (model.name, 2)):
            fake_upstream.set_json(
                {
                    "id": "c",
                    "object": "chat.completion",
                    "choices": [{"index": 0, "message": {"role": "assistant", "content": "x"}}],
                    "usage": {"prompt_tokens": 10, "completion_tokens": 2, "cost": cost},
                }
            )
            assert (
                await client.post(
                    "/v1/chat/completions",
                    json={"model": name, "messages": [{"role": "user", "content": "hi"}]},
                    headers=seeded.auth,
                )
            ).status_code == 200

        report = await build_report(
            session,
            ReportFilter(period=resolve_period("", None, None, "UTC"), group_by=GroupBy.TOTAL),
            currency="EUR",
            timezone="UTC",
        )

        by_provider = {row.provider: row for row in report.reconciliation}
        assert by_provider[seeded.provider.name].upstream_currency == "EUR"
        assert by_provider[seeded.provider.name].upstream_cost == Decimal("0.000500")
        # Two dollars, reported as two dollars — not converted, not folded in.
        assert by_provider["dollar-shop"].upstream_currency == "USD"
        assert by_provider["dollar-shop"].upstream_cost == Decimal(2)

        # And the note says which unit each figure is in, so a reader cannot
        # mistake one for the other.
        notes = " ".join(report.disclosures)
        assert "USD" in notes and "EUR" in notes
