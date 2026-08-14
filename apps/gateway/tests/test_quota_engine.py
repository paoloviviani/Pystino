"""Quota rule evaluation, reserve-then-settle, and the overrun policy.

The behaviour that most needs pinning down is concurrency. Without reservations,
N requests arriving together each read the same under-limit total and each decide
they are allowed, so a group with room for one request runs N. That is tested
explicitly below.
"""

from __future__ import annotations

import asyncio
import logging
import time
import uuid
from decimal import Decimal

import pytest
from gateway.config import QuotaSettings
from gateway.models import (
    Group,
    LimitMetric,
    LimitRule,
    LimitScope,
    Membership,
    UsageRecord,
    UsageStatus,
    User,
)
from gateway.quota import (
    DatabaseCounterStore,
    InMemoryCounterStore,
    QuotaAmounts,
    QuotaEngine,
    QuotaExceeded,
    QuotaSubject,
    QuotaUnavailable,
    ScopeRef,
    WindowQuery,
    WindowSpec,
    from_units,
    to_units,
)
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker


def engine_for(
    store: object = None, *, fallback: object = None, **overrides: object
) -> QuotaEngine:
    settings = QuotaSettings(enabled=True, **overrides)  # type: ignore[arg-type]
    return QuotaEngine(
        store or InMemoryCounterStore(),  # type: ignore[arg-type]
        settings=settings,
        fallback=fallback,  # type: ignore[arg-type]
    )


async def add_rule(
    session: AsyncSession,
    *,
    scope: LimitScope,
    metric: LimitMetric,
    limit: str,
    window_seconds: int = 60,
    scope_id: uuid.UUID | None = None,
) -> LimitRule:
    rule = LimitRule(
        name=f"{scope}-{metric}",
        scope=scope,
        scope_id=scope_id,
        metric=metric,
        window_seconds=window_seconds,
        limit_value=Decimal(limit),
    )
    session.add(rule)
    await session.commit()
    return rule


# --------------------------------------------------------------------------
# unit scaling
# --------------------------------------------------------------------------


class TestUnitScaling:
    def test_requests_and_tokens_are_stored_as_integers(self) -> None:
        assert to_units(LimitMetric.REQUESTS, Decimal(3)) == 3
        assert to_units(LimitMetric.TOKENS, Decimal(1500)) == 1500

    def test_cost_is_scaled_to_integer_nano_units(self) -> None:
        """Counters must hold integers: INCRBYFLOAT is not exact."""
        assert to_units(LimitMetric.COST, Decimal("1.5")) == 1_500_000_000
        assert from_units(LimitMetric.COST, 1_500_000_000) == Decimal("1.5")

    def test_round_trip_preserves_value(self) -> None:
        original = Decimal("0.123456789")
        assert from_units(LimitMetric.COST, to_units(LimitMetric.COST, original)) == original

    def test_reservations_round_up(self) -> None:
        """An estimate must never be accidentally generous."""
        assert to_units(LimitMetric.COST, Decimal("0.0000000001"), round_up=True) == 1
        assert to_units(LimitMetric.COST, Decimal("0.0000000001")) == 0


class TestScopeRef:
    def test_global_scope_rejects_an_id(self) -> None:
        with pytest.raises(ValueError):
            ScopeRef(LimitScope.GLOBAL, uuid.uuid4())

    def test_scoped_rules_require_an_id(self) -> None:
        with pytest.raises(ValueError):
            ScopeRef(LimitScope.GROUP, None)


# --------------------------------------------------------------------------
# counter store semantics
# --------------------------------------------------------------------------


