"""A price per request for web-search backends.

Ground rule 2 applies with its full weight: a wrong answer here is a wrong
invoice. The properties that carry it:

**Unpriced, zero and priced are three different rows.** A backend nobody priced
bills 0 with no price reference; a backend priced at 0 ("billed out of band")
bills 0 *with* one; a priced backend bills price times requests. A report finds
the first kind by its null reference, so collapsing it into the second would
hide the gap this feature exists to show.

**History is not rewritten.** Prices are effective-dated and append-only: a
search keeps the cost it was billed, and a price dated in the future is ignored
until it takes effect.

**The cost counts like any other cost.** A cost quota sees a search's price in
the reservation and in the settled actuals.
"""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal

import httpx
from conftest import FakeUpstream, Seeded
from gateway.accounting.cost import TokenCounts
from gateway.accounting.search_price import select_search_price
from gateway.models import (
    ApiSurface,
    FXRate,
    LimitMetric,
    LimitRule,
    LimitScope,
    PriceSource,
    SearchPrice,
    UsageRecord,
    UsageStatus,
)
from gateway.quota import to_units
from gateway.routers import _metered
from gateway.routers._metered import ResolvedModel
from gateway.types import utcnow
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from test_admin import as_user, make_admin
from test_own_search_metering import (
    FakeSearchBackend,
    accounting_for,
    fetch,
    load_model,
    principal_and_model,
)
from test_search_surface import add_backend, latest_record, linkup_body, linkup_request
from test_unified_search import set_policy


async def price(
    session: AsyncSession,
    per_request: str,
    *,
    backend: str = "linkup",
    tier: str = "",
    currency: str = "EUR",
    ago: timedelta = timedelta(hours=1),
) -> SearchPrice:
    row = SearchPrice(
        backend=backend,
        tier=tier,
        per_request=Decimal(per_request),
        currency=currency,
        effective_from=utcnow() - ago,
        source=PriceSource.MANUAL,
    )
    session.add(row)
    await session.commit()
    return row


async def search(
    client: httpx.AsyncClient, seeded: Seeded, fake_upstream: FakeUpstream
) -> httpx.Response:
    fake_upstream.set_json(linkup_body())
    return await client.post("/v1/search/linkup", json=linkup_request(), headers=seeded.auth)


class TestPricingAtRequestTime:
    async def test_a_priced_search_records_its_cost_and_the_price_it_came_from(
        self,
        client: httpx.AsyncClient,
        seeded: Seeded,
        session: AsyncSession,
        fake_upstream: FakeUpstream,
    ) -> None:
        await add_backend(session, seeded)
        row = await price(session, "0.007")

        assert (await search(client, seeded, fake_upstream)).status_code == 200

        record = await latest_record(session)
        assert record.own_search_requests == 1
        assert record.cost == Decimal("0.007")
        assert record.computed_cost == Decimal("0.007")
        assert record.cost_native == Decimal("0.007")
        assert record.cost_currency == "EUR"
        assert record.currency == "EUR"
        assert record.search_price_id == row.id

    async def test_the_unified_route_prices_the_same_way(
        self,
        client: httpx.AsyncClient,
        seeded: Seeded,
        session: AsyncSession,
        fake_upstream: FakeUpstream,
    ) -> None:
        anchor = await add_backend(session, seeded)
        await set_policy(session, seeded.group.id, anchor)
        row = await price(session, "0.004")
        fake_upstream.set_json(linkup_body(count=7))

        response = await client.post("/v1/search", json={"query": "x"}, headers=seeded.auth)
        assert response.status_code == 200

        # Seven results, one request: the result count is not in the arithmetic.
        record = await latest_record(session)
        assert record.cost == Decimal("0.004")
        assert record.search_price_id == row.id

    async def test_a_failed_search_is_still_billed_because_it_still_counts(
        self,
        client: httpx.AsyncClient,
        seeded: Seeded,
        session: AsyncSession,
        fake_upstream: FakeUpstream,
    ) -> None:
        """The count survives a vendor error (the ceiling must not be raisable
        by making the backend fail), so the cost does too."""
        await add_backend(session, seeded)
        await price(session, "0.007")
        fake_upstream.set_json({"error": {"message": "boom"}}, status=500)

        response = await client.post(
            "/v1/search/linkup", json=linkup_request(), headers=seeded.auth
        )
        assert response.status_code == 500

        record = await latest_record(session)
        assert record.status is UsageStatus.UPSTREAM_ERROR
        assert record.cost == Decimal("0.007")

    async def test_unpriced_is_zero_with_no_price_reference(
        self,
        client: httpx.AsyncClient,
        seeded: Seeded,
        session: AsyncSession,
        fake_upstream: FakeUpstream,
    ) -> None:
        await add_backend(session, seeded)

        assert (await search(client, seeded, fake_upstream)).status_code == 200

        record = await latest_record(session)
        assert record.own_search_requests == 1
        assert record.cost == Decimal(0)
        assert record.search_price_id is None

    async def test_a_zero_price_is_zero_with_a_price_reference(
        self,
        client: httpx.AsyncClient,
        seeded: Seeded,
        session: AsyncSession,
        fake_upstream: FakeUpstream,
    ) -> None:
        """ "Billed out of band" is a decision on record, not a gap."""
        await add_backend(session, seeded)
        row = await price(session, "0")

        assert (await search(client, seeded, fake_upstream)).status_code == 200

        record = await latest_record(session)
        assert record.cost == Decimal(0)
        assert record.search_price_id == row.id
        assert record.cost_native == Decimal(0)
        assert record.cost_currency == "EUR"

    async def test_another_backends_price_does_not_apply(
        self,
        client: httpx.AsyncClient,
        seeded: Seeded,
        session: AsyncSession,
        fake_upstream: FakeUpstream,
    ) -> None:
        await add_backend(session, seeded)
        await price(session, "0.5", backend="exa")

        assert (await search(client, seeded, fake_upstream)).status_code == 200

        record = await latest_record(session)
        assert record.cost == Decimal(0)
        assert record.search_price_id is None


