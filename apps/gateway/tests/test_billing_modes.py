"""Two billing modes, and both figures recorded in either (ADR 0032 decision 6).

The invariant that makes either mode safe is that **both figures are always
recorded**: a deployment that defers to the counterparty keeps its own
arithmetic, and one that bills its own prices keeps theirs. A divergence is then
always reconstructable rather than being something nobody can explain later.

Ground rule 3 territory. Every test here is a way the charge could come out
wrong, or right for the wrong reason.
"""

from __future__ import annotations

from decimal import Decimal

import httpx
import pytest
from conftest import FakeUpstream, Seeded
from gateway.models import (
    BillingMode,
    CostSource,
    GroupModelAccess,
    ModelDef,
    ModelKind,
    ModelPrice,
    Provider,
    UsageRecord,
)
from gateway.reporting import GroupBy, ReportFilter, build_report, resolve_period
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession


@pytest.fixture
async def admin_client(
    app: object, client: httpx.AsyncClient, seeded: Seeded, session_factory: object
) -> httpx.AsyncClient:
    from test_admin import as_user, make_admin

    as_user(app, await make_admin(session_factory, seeded))  # type: ignore[arg-type]
    return client


async def latest(session: AsyncSession) -> UsageRecord:
    stmt = select(UsageRecord).order_by(UsageRecord.created_at.desc()).limit(1)
    return (await session.execute(stmt)).scalars().one()


async def configure(
    session: AsyncSession,
    seeded: Seeded,
    *,
    mode: BillingMode,
    plugin: str | None = "cortecs",
) -> None:
    await session.execute(
        update(Provider)
        .where(Provider.id == seeded.provider.id)
        .values(billing_mode=mode, plugin=plugin)
    )
    await session.commit()


def completion(cost: int | None = None) -> dict[str, object]:
    usage: dict[str, object] = {"prompt_tokens": 1_000_000, "completion_tokens": 0}
    if cost is not None:
        usage["cost"] = cost
        usage["cost_details"] = {"prompt_cost": cost, "completion_cost": 0}
    return {
        "id": "c",
        "object": "chat.completion",
        "choices": [{"index": 0, "message": {"role": "assistant", "content": "x"}}],
        "usage": usage,
    }


async def ask(client: httpx.AsyncClient, seeded: Seeded) -> httpx.Response:
    return await client.post(
        "/v1/chat/completions",
        json={"model": seeded.model.name, "messages": [{"role": "user", "content": "hi"}]},
        headers=seeded.auth,
    )


class TestOwnPrices:
    """The default, and what every deployment did before the mode existed."""

    async def test_the_charge_is_our_arithmetic(
        self,
        client: httpx.AsyncClient,
        seeded: Seeded,
        session: AsyncSession,
        fake_upstream: FakeUpstream,
    ) -> None:
        await configure(session, seeded, mode=BillingMode.OWN_PRICES)
        # The seeded price is 1/Mtok input, so a million prompt tokens is 1.00.
        fake_upstream.set_json(completion(cost=999_999))
        assert (await ask(client, seeded)).status_code == 200

        record = await latest(session)
        assert record.cost == Decimal(1)
        assert record.cost_source is CostSource.OWN_PRICES
        # Their figure is kept even though it was not billed from — the whole
        # point of the invariant.
        assert record.upstream_cost == Decimal("0.999999")
        assert record.computed_cost == Decimal(1)

    async def test_their_figure_is_kept_even_when_wildly_different(
        self,
        client: httpx.AsyncClient,
        seeded: Seeded,
        session: AsyncSession,
        fake_upstream: FakeUpstream,
    ) -> None:
        """A divergence must be reconstructable, not silently dropped."""
        await configure(session, seeded, mode=BillingMode.OWN_PRICES)
        fake_upstream.set_json(completion(cost=50))
        await ask(client, seeded)

        record = await latest(session)
        assert record.cost == record.computed_cost == Decimal(1)
        assert record.upstream_cost == Decimal("0.000050")
        assert record.upstream_cost_details == {"prompt_cost": 50, "completion_cost": 0}