class TestInMemoryStore:
    async def test_totals_sum_the_window(self) -> None:
        store = InMemoryCounterStore()
        scope = ScopeRef(LimitScope.GROUP, uuid.uuid4())
        spec = WindowSpec(window_seconds=30, granularity_seconds=10)
        query = WindowQuery(scope=scope, metric=LimitMetric.TOKENS, spec=spec)

        from gateway.quota import CounterDelta

        await store.apply(
            [CounterDelta(scope=scope, metric=LimitMetric.TOKENS, spec=spec, units=100)],
            now=100,
        )
        await store.apply(
            [CounterDelta(scope=scope, metric=LimitMetric.TOKENS, spec=spec, units=50)],
            now=115,
        )

        assert (await store.totals([query], now=115))[0].units == 150

    async def test_traffic_ages_out_of_the_window(self) -> None:
        store = InMemoryCounterStore()
        scope = ScopeRef(LimitScope.USER, uuid.uuid4())
        spec = WindowSpec(window_seconds=30, granularity_seconds=10)
        query = WindowQuery(scope=scope, metric=LimitMetric.REQUESTS, spec=spec)

        from gateway.quota import CounterDelta

        await store.apply(
            [CounterDelta(scope=scope, metric=LimitMetric.REQUESTS, spec=spec, units=7)],
            now=100,
        )
        assert (await store.totals([query], now=100))[0].units == 7
        # Far enough in the future that bucket 10 is no longer in the window.
        assert (await store.totals([query], now=200))[0].units == 0

    async def test_negative_totals_are_clamped(self) -> None:
        store = InMemoryCounterStore()
        scope = ScopeRef(LimitScope.GLOBAL)
        spec = WindowSpec(window_seconds=30, granularity_seconds=10)
        from gateway.quota import CounterDelta

        await store.apply(
            [CounterDelta(scope=scope, metric=LimitMetric.TOKENS, spec=spec, units=-500)],
            now=100,
        )
        query = WindowQuery(scope=scope, metric=LimitMetric.TOKENS, spec=spec)
        assert (await store.totals([query], now=100))[0].units == 0


# --------------------------------------------------------------------------
# rule evaluation
# --------------------------------------------------------------------------


class TestRuleEvaluation:
    async def test_no_rules_means_no_limit(self, session: AsyncSession) -> None:
        quota = engine_for()
        reservation = await quota.check_and_reserve(
            session, QuotaSubject(uuid.uuid4(), uuid.uuid4()), QuotaAmounts(tokens=Decimal(10**9))
        )
        assert reservation.is_empty

    async def test_disabled_engine_skips_everything(self, session: AsyncSession) -> None:
        quota = QuotaEngine(InMemoryCounterStore(), settings=QuotaSettings(enabled=False))
        await add_rule(session, scope=LimitScope.GLOBAL, metric=LimitMetric.REQUESTS, limit="0")
        reservation = await quota.check_and_reserve(
            session, QuotaSubject(None, None), QuotaAmounts()
        )
        assert reservation.is_empty

    async def test_zero_limit_refuses_immediately(self, session: AsyncSession) -> None:
        await add_rule(session, scope=LimitScope.GLOBAL, metric=LimitMetric.REQUESTS, limit="0")
        with pytest.raises(QuotaExceeded):
            await engine_for().check_and_reserve(session, QuotaSubject(None, None), QuotaAmounts())

    async def test_requests_metric_refuses_after_the_limit_is_reached(
        self, session: AsyncSession
    ) -> None:
        await add_rule(session, scope=LimitScope.GLOBAL, metric=LimitMetric.REQUESTS, limit="2")
        quota = engine_for()
        subject = QuotaSubject(None, None)

        for _ in range(2):
            reservation = await quota.check_and_reserve(session, subject, QuotaAmounts())
            await quota.settle(reservation, QuotaAmounts(requests=Decimal(1)))

        with pytest.raises(QuotaExceeded) as caught:
            await quota.check_and_reserve(session, subject, QuotaAmounts())
        assert caught.value.retry_after_seconds >= 1

    async def test_only_rules_for_the_subject_apply(self, session: AsyncSession) -> None:
        """A rule on someone else's group must not affect this caller."""
        other_group = uuid.uuid4()
        await add_rule(
            session,
            scope=LimitScope.GROUP,
            scope_id=other_group,
            metric=LimitMetric.REQUESTS,
            limit="0",
        )
        reservation = await engine_for().check_and_reserve(
            session, QuotaSubject(uuid.uuid4(), uuid.uuid4()), QuotaAmounts()
        )
        assert reservation.is_empty

    async def test_all_matching_rules_must_pass(self, session: AsyncSession) -> None:
        """Additive, not most-specific-wins: the tightest rule governs."""
        group_id = uuid.uuid4()
        user_id = uuid.uuid4()
        await add_rule(session, scope=LimitScope.GLOBAL, metric=LimitMetric.TOKENS, limit="1000000")
        await add_rule(
            session,
            scope=LimitScope.USER,
            scope_id=user_id,
            metric=LimitMetric.TOKENS,
            limit="100",
        )
        quota = engine_for()
        subject = QuotaSubject(user_id, group_id)

        reservation = await quota.check_and_reserve(session, subject, QuotaAmounts())
        await quota.settle(reservation, QuotaAmounts(tokens=Decimal(150)))

        # The generous global rule does not rescue the tight user rule.
        with pytest.raises(QuotaExceeded) as caught:
            await quota.check_and_reserve(session, subject, QuotaAmounts())
        assert caught.value.violations[0].rule.scope is LimitScope.USER

    async def test_cost_metric_uses_exact_decimals(self, session: AsyncSession) -> None:
        group_id = uuid.uuid4()
        await add_rule(
            session,
            scope=LimitScope.GROUP,
            scope_id=group_id,
            metric=LimitMetric.COST,
            limit="0.10",
        )
        quota = engine_for()
        subject = QuotaSubject(None, group_id)

        reservation = await quota.check_and_reserve(session, subject, QuotaAmounts())
        await quota.settle(reservation, QuotaAmounts(cost=Decimal("0.09")))
        # Still under: 0.09 < 0.10
        second = await quota.check_and_reserve(session, subject, QuotaAmounts())
        await quota.settle(second, QuotaAmounts(cost=Decimal("0.01")))
        # Now exactly at the limit, so the next request is refused.
        with pytest.raises(QuotaExceeded):
            await quota.check_and_reserve(session, subject, QuotaAmounts())

    async def test_inactive_rules_are_ignored(self, session: AsyncSession) -> None:
        rule = await add_rule(
            session, scope=LimitScope.GLOBAL, metric=LimitMetric.REQUESTS, limit="0"
        )
        rule.is_active = False
        await session.commit()
        reservation = await engine_for().check_and_reserve(
            session, QuotaSubject(None, None), QuotaAmounts()
        )
        assert reservation.is_empty