class TestEffectiveDating:
    async def test_a_price_change_never_rewrites_an_earlier_search(
        self,
        client: httpx.AsyncClient,
        seeded: Seeded,
        session: AsyncSession,
        fake_upstream: FakeUpstream,
    ) -> None:
        await add_backend(session, seeded)
        old = await price(session, "0.010", ago=timedelta(days=2))
        assert (await search(client, seeded, fake_upstream)).status_code == 200
        first = await latest_record(session)

        new = await price(session, "0.020", ago=timedelta(minutes=1))
        assert (await search(client, seeded, fake_upstream)).status_code == 200

        rows = (
            (await session.execute(select(UsageRecord).order_by(UsageRecord.created_at)))
            .scalars()
            .all()
        )
        assert [(r.cost, r.search_price_id) for r in rows] == [
            (Decimal("0.010"), old.id),
            (Decimal("0.020"), new.id),
        ]
        # And the first row is the same row it was before the change.
        await session.refresh(first)
        assert first.cost == Decimal("0.010")

    async def test_a_price_dated_in_the_future_is_not_applied_yet(
        self,
        client: httpx.AsyncClient,
        seeded: Seeded,
        session: AsyncSession,
        fake_upstream: FakeUpstream,
    ) -> None:
        await add_backend(session, seeded)
        now_row = await price(session, "0.010", ago=timedelta(days=1))
        await price(session, "9", ago=timedelta(days=-1))

        assert (await search(client, seeded, fake_upstream)).status_code == 200

        record = await latest_record(session)
        assert record.cost == Decimal("0.010")
        assert record.search_price_id == now_row.id

    def test_a_tier_override_wins_and_a_tier_without_one_takes_the_backends_price(self) -> None:
        moment = utcnow()
        backend_wide = SearchPrice(
            backend="linkup",
            tier="",
            per_request=Decimal("0.005"),
            currency="EUR",
            effective_from=moment - timedelta(days=1),
        )
        deep = SearchPrice(
            backend="linkup",
            tier="deep",
            per_request=Decimal("0.05"),
            currency="EUR",
            effective_from=moment - timedelta(days=1),
        )
        rows = [backend_wide, deep]
        assert select_search_price(rows, tier="deep", at=moment) is deep
        assert select_search_price(rows, tier="flash", at=moment) is backend_wide
        assert select_search_price(rows, tier=None, at=moment) is backend_wide
        assert select_search_price([deep], tier=None, at=moment) is None

    async def test_a_tier_override_prices_a_search_that_reports_the_tier(
        self,
        session: AsyncSession,
        session_factory: async_sessionmaker[AsyncSession],
        settings: object,
        seeded: Seeded,
    ) -> None:
        from gateway.config import Settings

        assert isinstance(settings, Settings)
        model = await load_model(session, seeded.model.id)
        await price(session, "0.005", backend="linkup")
        deep = await price(session, "0.050", backend="linkup", tier="deep")

        accounting = accounting_for(
            session_factory=session_factory, settings=settings, seeded=seeded, model=model
        )
        record_id = await accounting.begin()
        FakeSearchBackend(name="linkup", tier="deep").search(accounting, times=2)
        await accounting.finalise(status=UsageStatus.COMPLETED)

        row = await fetch(session_factory, record_id)
        assert row.cost == Decimal("0.100")
        assert row.search_price_id == deep.id


