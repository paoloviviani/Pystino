"""Catalogue parsing and price import.

Prices are the input to every cost calculation, so a parser that quietly reads a
number wrong is as damaging as broken arithmetic. The envelope shape is treated as
uncertain by design — see the note in ``gateway.pricing``.
"""

from __future__ import annotations

from decimal import Decimal

from conftest import Seeded
from gateway.models import ModelDef, ModelPrice, PriceSource
from gateway.pricing import CataloguePrice, import_prices, parse_catalogue
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession


def catalogue_entry(
    model_id: str = "upstream/test-model",
    *,
    input_rate: str = "0.5",
    output_rate: str = "1.5",
    currency: str = "EUR",
    **extra: object,
) -> dict:
    entry: dict = {
        "id": model_id,
        "pricing": {
            "input_token": input_rate,
            "output_token": output_rate,
            "currency": currency,
        },
    }
    entry["pricing"].update(extra)
    return entry


class TestParseCatalogue:
    def test_reads_the_documented_shape(self) -> None:
        prices, unparsable = parse_catalogue({"data": [catalogue_entry()]})
        assert not unparsable
        assert len(prices) == 1
        assert prices[0].model_id == "upstream/test-model"
        assert prices[0].input_per_mtok == Decimal("0.5")
        assert prices[0].output_per_mtok == Decimal("1.5")
        assert prices[0].currency == "EUR"

    def test_accepts_a_bare_list(self) -> None:
        prices, _ = parse_catalogue([catalogue_entry()])
        assert len(prices) == 1

    def test_accepts_a_models_envelope(self) -> None:
        prices, _ = parse_catalogue({"models": [catalogue_entry()]})
        assert len(prices) == 1

    def test_accepts_flat_pricing_fields(self) -> None:
        prices, _ = parse_catalogue(
            [{"id": "m", "input_token": "1", "output_token": "2", "currency": "EUR"}]
        )
        assert len(prices) == 1
        assert prices[0].input_per_mtok == Decimal(1)

    def test_reads_cache_prices_when_present(self) -> None:
        prices, _ = parse_catalogue(
            {"data": [catalogue_entry(cache_read_cost="0.05", cache_write_cost="0.6")]}
        )
        assert prices[0].cache_read_per_mtok == Decimal("0.05")
        assert prices[0].cache_write_per_mtok == Decimal("0.6")

    def test_absent_cache_prices_are_none_not_zero(self) -> None:
        """Zero would mean "cached tokens are free", which is a different claim."""
        prices, _ = parse_catalogue({"data": [catalogue_entry()]})
        assert prices[0].cache_read_per_mtok is None

    def test_prices_never_go_through_float(self) -> None:
        prices, _ = parse_catalogue({"data": [catalogue_entry(input_rate="0.15")]})
        # Decimal("0.15"), not Decimal(0.15) which is 0.1499999999999999944...
        assert prices[0].input_per_mtok == Decimal("0.15")
        assert str(prices[0].input_per_mtok) == "0.15"

    def test_numeric_json_values_are_handled(self) -> None:
        prices, _ = parse_catalogue(
            {
                "data": [
                    {
                        "id": "m",
                        "pricing": {"input_token": 0.15, "output_token": 0.6, "currency": "EUR"},
                    }
                ]
            }
        )
        assert prices[0].input_per_mtok == Decimal("0.15")

    def test_context_window_is_picked_up(self) -> None:
        entry = catalogue_entry()
        entry["context_length"] = 128_000
        prices, _ = parse_catalogue({"data": [entry]})
        assert prices[0].context_window == 128_000

    def test_the_key_the_real_catalogue_actually_uses(self) -> None:
        """`context_size`, which was missing from the list this reads.

        Every model imported from the reference provider therefore had a null
        context window, and nothing failed — the field is nullable and the
        console renders a blank. Pinned by name so the spelling cannot quietly
        drift back (ADR 0031).
        """
        entry = catalogue_entry()
        entry["context_size"] = 200_000
        prices, _ = parse_catalogue({"data": [entry]})
        assert prices[0].context_window == 200_000