# --------------------------------------------------------------------------
# reserve / settle
# --------------------------------------------------------------------------


# A fixed instant, so a reservation and its settlement always share a bucket.
# Without pinning the clock these assertions depend on whether the test happened
# to straddle a one-second bucket boundary.
FIXED_NOW = 1_700_000_000.0


class TestReserveAndSettle:
    async def test_settling_replaces_the_estimate_exactly(self, session: AsyncSession) -> None:
        group_id = uuid.uuid4()
        await add_rule(
            session,
            scope=LimitScope.GROUP,
            scope_id=group_id,
            metric=LimitMetric.TOKENS,
            limit="10000",
        )
        store = InMemoryCounterStore()
        quota = engine_for(store)
        subject = QuotaSubject(None, group_id)

        reservation = await quota.check_and_reserve(
            session, subject, QuotaAmounts(tokens=Decimal(5000)), now=FIXED_NOW
        )
        spec = WindowSpec.for_window(60)
        query = WindowQuery(
            scope=ScopeRef(LimitScope.GROUP, group_id), metric=LimitMetric.TOKENS, spec=spec
        )
        # While in flight, the worst case is visible to everyone else.
        assert (await store.totals([query], now=FIXED_NOW))[0].units == 5000

        await quota.settle(reservation, QuotaAmounts(tokens=Decimal(120)), now=FIXED_NOW)
        # Afterwards, only the truth remains.
        assert (await store.totals([query], now=FIXED_NOW))[0].units == 120

    async def test_releasing_removes_the_reservation_entirely(self, session: AsyncSession) -> None:
        group_id = uuid.uuid4()
        await add_rule(
            session,
            scope=LimitScope.GROUP,
            scope_id=group_id,
            metric=LimitMetric.TOKENS,
            limit="10000",
        )
        store = InMemoryCounterStore()
        quota = engine_for(store)
        reservation = await quota.check_and_reserve(
            session, QuotaSubject(None, group_id), QuotaAmounts(tokens=Decimal(5000)), now=FIXED_NOW
        )
        await quota.release(reservation, now=FIXED_NOW)

        spec = WindowSpec.for_window(60)
        query = WindowQuery(
            scope=ScopeRef(LimitScope.GROUP, group_id), metric=LimitMetric.TOKENS, spec=spec
        )
        assert (await store.totals([query], now=FIXED_NOW))[0].units == 0

    async def test_settle_is_idempotent(self, session: AsyncSession) -> None:
        """finalise() runs on several exit paths that can overlap."""
        group_id = uuid.uuid4()
        await add_rule(
            session,
            scope=LimitScope.GROUP,
            scope_id=group_id,
            metric=LimitMetric.TOKENS,
            limit="10000",
        )
        store = InMemoryCounterStore()
        quota = engine_for(store)
        reservation = await quota.check_and_reserve(
            session, QuotaSubject(None, group_id), QuotaAmounts(tokens=Decimal(1000)), now=FIXED_NOW
        )
        await quota.settle(reservation, QuotaAmounts(tokens=Decimal(10)), now=FIXED_NOW)
        await quota.settle(reservation, QuotaAmounts(tokens=Decimal(10)), now=FIXED_NOW)

        spec = WindowSpec.for_window(60)
        query = WindowQuery(
            scope=ScopeRef(LimitScope.GROUP, group_id), metric=LimitMetric.TOKENS, spec=spec
        )
        assert (await store.totals([query], now=FIXED_NOW))[0].units == 10

    async def test_concurrent_requests_cannot_all_pass_the_same_check(
        self, session: AsyncSession, session_factory: async_sessionmaker[AsyncSession]
    ) -> None:
        """The reason reservations exist at all.

        Ten requests start at once against a 1000-token ceiling, each of which
        might use 400 tokens. Without reservations all ten would read a total of
        zero and all ten would proceed, spending up to 4000.
        """
        group_id = uuid.uuid4()
        await add_rule(
            session,
            scope=LimitScope.GROUP,
            scope_id=group_id,
            metric=LimitMetric.TOKENS,
            limit="1000",
        )
        quota = engine_for()
        subject = QuotaSubject(None, group_id)

        async def attempt() -> bool:
            # Each concurrent caller needs its own session: an AsyncSession is
            # not safe to share across tasks, exactly as in a real request.
            async with session_factory() as own_session:
                try:
                    await quota.check_and_reserve(
                        own_session, subject, QuotaAmounts(tokens=Decimal(400))
                    )
                    return True
                except QuotaExceeded:
                    return False

        results = await asyncio.gather(*(attempt() for _ in range(10)))
        admitted = sum(results)

        # At most ceil(1000/400) = 3 can be admitted before the reserved total
        # reaches the ceiling, and at least one must get through.
        assert 1 <= admitted <= 3, f"admitted {admitted} of 10"