class TestCurrency:
    async def test_a_price_in_another_currency_converts_at_the_days_rate(
        self,
        client: httpx.AsyncClient,
        seeded: Seeded,
        session: AsyncSession,
        fake_upstream: FakeUpstream,
    ) -> None:
        await add_backend(session, seeded)
        row = await price(session, "0.010", currency="USD")
        # Seeded, not fetched: 1 USD = 0.5 EUR exactly, and no network.
        session.add(FXRate(base="USD", quote="EUR", rate=Decimal("0.5"), fetched_at=utcnow()))
        await session.commit()

        assert (await search(client, seeded, fake_upstream)).status_code == 200

        record = await latest_record(session)
        assert record.currency == "EUR"
        assert record.cost == Decimal("0.005")
        assert record.cost_native == Decimal("0.010")
        assert record.cost_currency == "USD"
        assert record.cost_fx_rate == Decimal("0.5")
        assert record.computed_cost == Decimal("0.005")
        assert record.search_price_id == row.id

    async def test_no_rate_means_unpriced_not_a_guessed_price(
        self,
        client: httpx.AsyncClient,
        seeded: Seeded,
        session: AsyncSession,
        fake_upstream: FakeUpstream,
    ) -> None:
        await add_backend(session, seeded)
        await price(session, "0.010", currency="ZZZ")

        assert (await search(client, seeded, fake_upstream)).status_code == 200

        record = await latest_record(session)
        assert record.cost == Decimal(0)
        assert record.search_price_id is None


class TestWhereItShows:
    async def test_the_request_log_and_the_report_show_the_search_cost_not_zero(
        self,
        app: object,
        client: httpx.AsyncClient,
        seeded: Seeded,
        session: AsyncSession,
        session_factory: async_sessionmaker[AsyncSession],
        fake_upstream: FakeUpstream,
    ) -> None:
        await add_backend(session, seeded)
        await price(session, "0.007")
        assert (await search(client, seeded, fake_upstream)).status_code == 200
        as_user(app, await make_admin(session_factory, seeded))

        logged = (await client.get("/api/admin/requests")).json()["items"][0]
        assert Decimal(logged["cost"]) == Decimal("0.007")

        report = (await client.get("/api/admin/reports/usage")).json()
        assert Decimal(report["totals"]["cost"]) == Decimal("0.007")
        assert report["totals"]["own_searches"] == 1
        assert report["totals"]["unpriced_own_searches"] == 0


