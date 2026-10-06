"""A rebuild replaces, and the health view and reconcile act on the same figures.

The regression these pin: a counter that already carried live traffic had the
ledger total *added* to it at the next start, because the rebuild was an INCRBY and
the "seeded" marker that was meant to prevent it was only claimed once a rule
existed. Observed against a live deployment: €0.3798 in Valkey, €0.2832 in the
ledger.
"""

from __future__ import annotations

import time
import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import httpx
from conftest import Seeded
from gateway.models import (
    Group,
    IdentityEvent,
    IdentityEventAction,
    LimitMetric,
    LimitRule,
    LimitScope,
    UsageRecord,
    UsageStatus,
)
from gateway.periods import PeriodKind
from gateway.quota import (
    CounterDelta,
    DatabaseCounterStore,
    InMemoryCounterStore,
    QuotaAmounts,
    QuotaSubject,
    ScopeRef,
    WindowQuery,
    WindowSpec,
    from_units,
    to_units,
)
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from test_admin import as_user, make_admin
from test_quota_calendar import add_calendar_rule, make_group, spend
from test_quota_engine import add_rule, engine_for


def cost_query(group: Group, *, period: bool) -> WindowQuery:
    from gateway.periods import period_containing

    if period:
        return WindowQuery(
            scope=ScopeRef(LimitScope.GROUP, group.id),
            metric=LimitMetric.COST,
            period=period_containing(PeriodKind.MONTH, datetime.now(UTC), timezone="Europe/Rome"),
        )
    return WindowQuery(
        scope=ScopeRef(LimitScope.GROUP, group.id),
        metric=LimitMetric.COST,
        spec=WindowSpec.for_window(3600),
    )


def delta(query: WindowQuery, units: int) -> CounterDelta:
    return CounterDelta(
        scope=query.scope,
        metric=query.metric,
        units=units,
        spec=query.spec,
        period=query.period,
        reset_epoch=query.reset_epoch,
    )


async def counter(store: InMemoryCounterStore, query: WindowQuery, now: float) -> Decimal:
    return from_units(query.metric, (await store.totals([query], now=now))[0].units)


class TestReplace:
    async def test_calendar_replace_sets_rather_than_adds(self) -> None:
        store = InMemoryCounterStore()
        query = cost_query(Group(id=uuid.uuid4(), name="g"), period=True)
        now = time.time()
        await store.apply([delta(query, to_units(LimitMetric.COST, Decimal("7")))], now=now)

        await store.replace([delta(query, to_units(LimitMetric.COST, Decimal("3")))], now=now)
        assert await counter(store, query, now) == Decimal(3)

        await store.replace([delta(query, 0)], now=now)
        assert await counter(store, query, now) == Decimal(0)

    async def test_rolling_replace_clears_every_bucket_then_fills_the_newest(self) -> None:
        store = InMemoryCounterStore()
        query = cost_query(Group(id=uuid.uuid4(), name="g"), period=False)
        assert query.spec is not None
        now = 1_000_000.0
        # Traffic spread over three different buckets of the window.
        for age in (0, 1200, 2400):
            await store.apply([delta(query, 100)], now=now - age)
        assert (await store.totals([query], now=now))[0].units == 300

        await store.replace([delta(query, 120)], now=now)
        total = (await store.totals([query], now=now))[0]
        assert total.units == 120
        assert total.buckets == {query.spec.bucket_index(now): 120}

    async def test_replace_does_not_touch_another_scope_or_epoch(self) -> None:
        store = InMemoryCounterStore()
        mine = cost_query(Group(id=uuid.uuid4(), name="a"), period=True)
        other = cost_query(Group(id=uuid.uuid4(), name="b"), period=True)
        now = time.time()
        await store.apply([delta(other, 5)], now=now)
        await store.replace([delta(mine, 9)], now=now)
        assert (await store.totals([other], now=now))[0].units == 5