class TestCapabilities:
    """What the catalogue says a model can do (ADR 0031)."""

    def test_modalities_and_features_are_carried_through(self) -> None:
        entry = catalogue_entry()
        entry["input_modalities"] = ["text", "image"]
        entry["output_modalities"] = ["text"]
        entry["supported_features"] = ["tools", "reasoning"]
        prices, _ = parse_catalogue({"data": [entry]})
        assert prices[0].input_modalities == ("image", "text")
        assert prices[0].supported_features == ("reasoning", "tools")

    def test_normalised_so_a_re_import_is_visibly_a_no_op(self) -> None:
        """Sorted, lower-cased, deduplicated.

        Otherwise the same catalogue read twice produces two different values
        and a re-import looks like a change nobody made.
        """
        entry = catalogue_entry()
        entry["supported_features"] = ["Tools", "tools", " REASONING ", ""]
        prices, _ = parse_catalogue({"data": [entry]})
        assert prices[0].supported_features == ("reasoning", "tools")

    def test_an_unknown_feature_is_kept_rather_than_dropped(self) -> None:
        """The provider documents this as an open set.

        Filtering to a vocabulary we compiled today would silently hide
        exactly the capability an operator most wants to hear about — the one
        that is new.
        """
        entry = catalogue_entry()
        entry["supported_features"] = ["tools", "some_feature_added_next_week"]
        prices, _ = parse_catalogue({"data": [entry]})
        assert "some_feature_added_next_week" in prices[0].supported_features

    def test_a_catalogue_that_says_nothing_claims_nothing(self) -> None:
        """Empty means "not stated", and must not be read as "cannot"."""
        prices, _ = parse_catalogue({"data": [catalogue_entry()]})
        assert prices[0].input_modalities == ()
        assert prices[0].supported_features == ()

    def test_a_non_list_does_not_blow_up_the_import(self) -> None:
        entry = catalogue_entry()
        entry["supported_features"] = "tools"
        prices, _ = parse_catalogue({"data": [entry]})
        assert prices[0].supported_features == ()

    def test_unreadable_entries_are_reported_not_dropped(self) -> None:
        """A model that fails to import looks just like a free model."""
        prices, unparsable = parse_catalogue(
            {
                "data": [
                    catalogue_entry("good"),
                    {"id": "no-pricing"},
                    {"pricing": {"input_token": "1", "output_token": "2", "currency": "EUR"}},
                    "not-an-object",
                ]
            }
        )
        assert [price.model_id for price in prices] == ["good"]
        assert len(unparsable) == 3
        assert "no-pricing" in unparsable

    def test_negative_prices_are_rejected(self) -> None:
        _, unparsable = parse_catalogue({"data": [catalogue_entry(input_rate="-1")]})
        assert unparsable

    def test_empty_and_junk_payloads(self) -> None:
        assert parse_catalogue({}) == ([], [])
        assert parse_catalogue([]) == ([], [])
        assert parse_catalogue(None) == ([], [])
        assert parse_catalogue("nonsense") == ([], [])