# --------------------------------------------------------------------------
# fallback behaviour
# --------------------------------------------------------------------------


class BrokenStore:
    """A counter store that is always down."""

    async def totals(self, queries: object, *, now: float) -> list[object]:
        raise ConnectionError("counter store is down")

    async def apply(self, deltas: object, *, now: float) -> None:
        raise ConnectionError("counter store is down")

    async def claim_once(self, key: str, *, ttl_seconds: int) -> bool:
        raise ConnectionError("counter store is down")


class TestFallback:
    async def test_falls_back_to_the_database(
        self, session: AsyncSession, session_factory: async_sessionmaker[AsyncSession]
    ) -> None:
        """Losing Valkey must not lose quota enforcement."""
        group = Group(name="fallback-group")
        session.add(group)
        await session.commit()

        await add_rule(
            session,
            scope=LimitScope.GROUP,
            scope_id=group.id,
            metric=LimitMetric.REQUESTS,
            limit="1",
        )
        # One settled request already on the ledger.
        session.add(
            UsageRecord(
                request_id="r1",
                status=UsageStatus.COMPLETED,
                group_id=group.id,
                model_name="m",
                currency="EUR",
                total_tokens=10,
                cost=Decimal("0.5"),
            )
        )
        await session.commit()

        quota = engine_for(BrokenStore(), fallback=DatabaseCounterStore(session_factory))
        with pytest.raises(QuotaExceeded):
            await quota.check_and_reserve(session, QuotaSubject(None, group.id), QuotaAmounts())

    async def test_refuses_when_no_store_can_answer(self, session: AsyncSession) -> None:
        """Fail closed by default: protect the budget, not the availability."""
        await add_rule(session, scope=LimitScope.GLOBAL, metric=LimitMetric.REQUESTS, limit="100")
        quota = engine_for(BrokenStore(), fallback=None)
        with pytest.raises(QuotaUnavailable):
            await quota.check_and_reserve(session, QuotaSubject(None, None), QuotaAmounts())

    async def test_fail_open_allows_when_configured(self, session: AsyncSession) -> None:
        await add_rule(session, scope=LimitScope.GLOBAL, metric=LimitMetric.REQUESTS, limit="100")
        quota = engine_for(BrokenStore(), fallback=None, fail_open=True)
        reservation = await quota.check_and_reserve(
            session, QuotaSubject(None, None), QuotaAmounts()
        )
        # Counter writes also fail, but that must not raise: the ledger is a
        # separate, transactional path.
        assert reservation is not None

    async def test_recurring_outage_logs_one_traceback_not_one_per_request(
        self, session: AsyncSession
    ) -> None:
        """A counter-store outage must not become a second incident.

        The store is touched three times per request and the outage recurs on
        every request, so logging a full stack each time floods the log with
        identical traces and buries the single line an operator needs. Found by
        stopping Valkey under a real running stack.
        """
        await add_rule(session, scope=LimitScope.GLOBAL, metric=LimitMetric.REQUESTS, limit="100")
        quota = engine_for(BrokenStore(), fallback=None, fail_open=True)

        captured: list[logging.LogRecord] = []

        class Capture(logging.Handler):
            def emit(self, record: logging.LogRecord) -> None:
                captured.append(record)

        handler = Capture()
        engine_logger = logging.getLogger("gateway.quota.engine")
        engine_logger.addHandler(handler)
        try:
            for _ in range(5):
                reservation = await quota.check_and_reserve(
                    session, QuotaSubject(None, None), QuotaAmounts(), now=FIXED_NOW
                )
                await quota.settle(reservation, QuotaAmounts(), now=FIXED_NOW)
        finally:
            engine_logger.removeHandler(handler)

        read_failures = [r for r in captured if "unavailable for reads" in r.getMessage()]
        # Every failure is still reported — silence would be worse.
        assert len(read_failures) == 5
        # But only the first carries a traceback.
        assert sum(1 for r in read_failures if r.exc_info) == 1
        assert "traceback suppressed" in read_failures[-1].getMessage()

    async def test_write_failures_do_not_break_the_request(self, session: AsyncSession) -> None:
        await add_rule(session, scope=LimitScope.GLOBAL, metric=LimitMetric.REQUESTS, limit="100")

        class ReadOkWriteBroken(InMemoryCounterStore):
            async def apply(self, deltas: object, *, now: float) -> None:
                raise ConnectionError("write failed")

        quota = engine_for(ReadOkWriteBroken())
        reservation = await quota.check_and_reserve(
            session, QuotaSubject(None, None), QuotaAmounts()
        )
        await quota.settle(reservation, QuotaAmounts())