class TestRebuildNeverDoubles:
    async def test_regression_counters_without_a_marker_are_not_doubled(
        self, session: AsyncSession, session_factory: async_sessionmaker[AsyncSession]
    ) -> None:
        """Create a rule, serve traffic, restart with the marker absent: counter == ledger."""
        group = await make_group(session)
        await add_calendar_rule(
            session, scope=LimitScope.GROUP, scope_id=group.id, period=PeriodKind.MONTH, limit="5"
        )
        cache = InMemoryCounterStore()
        db = DatabaseCounterStore(session_factory)
        quota = engine_for(cache, fallback=db)
        subject = QuotaSubject(None, group.id)

        # Live traffic after the rule exists. Each request is in the ledger too.
        for _ in range(3):
            reservation = await quota.check_and_reserve(session, subject, QuotaAmounts())
            await quota.settle(reservation, QuotaAmounts(cost=Decimal("0.1")))
            await spend(session, group_id=group.id, cost="0.1")

        query = cost_query(group, period=True)
        now = time.time()
        assert await counter(cache, query, now) == Decimal("0.3")

        # The restart: a new engine over the same cache. No marker was ever
        # claimed (that was the bug), only counters exist.
        restarted = engine_for(cache, fallback=db)
        assert await restarted.rebuild_if_cache_is_cold(session) is False
        assert await counter(cache, query, now) == Decimal("0.3")

        # And even a rebuild forced onto the warm counter lands on the ledger's
        # figure, not on twice it: this is what "replace" buys.
        await db.rebuild_into(cache, [query], now=now)
        assert await counter(cache, query, now) == Decimal("0.3")

    async def test_seeding_a_rule_whose_counter_already_has_traffic_does_not_double(
        self, session: AsyncSession, session_factory: async_sessionmaker[AsyncSession]
    ) -> None:
        group = await make_group(session)
        rule = await add_rule(
            session,
            scope=LimitScope.GROUP,
            scope_id=group.id,
            metric=LimitMetric.COST,
            limit="9",
            window_seconds=3600,
        )
        await session.refresh(rule, attribute_names=["resets"])
        cache = InMemoryCounterStore()
        quota = engine_for(cache, fallback=DatabaseCounterStore(session_factory))
        await spend(session, group_id=group.id, cost="2")

        await quota.seed_rule(rule)
        await quota.seed_rule(rule)  # an edit, a re-activation, a second worker

        assert await counter(cache, cost_query(group, period=False), time.time()) == Decimal(2)

    async def test_only_cold_windows_are_rebuilt(
        self, session: AsyncSession, session_factory: async_sessionmaker[AsyncSession]
    ) -> None:
        """A partially lost cache: the warm window keeps its counter, the cold one is refilled."""
        warm, cold = await make_group(session), await make_group(session)
        for group in (warm, cold):
            await add_rule(
                session,
                scope=LimitScope.GROUP,
                scope_id=group.id,
                metric=LimitMetric.COST,
                limit="9",
                window_seconds=3600,
            )
            await spend(session, group_id=group.id, cost="2")
        cache = InMemoryCounterStore()
        now = time.time()
        # The warm group's counter holds an in-flight reservation the ledger lacks.
        await cache.apply([delta(cost_query(warm, period=False), 3 * 10**9)], now=now)

        quota = engine_for(cache, fallback=DatabaseCounterStore(session_factory))
        assert await quota.rebuild_if_cache_is_cold(session, now=now) is True

        assert await counter(cache, cost_query(warm, period=False), now) == Decimal(3)
        assert await counter(cache, cost_query(cold, period=False), now) == Decimal(2)

    async def test_an_idle_deployment_writes_nothing(
        self, session: AsyncSession, session_factory: async_sessionmaker[AsyncSession]
    ) -> None:
        group = await make_group(session)
        await add_rule(
            session,
            scope=LimitScope.GROUP,
            scope_id=group.id,
            metric=LimitMetric.COST,
            limit="9",
            window_seconds=3600,
        )
        cache = InMemoryCounterStore()
        quota = engine_for(cache, fallback=DatabaseCounterStore(session_factory))
        await quota.rebuild_if_cache_is_cold(session)
        assert not cache._buckets or all(not slot for slot in cache._buckets.values())


class TestReconcile:
    async def test_a_drifted_counter_is_pulled_back_to_the_ledger(
        self, session: AsyncSession, session_factory: async_sessionmaker[AsyncSession]
    ) -> None:
        group = await make_group(session)
        await add_calendar_rule(
            session, scope=LimitScope.GROUP, scope_id=group.id, period=PeriodKind.MONTH, limit="5"
        )
        await spend(session, group_id=group.id, cost="0.2832")
        cache = InMemoryCounterStore()
        query = cost_query(group, period=True)
        now = time.time()
        await cache.apply([delta(query, to_units(LimitMetric.COST, Decimal("0.3798")))], now=now)
        quota = engine_for(cache, fallback=DatabaseCounterStore(session_factory))

        done = await quota.reconcile(session, now=now)

        assert [(d.before, d.after) for d in done] == [(Decimal("0.3798"), Decimal("0.2832"))]
        assert await counter(cache, query, now) == Decimal("0.2832")
        # Idempotent: a second run changes nothing.
        again = await quota.reconcile(session, now=now)
        assert [(d.before, d.after) for d in again] == [(Decimal("0.2832"), Decimal("0.2832"))]

    async def test_reconcile_skips_inactive_rules(
        self, session: AsyncSession, session_factory: async_sessionmaker[AsyncSession]
    ) -> None:
        group = await make_group(session)
        rule = await add_rule(
            session,
            scope=LimitScope.GROUP,
            scope_id=group.id,
            metric=LimitMetric.COST,
            limit="9",
            window_seconds=3600,
        )
        rule.is_active = False
        await session.commit()
        quota = engine_for(InMemoryCounterStore(), fallback=DatabaseCounterStore(session_factory))
        assert await quota.reconcile(session) == []