class TestImportPrices:
    async def test_matches_on_the_upstream_model_name(
        self,
        session: AsyncSession,
        seeded: Seeded,
    ) -> None:
        """The catalogue publishes the upstream id, not our chosen name."""
        report = await import_prices(
            session,
            [CataloguePrice("upstream/test-model", Decimal("2"), Decimal("4"), "EUR")],
            billing_currency="EUR",
            dry_run=False,
        )
        assert report.created == ["test-model"]

        prices = (
            (
                await session.execute(
                    select(ModelPrice).where(ModelPrice.model_id == seeded.model.id)
                )
            )
            .scalars()
            .all()
        )
        assert len(prices) == 2  # the seeded one, plus the imported one
        newest = max(prices, key=lambda price: price.effective_from)
        assert newest.input_per_mtok == Decimal("2")
        assert newest.source is PriceSource.CORTECS

    async def test_dry_run_writes_nothing(self, session: AsyncSession, seeded: Seeded) -> None:
        report = await import_prices(
            session,
            [CataloguePrice("upstream/test-model", Decimal("9"), Decimal("9"), "EUR")],
            billing_currency="EUR",
            dry_run=True,
        )
        assert report.created == ["test-model"]
        prices = (
            (
                await session.execute(
                    select(ModelPrice).where(ModelPrice.model_id == seeded.model.id)
                )
            )
            .scalars()
            .all()
        )
        assert len(prices) == 1

    async def test_unchanged_prices_do_not_append_a_row(
        self, session: AsyncSession, seeded: Seeded
    ) -> None:
        """Otherwise a nightly cron makes the price history unreadable."""
        same = CataloguePrice("upstream/test-model", Decimal("1"), Decimal("2"), "EUR")
        report = await import_prices(session, [same], billing_currency="EUR", dry_run=False)
        assert report.unchanged == ["test-model"]
        assert report.created == []

        prices = (
            (
                await session.execute(
                    select(ModelPrice).where(ModelPrice.model_id == seeded.model.id)
                )
            )
            .scalars()
            .all()
        )
        assert len(prices) == 1

    async def test_history_is_preserved_so_past_costs_stay_correct(
        self, session: AsyncSession, seeded: Seeded
    ) -> None:
        await import_prices(
            session,
            [CataloguePrice("upstream/test-model", Decimal("5"), Decimal("6"), "EUR")],
            billing_currency="EUR",
            dry_run=False,
        )
        prices = (
            (
                await session.execute(
                    select(ModelPrice).where(ModelPrice.model_id == seeded.model.id)
                )
            )
            .scalars()
            .all()
        )
        # The original row is still there, unmodified.
        original = min(prices, key=lambda price: price.effective_from)
        assert original.input_per_mtok == Decimal("1")

    async def test_currency_mismatch_is_refused(
        self, session: AsyncSession, seeded: Seeded
    ) -> None:
        report = await import_prices(
            session,
            [CataloguePrice("upstream/test-model", Decimal("1"), Decimal("2"), "USD")],
            billing_currency="EUR",
            dry_run=False,
        )
        assert report.currency_mismatches == [("upstream/test-model", "USD")]
        assert report.created == []

    async def test_unknown_models_are_not_created(
        self, session: AsyncSession, seeded: Seeded
    ) -> None:
        """Which models exist stays an administrative decision."""
        report = await import_prices(
            session,
            [CataloguePrice("some/other-model", Decimal("1"), Decimal("2"), "EUR")],
            billing_currency="EUR",
            dry_run=False,
        )
        assert report.unknown_models == ["some/other-model"]
        models = (await session.execute(select(ModelDef))).scalars().all()
        assert {model.name for model in models} == {"test-model"}

    async def test_context_window_is_filled_in_when_missing(
        self, session: AsyncSession, seeded: Seeded
    ) -> None:
        model = (
            await session.execute(select(ModelDef).where(ModelDef.id == seeded.model.id))
        ).scalar_one()
        model.context_window = None
        await session.commit()

        await import_prices(
            session,
            [
                CataloguePrice(
                    "upstream/test-model",
                    Decimal("3"),
                    Decimal("4"),
                    "EUR",
                    context_window=64_000,
                )
            ],
            billing_currency="EUR",
            dry_run=False,
        )
        refreshed = (
            await session.execute(select(ModelDef).where(ModelDef.id == seeded.model.id))
        ).scalar_one()
        assert refreshed.context_window == 64_000

    async def test_empty_input_is_a_no_op(self, session: AsyncSession) -> None:
        report = await import_prices(session, [], billing_currency="EUR", dry_run=False)
        assert report.summary().startswith("0 price(s) written")