# --------------------------------------------------------------------------
# database counter store
# --------------------------------------------------------------------------


class TestColdCacheRebuild:
    """An empty counter cache is not a failed read — it answers zero, confidently.

    Found by restarting Valkey under a running stack: the ledger said 11.70 spent
    against a 10 ceiling while the cache said 0.90, so the group was served.
    """

    async def _group_with_spend(self, session: AsyncSession, *, spend: str, limit: str) -> Group:
        group = Group(name=f"cold-{uuid.uuid4().hex[:8]}")
        session.add(group)
        await session.flush()
        await add_rule(
            session,
            scope=LimitScope.GROUP,
            scope_id=group.id,
            metric=LimitMetric.COST,
            limit=limit,
            window_seconds=3600,
        )
        session.add(
            UsageRecord(
                request_id=f"r-{uuid.uuid4().hex[:8]}",
                status=UsageStatus.COMPLETED,
                group_id=group.id,
                model_name="m",
                currency="EUR",
                cost=Decimal(spend),
            )
        )
        await session.commit()
        return group

    async def test_cold_cache_is_refilled_from_the_ledger(
        self, session: AsyncSession, session_factory: async_sessionmaker[AsyncSession]
    ) -> None:
        group = await self._group_with_spend(session, spend="11.5", limit="10")
        cache = InMemoryCounterStore()
        quota = engine_for(cache, fallback=DatabaseCounterStore(session_factory))

        query = WindowQuery(
            scope=ScopeRef(LimitScope.GROUP, group.id),
            metric=LimitMetric.COST,
            spec=WindowSpec.for_window(3600),
        )
        # Cold: the cache knows nothing, so an over-budget group looks fine.
        assert (await cache.totals([query], now=time.time()))[0].units == 0

        assert await quota.rebuild_if_cache_is_cold(session) is True
        restored = from_units(
            LimitMetric.COST, (await cache.totals([query], now=time.time()))[0].units
        )
        assert restored == Decimal("11.5")

    async def test_an_over_budget_group_is_refused_after_a_rebuild(
        self, session: AsyncSession, session_factory: async_sessionmaker[AsyncSession]
    ) -> None:
        """The behaviour that actually matters: the money leak is closed."""
        group = await self._group_with_spend(session, spend="11.5", limit="10")
        cache = InMemoryCounterStore()
        quota = engine_for(cache, fallback=DatabaseCounterStore(session_factory))
        subject = QuotaSubject(None, group.id)

        # Before the rebuild the cold cache lets it straight through.
        await quota.check_and_reserve(session, subject, QuotaAmounts())

        cache2 = InMemoryCounterStore()
        quota2 = engine_for(cache2, fallback=DatabaseCounterStore(session_factory))
        await quota2.rebuild_if_cache_is_cold(session)
        with pytest.raises(QuotaExceeded):
            await quota2.check_and_reserve(session, subject, QuotaAmounts())

    async def test_a_warm_cache_is_not_rebuilt(
        self, session: AsyncSession, session_factory: async_sessionmaker[AsyncSession]
    ) -> None:
        """Rebuilding a warm cache would double every total."""
        group = await self._group_with_spend(session, spend="3", limit="10")
        cache = InMemoryCounterStore()
        quota = engine_for(cache, fallback=DatabaseCounterStore(session_factory))

        assert await quota.rebuild_if_cache_is_cold(session) is True
        assert await quota.rebuild_if_cache_is_cold(session) is False

        query = WindowQuery(
            scope=ScopeRef(LimitScope.GROUP, group.id),
            metric=LimitMetric.COST,
            spec=WindowSpec.for_window(3600),
        )
        total = from_units(
            LimitMetric.COST, (await cache.totals([query], now=time.time()))[0].units
        )
        assert total == Decimal(3)

    async def test_concurrent_workers_produce_one_rebuild_not_n(
        self, session: AsyncSession, session_factory: async_sessionmaker[AsyncSession]
    ) -> None:
        """Four workers sharing a cache must not sum to four times the truth."""
        group = await self._group_with_spend(session, spend="2", limit="10")
        cache = InMemoryCounterStore()
        engines = [
            engine_for(cache, fallback=DatabaseCounterStore(session_factory)) for _ in range(4)
        ]

        results = []
        for quota in engines:
            async with session_factory() as own:
                results.append(await quota.rebuild_if_cache_is_cold(own))
        assert sum(results) == 1, "more than one worker rebuilt"

        query = WindowQuery(
            scope=ScopeRef(LimitScope.GROUP, group.id),
            metric=LimitMetric.COST,
            spec=WindowSpec.for_window(3600),
        )
        total = from_units(
            LimitMetric.COST, (await cache.totals([query], now=time.time()))[0].units
        )
        assert total == Decimal(2)

    async def test_no_rules_means_nothing_to_rebuild(self, session: AsyncSession) -> None:
        quota = engine_for(InMemoryCounterStore(), fallback=DatabaseCounterStore)  # type: ignore[arg-type]
        assert await quota.rebuild_if_cache_is_cold(session) is False

    async def test_no_fallback_means_no_rebuild(self, session: AsyncSession) -> None:
        await add_rule(session, scope=LimitScope.GLOBAL, metric=LimitMetric.COST, limit="1")
        quota = engine_for(InMemoryCounterStore(), fallback=None)
        assert await quota.rebuild_if_cache_is_cold(session) is False

    async def test_a_broken_cache_does_not_break_startup(
        self, session: AsyncSession, session_factory: async_sessionmaker[AsyncSession]
    ) -> None:
        """A gateway that will not boot is worse than optimistic counters."""
        await add_rule(session, scope=LimitScope.GLOBAL, metric=LimitMetric.COST, limit="1")
        quota = engine_for(BrokenStore(), fallback=DatabaseCounterStore(session_factory))
        assert await quota.rebuild_if_cache_is_cold(session) is False