class TestHealth:
    async def test_the_difference_and_the_stale_rows_are_reported(
        self, session: AsyncSession, session_factory: async_sessionmaker[AsyncSession]
    ) -> None:
        group = await make_group(session)
        await add_calendar_rule(
            session, scope=LimitScope.GROUP, scope_id=group.id, period=PeriodKind.MONTH, limit="5"
        )
        await spend(session, group_id=group.id, cost="1")
        session.add(
            UsageRecord(
                request_id=f"r-{uuid.uuid4().hex[:8]}",
                status=UsageStatus.IN_PROGRESS,
                group_id=group.id,
                model_name="m",
                currency="EUR",
                created_at=datetime.now(UTC) - timedelta(hours=1),
            )
        )
        await session.commit()
        cache = InMemoryCounterStore()
        now = time.time()
        await cache.apply(
            [delta(cost_query(group, period=True), to_units(LimitMetric.COST, Decimal("1.5")))],
            now=now,
        )
        quota = engine_for(cache, fallback=DatabaseCounterStore(session_factory))

        health = await quota.health(session, now=now)

        (window,) = health.windows
        assert (window.counter, window.ledger, window.difference) == (
            Decimal("1.5"),
            Decimal(1),
            Decimal("0.5"),
        )
        assert window.stale_in_progress == 1


class TestAdminRoutes:
    async def _rule_and_traffic(
        self,
        app: object,
        client: httpx.AsyncClient,
        seeded: Seeded,
        fake_upstream: object,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        from helpers import completion_body

        as_user(app, await make_admin(session_factory, seeded))
        created = await client.post(
            "/api/admin/limits",
            json={
                "name": "r",
                "scope": "group",
                "scope_id": str(seeded.group.id),
                "metric": "requests",
                "window_seconds": 3600,
                "limit_value": "100",
            },
        )
        assert created.status_code == 201, created.text
        fake_upstream.set_json(completion_body())  # type: ignore[attr-defined]
        for _ in range(2):
            response = await client.post(
                "/v1/chat/completions",
                json={"model": "test-model", "messages": [{"role": "user", "content": "hi"}]},
                headers=seeded.auth,
            )
            assert response.status_code == 200

    async def test_health_then_reconcile_then_audit(
        self,
        app: object,
        client: httpx.AsyncClient,
        seeded: Seeded,
        fake_upstream: object,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        await self._rule_and_traffic(app, client, seeded, fake_upstream, session_factory)

        healthy = (await client.get("/api/admin/quota/health")).json()
        (window,) = healthy["windows"]
        assert window["counter_value"] == window["ledger_total"] == "2"
        assert window["difference"] == "0"
        assert window["window_id"] == "g60"
        assert window["stale_in_progress"] == 0

        # Drift the counter, as a double count would have.
        engine = app.state.quota_engine  # type: ignore[attr-defined]
        spec = WindowSpec.for_window(3600)
        await engine._store.apply(
            [
                CounterDelta(
                    scope=ScopeRef(LimitScope.GROUP, seeded.group.id),
                    metric=LimitMetric.REQUESTS,
                    units=5,
                    spec=spec,
                )
            ],
            now=time.time(),
        )
        drifted = (await client.get("/api/admin/quota/health")).json()["windows"][0]
        assert drifted["difference"] == "5"

        fixed = await client.post("/api/admin/quota/reconcile")
        assert fixed.status_code == 200, fixed.text
        body = fixed.json()
        assert body["corrected"] == 1
        assert (body["reconciled"][0]["before"], body["reconciled"][0]["after"]) == ("7", "2")
        assert (await client.get("/api/admin/quota/health")).json()["windows"][0][
            "difference"
        ] == "0"

        async with session_factory() as session:
            events = (
                (
                    await session.execute(
                        select(IdentityEvent).where(
                            IdentityEvent.action == IdentityEventAction.QUOTA_RECONCILE
                        )
                    )
                )
                .scalars()
                .all()
            )
        assert [e.detail for e in events] == [{"windows": 1, "corrected": 1}]

    async def test_both_routes_are_admin_only(
        self,
        app: object,
        client: httpx.AsyncClient,
        seeded: Seeded,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        as_user(app, await make_admin(session_factory, seeded, admin=False))
        assert (await client.get("/api/admin/quota/health")).status_code == 403
        assert (await client.post("/api/admin/quota/reconcile")).status_code == 403

    async def test_reactivating_a_rule_reseeds_it_rather_than_adding(
        self,
        app: object,
        client: httpx.AsyncClient,
        seeded: Seeded,
        fake_upstream: object,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        await self._rule_and_traffic(app, client, seeded, fake_upstream, session_factory)
        async with session_factory() as session:
            rule = (await session.execute(select(LimitRule))).scalar_one()
        await client.patch(f"/api/admin/limits/{rule.id}", json={"is_active": False})
        await client.patch(f"/api/admin/limits/{rule.id}", json={"is_active": True})

        (window,) = (await client.get("/api/admin/quota/health")).json()["windows"]
        assert window["difference"] == "0"