class TestQuotas:
    async def _cost_rule(self, session: AsyncSession, seeded: Seeded, limit: str) -> None:
        session.add(
            LimitRule(
                name="spend",
                scope=LimitScope.GROUP,
                scope_id=seeded.group.id,
                metric=LimitMetric.COST,
                window_seconds=3600,
                limit_value=Decimal(limit),
            )
        )
        await session.commit()

    async def test_a_cost_rule_counts_what_searches_cost(
        self,
        client: httpx.AsyncClient,
        seeded: Seeded,
        session: AsyncSession,
        fake_upstream: FakeUpstream,
    ) -> None:
        await add_backend(session, seeded)
        await price(session, "0.006")
        await self._cost_rule(session, seeded, "0.01")

        # A rule refuses *at* its limit: 0.006 and then 0.012 are both below it
        # when admitted, and the third finds the window spent.
        for _ in range(2):
            assert (await search(client, seeded, fake_upstream)).status_code == 200
        calls = len(fake_upstream.bodies)
        third = await search(client, seeded, fake_upstream)
        assert third.status_code == 429
        assert len(fake_upstream.bodies) == calls

    async def test_an_unpriced_search_spends_none_of_a_cost_rule(
        self,
        client: httpx.AsyncClient,
        seeded: Seeded,
        session: AsyncSession,
        fake_upstream: FakeUpstream,
    ) -> None:
        await add_backend(session, seeded)
        await self._cost_rule(session, seeded, "0.01")

        for _ in range(3):
            assert (await search(client, seeded, fake_upstream)).status_code == 200

    async def test_the_reservation_carries_the_price_of_the_searches_it_may_run(
        self,
        app: object,
        seeded: Seeded,
        session: AsyncSession,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        await add_backend(session, seeded)
        await price(session, "0.006")
        await self._cost_rule(session, seeded, "1")
        state = app.state  # type: ignore[attr-defined]

        async with session_factory() as db:
            principal, model = await principal_and_model(db, seeded)
            metered = await _metered.begin(
                fx=state.fx,
                session_factory=session_factory,
                session=db,
                principal=principal,
                settings=state.settings,
                quota=state.quota_engine,
                estimator=state.token_estimator,
                resolved=ResolvedModel(model=model, requested_name=model.name),
                surface=ApiSurface.SEARCH,
                request_id="req-search-price-reserve",
                worst_case=TokenCounts(),
                worst_case_own_searches=2,
                search_backend="linkup",
            )
            assert isinstance(metered, _metered.Metered)
            assert metered.reservation.reserved[LimitMetric.COST] == to_units(
                LimitMetric.COST, Decimal("0.012")
            )


class TestAdminPrices:
    async def _admin(
        self,
        app: object,
        client: httpx.AsyncClient,
        seeded: Seeded,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> httpx.AsyncClient:
        as_user(app, await make_admin(session_factory, seeded))
        return client

    async def test_an_admin_sets_a_price_and_sees_it_in_force_with_its_history(
        self,
        app: object,
        client: httpx.AsyncClient,
        seeded: Seeded,
        session: AsyncSession,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        await add_backend(session, seeded)
        admin = await self._admin(app, client, seeded, session_factory)

        created = await admin.post(
            "/api/admin/search-prices",
            json={
                "backend": "linkup",
                "per_request": "0.005",
                "effective_from": (utcnow() - timedelta(days=2)).isoformat(),
            },
        )
        assert created.status_code == 201, created.text
        # The billing currency when none is named.
        assert created.json()["currency"] == "EUR"
        assert created.json()["tier"] is None
        newer = await admin.post(
            "/api/admin/search-prices", json={"backend": "linkup", "per_request": "0"}
        )
        assert newer.status_code == 201

        history = (await admin.get("/api/admin/search-prices?backend=linkup")).json()["items"]
        assert [(h["per_request"], h["in_force"]) for h in history] == [
            ("0.000000000000", True),
            ("0.005000000000", False),
        ]

    async def test_a_price_for_an_unknown_backend_is_refused(
        self,
        app: object,
        client: httpx.AsyncClient,
        seeded: Seeded,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        admin = await self._admin(app, client, seeded, session_factory)
        response = await admin.post(
            "/api/admin/search-prices", json={"backend": "linkpu", "per_request": "0.005"}
        )
        assert response.status_code == 400
        assert response.json()["error"]["code"] == "unknown_search_backend"

    async def test_a_negative_price_is_refused(
        self,
        app: object,
        client: httpx.AsyncClient,
        seeded: Seeded,
        session: AsyncSession,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        await add_backend(session, seeded)
        admin = await self._admin(app, client, seeded, session_factory)
        response = await admin.post(
            "/api/admin/search-prices", json={"backend": "linkup", "per_request": "-1"}
        )
        assert response.status_code == 400

    async def test_the_same_effective_moment_twice_is_a_conflict_not_an_overwrite(
        self,
        app: object,
        client: httpx.AsyncClient,
        seeded: Seeded,
        session: AsyncSession,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        await add_backend(session, seeded)
        admin = await self._admin(app, client, seeded, session_factory)
        body = {
            "backend": "linkup",
            "per_request": "0.005",
            "effective_from": "2026-01-01T00:00:00+00:00",
        }
        assert (await admin.post("/api/admin/search-prices", json=body)).status_code == 201
        assert (await admin.post("/api/admin/search-prices", json=body)).status_code == 409

    async def test_a_non_admin_cannot_set_a_price(
        self,
        app: object,
        client: httpx.AsyncClient,
        seeded: Seeded,
        session: AsyncSession,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        await add_backend(session, seeded)
        as_user(app, await make_admin(session_factory, seeded, admin=False))
        response = await client.post(
            "/api/admin/search-prices", json={"backend": "linkup", "per_request": "0.005"}
        )
        assert response.status_code == 403

    async def test_a_price_set_through_the_api_prices_the_next_search(
        self,
        app: object,
        client: httpx.AsyncClient,
        seeded: Seeded,
        session: AsyncSession,
        session_factory: async_sessionmaker[AsyncSession],
        fake_upstream: FakeUpstream,
    ) -> None:
        await add_backend(session, seeded)
        admin = await self._admin(app, client, seeded, session_factory)
        created = await admin.post(
            "/api/admin/search-prices",
            json={
                "backend": "linkup",
                "per_request": "0.012",
                "effective_from": (utcnow() - timedelta(minutes=1)).isoformat(),
            },
        )
        assert created.status_code == 201

        assert (await search(client, seeded, fake_upstream)).status_code == 200

        record = await latest_record(session)
        assert record.cost == Decimal("0.012")
        assert str(record.search_price_id) == created.json()["id"]