class TestDatabaseCounterStore:
    async def test_aggregates_by_scope_and_window(
        self, session: AsyncSession, session_factory: async_sessionmaker[AsyncSession]
    ) -> None:
        group = Group(name="agg-group")
        user = User(issuer="i", subject="s")
        session.add_all([group, user])
        await session.flush()
        session.add(Membership(user_id=user.id, group_id=group.id))

        for index in range(3):
            session.add(
                UsageRecord(
                    request_id=f"req-{index}",
                    status=UsageStatus.COMPLETED,
                    group_id=group.id,
                    user_id=user.id,
                    model_name="m",
                    currency="EUR",
                    total_tokens=100,
                    # Values chosen to survive a SQLite float round-trip.
                    cost=Decimal("0.25"),
                )
            )
        await session.commit()

        store = DatabaseCounterStore(session_factory)
        spec = WindowSpec.for_window(3600)
        scope = ScopeRef(LimitScope.GROUP, group.id)
        now = time.time()

        requests, tokens, cost = await store.totals(
            [
                WindowQuery(scope=scope, metric=LimitMetric.REQUESTS, spec=spec),
                WindowQuery(scope=scope, metric=LimitMetric.TOKENS, spec=spec),
                WindowQuery(scope=scope, metric=LimitMetric.COST, spec=spec),
            ],
            now=now,
        )
        assert requests.units == 3
        assert tokens.units == 300
        assert from_units(LimitMetric.COST, cost.units) == Decimal("0.75")

    async def test_in_progress_rows_are_excluded(
        self, session: AsyncSession, session_factory: async_sessionmaker[AsyncSession]
    ) -> None:
        """A row that has not finished yet has nothing to contribute."""
        group = Group(name="inflight-group")
        session.add(group)
        await session.flush()
        session.add(
            UsageRecord(
                request_id="inflight",
                status=UsageStatus.IN_PROGRESS,
                group_id=group.id,
                model_name="m",
                currency="EUR",
            )
        )
        await session.commit()

        store = DatabaseCounterStore(session_factory)
        total = (
            await store.totals(
                [
                    WindowQuery(
                        scope=ScopeRef(LimitScope.GROUP, group.id),
                        metric=LimitMetric.REQUESTS,
                        spec=WindowSpec.for_window(3600),
                    )
                ],
                now=time.time(),
            )
        )[0]
        assert total.units == 0

    async def test_disconnected_streams_still_count(
        self, session: AsyncSession, session_factory: async_sessionmaker[AsyncSession]
    ) -> None:
        """Tokens spent before a client vanished were still spent."""
        group = Group(name="disconnected-group")
        session.add(group)
        await session.flush()
        session.add(
            UsageRecord(
                request_id="gone",
                status=UsageStatus.CLIENT_DISCONNECTED,
                group_id=group.id,
                model_name="m",
                currency="EUR",
                total_tokens=42,
            )
        )
        await session.commit()

        store = DatabaseCounterStore(session_factory)
        total = (
            await store.totals(
                [
                    WindowQuery(
                        scope=ScopeRef(LimitScope.GROUP, group.id),
                        metric=LimitMetric.TOKENS,
                        spec=WindowSpec.for_window(3600),
                    )
                ],
                now=time.time(),
            )
        )[0]
        assert total.units == 42

    async def test_rebuild_restores_counters_from_the_ledger(
        self, session: AsyncSession, session_factory: async_sessionmaker[AsyncSession]
    ) -> None:
        """Valkey is disposable precisely because this works."""
        group = Group(name="rebuild-group")
        session.add(group)
        await session.flush()
        session.add(
            UsageRecord(
                request_id="one",
                status=UsageStatus.COMPLETED,
                group_id=group.id,
                model_name="m",
                currency="EUR",
                total_tokens=250,
            )
        )
        await session.commit()

        db_store = DatabaseCounterStore(session_factory)
        cache = InMemoryCounterStore()
        spec = WindowSpec.for_window(3600)
        query = WindowQuery(
            scope=ScopeRef(LimitScope.GROUP, group.id), metric=LimitMetric.TOKENS, spec=spec
        )
        now = time.time()

        assert (await cache.totals([query], now=now))[0].units == 0
        await db_store.rebuild_into(cache, [query], now=now)
        assert (await cache.totals([query], now=now))[0].units == 250