class TestProviderReported:
    """Pass-through cost recovery: their figure is the charge."""

    async def test_the_charge_is_their_figure(
        self,
        client: httpx.AsyncClient,
        seeded: Seeded,
        session: AsyncSession,
        fake_upstream: FakeUpstream,
    ) -> None:
        await configure(session, seeded, mode=BillingMode.PROVIDER_REPORTED)
        fake_upstream.set_json(completion(cost=250_000))
        assert (await ask(client, seeded)).status_code == 200

        record = await latest(session)
        assert record.cost == Decimal("0.250000")
        assert record.cost_source is CostSource.PROVIDER_REPORTED
        # And our own arithmetic is still there, as the evidence.
        assert record.computed_cost == Decimal(1)
        assert record.cost != record.computed_cost

    async def test_no_reported_figure_falls_back_and_says_so(
        self,
        client: httpx.AsyncClient,
        seeded: Seeded,
        session: AsyncSession,
        fake_upstream: FakeUpstream,
    ) -> None:
        """The failure this distinction exists to surface.

        Silently falling back would leave a pass-through deployment billing from
        a price table nobody is maintaining, looking exactly like one that works.
        """
        await configure(session, seeded, mode=BillingMode.PROVIDER_REPORTED)
        fake_upstream.set_json(completion(cost=None))
        assert (await ask(client, seeded)).status_code == 200

        record = await latest(session)
        assert record.cost == Decimal(1)
        assert record.cost_source is CostSource.OWN_PRICES_FALLBACK
        assert record.upstream_cost is None

    async def test_the_fallback_is_disclosed_in_reports(
        self,
        client: httpx.AsyncClient,
        seeded: Seeded,
        session: AsyncSession,
        fake_upstream: FakeUpstream,
    ) -> None:
        await configure(session, seeded, mode=BillingMode.PROVIDER_REPORTED)
        fake_upstream.set_json(completion(cost=None))
        await ask(client, seeded)

        report = await build_report(
            session,
            ReportFilter(period=resolve_period("", None, None, "UTC"), group_by=GroupBy.TOTAL),
            currency="EUR",
            timezone="UTC",
        )
        assert any("reported none" in note for note in report.disclosures)

    async def test_a_plugin_that_cannot_claim_authority_is_not_billed_from(
        self,
        client: httpx.AsyncClient,
        seeded: Seeded,
        session: AsyncSession,
        fake_upstream: FakeUpstream,
    ) -> None:
        """A plugin reads a figure but asserts nothing about it.

        Reporting a number is not claiming it is the charge, and that distinction
        is the plugin's to make rather than an operator's. So even in pass-through
        mode a non-authoritative figure must not become the charge. The API
        refuses this configuration; the request path must not trust it either.
        """
        await session.execute(
            update(Provider)
            .where(Provider.id == seeded.provider.id)
            .values(billing_mode=BillingMode.PROVIDER_REPORTED, plugin="indicative")
        )
        await session.commit()

        fake_upstream.set_json(completion(cost=250_000))
        await ask(client, seeded)

        record = await latest(session)
        assert record.cost == Decimal(1)
        assert record.cost_source is CostSource.OWN_PRICES_FALLBACK
        # Read and recorded — just not billed from.
        assert record.upstream_cost == Decimal("0.250000")

    async def test_a_foreign_currency_is_recorded_but_not_billed_from(
        self,
        client: httpx.AsyncClient,
        seeded: Seeded,
        session: AsyncSession,
        fake_upstream: FakeUpstream,
    ) -> None:
        """Reporting dollars is supported; *billing* in them waits on quotas.

        Cost counters sum `cost` across rows, and summing dollars into a euro
        budget is the one arithmetic this refuses. So a foreign figure is kept in
        its own unit and the charge stays ours — recorded as a fallback rather
        than converted.
        """
        dollars = Provider(
            name="dollar-router",
            base_url=seeded.provider.base_url,
            billing_mode=BillingMode.PROVIDER_REPORTED,
            plugin="dollar-shop",
        )
        session.add(dollars)
        await session.flush()
        model = ModelDef(
            name="dollar-model",
            upstream_model="vendor/x",
            provider_id=dollars.id,
            kind=ModelKind.CHAT,
        )
        session.add(model)
        await session.flush()
        session.add(
            ModelPrice(
                model_id=model.id,
                input_per_mtok=Decimal(1),
                output_per_mtok=Decimal(2),
                currency="EUR",
            )
        )
        session.add(GroupModelAccess(group_id=seeded.group.id, model_id=model.id))
        await session.commit()

        fake_upstream.set_json(completion(cost=7))
        response = await client.post(
            "/v1/chat/completions",
            json={"model": "dollar-model", "messages": [{"role": "user", "content": "hi"}]},
            headers=seeded.auth,
        )
        assert response.status_code == 200

        record = await latest(session)
        assert record.currency == "EUR"
        assert record.cost == Decimal(1)
        assert record.cost_source is CostSource.OWN_PRICES_FALLBACK
        assert record.upstream_cost_currency == "USD"


class TestConfiguration:
    async def test_pass_through_needs_a_plugin_that_asserts_authority(
        self, admin_client: httpx.AsyncClient
    ) -> None:
        response = await admin_client.post(
            "/api/admin/providers",
            json={
                "name": "wishful",
                "base_url": "https://x.test/v1",
                "billing_mode": "provider_reported",
            },
        )
        assert response.status_code == 400
        assert "does not assert" in response.text

    async def test_pass_through_is_accepted_with_one_that_does(
        self, admin_client: httpx.AsyncClient
    ) -> None:
        response = await admin_client.post(
            "/api/admin/providers",
            json={
                "name": "passthrough",
                "base_url": "https://x.test/v1",
                "plugin": "cortecs",
                "kind": "router",
                "billing_mode": "provider_reported",
            },
        )
        assert response.status_code == 201
        assert response.json()["billing_mode"] == "provider_reported"

    async def test_unpriced_models_are_counted_for_the_console(
        self, admin_client: httpx.AsyncClient, seeded: Seeded, session: AsyncSession
    ) -> None:
        """An unpriced model reserves nothing, so no cost ceiling ever trips.

        A hole in any mode and a sharp one in pass-through, where the
        counterparty's figure arrives far too late to admit on.
        """
        session.add(
            ModelDef(
                name="unpriced",
                upstream_model="vendor/unpriced",
                provider_id=seeded.provider.id,
                kind=ModelKind.CHAT,
            )
        )
        await session.commit()

        listing = (await admin_client.get("/api/admin/providers?limit=50")).json()["items"]
        mine = next(p for p in listing if p["name"] == seeded.provider.name)
        assert mine["unpriced_model_count"] >= 1
