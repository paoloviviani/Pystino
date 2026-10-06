"""Calendar-period quotas, api_key scope, and resettable quotas (ADR 0025).

The two assertions that matter most here are the ones a design document cannot make
on its own: that a calendar budget and a report for the same period return the same
number, and that a reset survives the counter cache being wiped and rebuilt.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from gateway.models import (
    ApiKey,
    Group,
    LimitMetric,
    LimitRule,
    LimitScope,
    QuotaReset,
    UsageRecord,
    UsageStatus,
    User,
)
from gateway.periods import PeriodKind, parse_period
from gateway.quota import (
    DatabaseCounterStore,
    InMemoryCounterStore,
    QuotaAmounts,
    QuotaExceeded,
    QuotaSubject,
    ScopeRef,
    WindowQuery,
    from_units,
)
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from test_quota_engine import engine_for


async def make_group(session: AsyncSession, name: str | None = None) -> Group:
    group = Group(name=name or f"g-{uuid.uuid4().hex[:8]}")
    session.add(group)
    await session.flush()
    return group


async def make_key(session: AsyncSession) -> ApiKey:
    handle = uuid.uuid4().hex[:12]
    user = User(
        issuer="https://idp.example.test",
        subject=f"s-{handle}",
        email=f"{handle}@example.test",
        display_name=handle,
    )
    session.add(user)
    await session.flush()
    key = ApiKey(user_id=user.id, prefix=f"p-{handle}", key_hash=handle * 4, name=handle)
    session.add(key)
    await session.flush()
    return key


async def add_calendar_rule(
    session: AsyncSession,
    *,
    scope: LimitScope,
    scope_id: uuid.UUID | None,
    period: PeriodKind,
    limit: str,
    metric: LimitMetric = LimitMetric.COST,
) -> LimitRule:
    rule = LimitRule(
        name=f"{scope}-{period}",
        scope=scope,
        scope_id=scope_id,
        metric=metric,
        period=period,
        limit_value=Decimal(limit),
    )
    session.add(rule)
    await session.commit()
    await session.refresh(rule, attribute_names=["resets"])
    return rule


async def spend(
    session: AsyncSession,
    *,
    group_id: uuid.UUID | None = None,
    user_id: uuid.UUID | None = None,
    api_key_id: uuid.UUID | None = None,
    cost: str = "1",
    at: datetime | None = None,
) -> None:
    session.add(
        UsageRecord(
            request_id=f"r-{uuid.uuid4().hex[:8]}",
            status=UsageStatus.COMPLETED,
            group_id=group_id,
            user_id=user_id,
            api_key_id=api_key_id,
            model_name="m",
            currency="EUR",
            total_tokens=10,
            cost=Decimal(cost),
            created_at=at or datetime.now(UTC),
        )
    )
    await session.commit()


class TestRuleShape:
    async def test_a_rule_is_rolling_or_calendar_never_both(self, session: AsyncSession) -> None:
        """Enforced by ck_limit_rules_one_window_kind, not just by convention."""
        from sqlalchemy.exc import IntegrityError

        session.add(
            LimitRule(
                name="both",
                scope=LimitScope.GLOBAL,
                metric=LimitMetric.COST,
                window_seconds=60,
                period=PeriodKind.MONTH,
                limit_value=Decimal(1),
            )
        )
        with pytest.raises(IntegrityError):
            await session.commit()
        await session.rollback()

    async def test_a_rule_must_have_one_of_them(self, session: AsyncSession) -> None:
        from sqlalchemy.exc import IntegrityError

        session.add(
            LimitRule(
                name="neither",
                scope=LimitScope.GLOBAL,
                metric=LimitMetric.COST,
                limit_value=Decimal(1),
            )
        )
        with pytest.raises(IntegrityError):
            await session.commit()
        await session.rollback()

    async def test_duplicate_global_rules_are_rejected(self, session: AsyncSession) -> None:
        """A plain UNIQUE could not do this: scope_id is NULL for global rules and
        SQL treats NULLs as distinct, so both rows were previously accepted."""
        from sqlalchemy.exc import IntegrityError

        for _ in range(2):
            session.add(
                LimitRule(
                    name="dup",
                    scope=LimitScope.GLOBAL,
                    metric=LimitMetric.TOKENS,
                    window_seconds=60,
                    limit_value=Decimal(1),
                )
            )
        with pytest.raises(IntegrityError):
            await session.commit()
        await session.rollback()


class TestCalendarEnforcement:
    async def test_a_monthly_budget_refuses_once_spent(
        self, session: AsyncSession, session_factory: async_sessionmaker[AsyncSession]
    ) -> None:
        group = await make_group(session)
        await add_calendar_rule(
            session,
            scope=LimitScope.GROUP,
            scope_id=group.id,
            period=PeriodKind.MONTH,
            limit="10",
        )
        quota = engine_for(fallback=DatabaseCounterStore(session_factory))
        subject = QuotaSubject(None, group.id)

        reservation = await quota.check_and_reserve(session, subject, QuotaAmounts())
        await quota.settle(reservation, QuotaAmounts(cost=Decimal(10)))

        with pytest.raises(QuotaExceeded) as caught:
            await quota.check_and_reserve(session, subject, QuotaAmounts())
        violation = caught.value.violations[0]
        assert violation.rule.period is PeriodKind.MONTH
        # The message renders the window, not "per Nones".
        assert "per month" in violation.describe()

    async def test_retry_after_is_the_exact_time_to_the_period_end(
        self, session: AsyncSession
    ) -> None:
        """A calendar period knows when it ends; a rolling window can only estimate."""
        group = await make_group(session)
        await add_calendar_rule(
            session,
            scope=LimitScope.GROUP,
            scope_id=group.id,
            period=PeriodKind.MONTH,
            limit="0",
        )
        quota = engine_for()
        with pytest.raises(QuotaExceeded) as caught:
            await quota.check_and_reserve(session, QuotaSubject(None, group.id), QuotaAmounts())

        now = datetime.now(UTC)
        month = parse_period(now.strftime("%Y-%m"))
        expected = month.seconds_remaining(now)
        assert abs(caught.value.retry_after_seconds - expected) <= 5

    async def test_rolling_and_calendar_rules_stack(
        self, session: AsyncSession, session_factory: async_sessionmaker[AsyncSession]
    ) -> None:
        """ "EUR 1 every 6 hours" and "EUR 5 per month" are two rules; either can refuse."""
        group = await make_group(session)
        session.add(
            LimitRule(
                name="hourly",
                scope=LimitScope.GROUP,
                scope_id=group.id,
                metric=LimitMetric.COST,
                window_seconds=6 * 3600,
                limit_value=Decimal(1),
            )
        )
        await session.commit()
        await add_calendar_rule(
            session,
            scope=LimitScope.GROUP,
            scope_id=group.id,
            period=PeriodKind.MONTH,
            limit="5",
        )

        quota = engine_for()
        subject = QuotaSubject(None, group.id)
        reservation = await quota.check_and_reserve(session, subject, QuotaAmounts())
        await quota.settle(reservation, QuotaAmounts(cost=Decimal("1.5")))

        # Under the monthly cap, over the 6-hour one: the tighter rule refuses.
        with pytest.raises(QuotaExceeded) as caught:
            await quota.check_and_reserve(session, subject, QuotaAmounts())
        assert caught.value.violations[0].rule.window_seconds == 6 * 3600

    async def test_the_two_kinds_use_separate_counters(self, session: AsyncSession) -> None:
        """Sharing a key would double-count one metric against both rules."""
        group = await make_group(session)
        store = InMemoryCounterStore()
        session.add(
            LimitRule(
                name="rolling",
                scope=LimitScope.GROUP,
                scope_id=group.id,
                metric=LimitMetric.COST,
                window_seconds=3600,
                limit_value=Decimal(100),
            )
        )
        await session.commit()
        await add_calendar_rule(
            session,
            scope=LimitScope.GROUP,
            scope_id=group.id,
            period=PeriodKind.MONTH,
            limit="100",
        )

        quota = engine_for(store)
        reservation = await quota.check_and_reserve(
            session, QuotaSubject(None, group.id), QuotaAmounts()
        )
        await quota.settle(reservation, QuotaAmounts(cost=Decimal(7)))

        scope = ScopeRef(LimitScope.GROUP, group.id)
        now = datetime.now(UTC)
        month = parse_period(now.strftime("%Y-%m"))
        rolling, calendar = await store.totals(
            [
                WindowQuery(scope=scope, metric=LimitMetric.COST, spec=quota._spec(3600)),
                WindowQuery(scope=scope, metric=LimitMetric.COST, period=month),
            ],
            now=now.timestamp(),
        )
        # Each records the spend once, in its own counter.
        assert from_units(LimitMetric.COST, rolling.units) == Decimal(7)
        assert from_units(LimitMetric.COST, calendar.units) == Decimal(7)


class TestBudgetAndReportAgree:
    async def test_a_monthly_quota_and_a_monthly_report_return_the_same_number(
        self, session: AsyncSession, session_factory: async_sessionmaker[AsyncSession]
    ) -> None:
        """The whole point of sharing one boundary implementation."""
        group = await make_group(session)
        now = datetime.now(UTC)
        month = parse_period(now.strftime("%Y-%m"))

        # Inside the month, and safely away from either edge.
        inside = max(month.start + timedelta(hours=1), now - timedelta(hours=1))
        await spend(session, group_id=group.id, cost="3.25", at=inside)
        # Before it: counted by neither.
        await spend(session, group_id=group.id, cost="99", at=month.start - timedelta(hours=1))

        db = DatabaseCounterStore(session_factory)
        quota_view = (
            await db.totals(
                [
                    WindowQuery(
                        scope=ScopeRef(LimitScope.GROUP, group.id),
                        metric=LimitMetric.COST,
                        period=month,
                    )
                ],
                now=now.timestamp(),
            )
        )[0]

        report_total = (
            (
                await session.execute(
                    select(UsageRecord.cost).where(
                        UsageRecord.group_id == group.id,
                        UsageRecord.created_at >= month.start,
                        UsageRecord.created_at < month.end,
                    )
                )
            )
            .scalars()
            .all()
        )

        assert from_units(LimitMetric.COST, quota_view.units) == sum(report_total)
        assert from_units(LimitMetric.COST, quota_view.units) == Decimal("3.25")


class TestApiKeyScope:
    async def test_a_key_can_have_its_own_budget(
        self, session: AsyncSession, session_factory: async_sessionmaker[AsyncSession]
    ) -> None:
        """ "This CI key gets EUR 5 a month" was previously inexpressible."""
        key_id = uuid.uuid4()
        await add_calendar_rule(
            session,
            scope=LimitScope.API_KEY,
            scope_id=key_id,
            period=PeriodKind.MONTH,
            limit="5",
        )
        quota = engine_for()
        subject = QuotaSubject(uuid.uuid4(), uuid.uuid4(), api_key_id=key_id)

        reservation = await quota.check_and_reserve(session, subject, QuotaAmounts())
        await quota.settle(reservation, QuotaAmounts(cost=Decimal(5)))

        with pytest.raises(QuotaExceeded) as caught:
            await quota.check_and_reserve(session, subject, QuotaAmounts())
        assert caught.value.violations[0].rule.scope is LimitScope.API_KEY

    async def test_another_key_is_unaffected(self, session: AsyncSession) -> None:
        key_id = uuid.uuid4()
        await add_calendar_rule(
            session,
            scope=LimitScope.API_KEY,
            scope_id=key_id,
            period=PeriodKind.MONTH,
            limit="0",
        )
        quota = engine_for()
        other = QuotaSubject(uuid.uuid4(), uuid.uuid4(), api_key_id=uuid.uuid4())
        assert (await quota.check_and_reserve(session, other, QuotaAmounts())).is_empty

    async def test_the_database_store_filters_by_key(
        self, session: AsyncSession, session_factory: async_sessionmaker[AsyncSession]
    ) -> None:
        key, other_key = await make_key(session), await make_key(session)
        key_id = key.id
        await spend(session, api_key_id=key_id, cost="2")
        await spend(session, api_key_id=other_key.id, cost="8")

        now = datetime.now(UTC)
        total = (
            await DatabaseCounterStore(session_factory).totals(
                [
                    WindowQuery(
                        scope=ScopeRef(LimitScope.API_KEY, key_id),
                        metric=LimitMetric.COST,
                        period=parse_period(now.strftime("%Y-%m")),
                    )
                ],
                now=now.timestamp(),
            )
        )[0]
        assert from_units(LimitMetric.COST, total.units) == Decimal(2)


class TestResets:
    async def test_a_reset_clears_consumption(
        self, session: AsyncSession, session_factory: async_sessionmaker[AsyncSession]
    ) -> None:
        group = await make_group(session)
        rule = await add_calendar_rule(
            session,
            scope=LimitScope.GROUP,
            scope_id=group.id,
            period=PeriodKind.MONTH,
            limit="5",
        )
        quota = engine_for()
        subject = QuotaSubject(None, group.id)

        reservation = await quota.check_and_reserve(session, subject, QuotaAmounts())
        await quota.settle(reservation, QuotaAmounts(cost=Decimal(5)))
        with pytest.raises(QuotaExceeded):
            await quota.check_and_reserve(session, subject, QuotaAmounts())

        session.add(QuotaReset(rule_id=rule.id, reason="budget increased mid-project"))
        await session.commit()

        # The watermark renames the counter key, so the old value is orphaned.
        assert not (await quota.check_and_reserve(session, subject, QuotaAmounts())).is_empty

    async def test_a_reset_survives_a_counter_cache_wipe(
        self, session: AsyncSession, session_factory: async_sessionmaker[AsyncSession]
    ) -> None:
        """The reason a reset is a watermark and not a counter mutation.

        Zeroing the counter would be undone by the cold-cache rebuild, which
        recomputes from the ledger. A watermark is *in* the ledger's database, so the
        rebuild honours it.
        """
        group = await make_group(session)
        rule = await add_calendar_rule(
            session,
            scope=LimitScope.GROUP,
            scope_id=group.id,
            period=PeriodKind.MONTH,
            limit="5",
        )
        # Minutes old, not now: a reset's watermark is a whole second, so spend
        # recorded in the same second as the reset still counts after it.
        await spend(
            session, group_id=group.id, cost="9", at=datetime.now(UTC) - timedelta(minutes=2)
        )

        session.add(QuotaReset(rule_id=rule.id, reason="wipe test"))
        await session.commit()

        # A brand-new cache, as after losing Valkey entirely.
        cache = InMemoryCounterStore()
        quota = engine_for(cache, fallback=DatabaseCounterStore(session_factory))
        # Asserted, not assumed: a rebuild that fails is caught and logged, so
        # without this the test would pass for the wrong reason.
        assert await quota.rebuild_if_cache_is_cold(session) is True

        # The 9 EUR predates the reset, so it no longer counts and the group can spend.
        assert not (
            await quota.check_and_reserve(session, QuotaSubject(None, group.id), QuotaAmounts())
        ).is_empty

    async def test_a_rebuild_restores_calendar_consumption_that_was_not_reset(
        self, session: AsyncSession, session_factory: async_sessionmaker[AsyncSession]
    ) -> None:
        """The other half: without a reset, a wiped cache must *not* hand out a fresh
        monthly budget. The rebuilt delta has to keep its period, or it lands under
        the wrong key and the group starts the month again at zero."""
        group = await make_group(session)
        await add_calendar_rule(
            session,
            scope=LimitScope.GROUP,
            scope_id=group.id,
            period=PeriodKind.MONTH,
            limit="5",
        )
        await spend(session, group_id=group.id, cost="9")

        quota = engine_for(InMemoryCounterStore(), fallback=DatabaseCounterStore(session_factory))
        assert await quota.rebuild_if_cache_is_cold(session) is True

        with pytest.raises(QuotaExceeded):
            await quota.check_and_reserve(session, QuotaSubject(None, group.id), QuotaAmounts())

    async def test_a_reset_does_not_change_the_billing_report(
        self, session: AsyncSession, session_factory: async_sessionmaker[AsyncSession]
    ) -> None:
        """Enforcement and accounting stay separate: usage_records is untouched."""
        group = await make_group(session)
        rule = await add_calendar_rule(
            session,
            scope=LimitScope.GROUP,
            scope_id=group.id,
            period=PeriodKind.MONTH,
            limit="5",
        )
        await spend(session, group_id=group.id, cost="9")

        before = (
            (
                await session.execute(
                    select(UsageRecord.cost).where(UsageRecord.group_id == group.id)
                )
            )
            .scalars()
            .all()
        )

        session.add(QuotaReset(rule_id=rule.id, reason="does not touch the ledger"))
        await session.commit()

        after = (
            (
                await session.execute(
                    select(UsageRecord.cost).where(UsageRecord.group_id == group.id)
                )
            )
            .scalars()
            .all()
        )
        assert before == after == [Decimal(9)]

    async def test_the_latest_reset_wins(self, session: AsyncSession) -> None:
        group = await make_group(session)
        rule = await add_calendar_rule(
            session,
            scope=LimitScope.GROUP,
            scope_id=group.id,
            period=PeriodKind.MONTH,
            limit="5",
        )
        old = datetime.now(UTC) - timedelta(days=2)
        session.add_all(
            [
                QuotaReset(rule_id=rule.id, effective_at=old, reason="older"),
                QuotaReset(rule_id=rule.id, reason="newer"),
            ]
        )
        await session.commit()
        await session.refresh(rule, attribute_names=["resets"])

        quota = engine_for()
        newest = max(reset.effective_at for reset in rule.resets)
        assert quota.reset_epoch_for(rule) == int(newest.timestamp())

    async def test_resets_are_kept_as_an_audit_trail(self, session: AsyncSession) -> None:
        """Zeroing a spending cap is financially meaningful; who and why are recorded."""
        group = await make_group(session)
        rule = await add_calendar_rule(
            session,
            scope=LimitScope.GROUP,
            scope_id=group.id,
            period=PeriodKind.MONTH,
            limit="5",
        )
        session.add(QuotaReset(rule_id=rule.id, reason="grant extension approved"))
        await session.commit()

        stored = (
            await session.execute(select(QuotaReset).where(QuotaReset.rule_id == rule.id))
        ).scalar_one()
        assert stored.reason == "grant extension approved"
        assert stored.effective_at is not None

    async def test_deleting_a_rule_removes_its_resets(self, session: AsyncSession) -> None:
        group = await make_group(session)
        rule = await add_calendar_rule(
            session,
            scope=LimitScope.GROUP,
            scope_id=group.id,
            period=PeriodKind.MONTH,
            limit="5",
        )
        session.add(QuotaReset(rule_id=rule.id, reason="x"))
        await session.commit()

        await session.delete(rule)
        await session.commit()
        remaining = (await session.execute(select(QuotaReset))).scalars().all()
        assert remaining == []


class TestPeriodBoundaryHazard:
    async def test_settlement_lands_in_the_period_the_reservation_used(
        self, session: AsyncSession
    ) -> None:
        """A stream starting 23:59:50 on the 31st settles at 00:00:10 on the 1st.

        Settling into "the current period" would leave the old month over-reserved
        and the new one under-counted — both wrong, once a month.
        """
        group = await make_group(session)
        await add_calendar_rule(
            session,
            scope=LimitScope.GROUP,
            scope_id=group.id,
            period=PeriodKind.MONTH,
            limit="100",
        )
        store = InMemoryCounterStore()
        quota = engine_for(store)

        month = parse_period("2026-08")
        just_before = month.end - timedelta(seconds=10)
        just_after = month.end + timedelta(seconds=10)

        reservation = await quota.check_and_reserve(
            session,
            QuotaSubject(None, group.id),
            QuotaAmounts(cost=Decimal(4)),
            now=just_before.timestamp(),
        )
        # Settled after the boundary has passed.
        await quota.settle(reservation, QuotaAmounts(cost=Decimal(1)), now=just_after.timestamp())

        scope = ScopeRef(LimitScope.GROUP, group.id)
        august = (
            await store.totals(
                [WindowQuery(scope=scope, metric=LimitMetric.COST, period=month)],
                now=just_before.timestamp(),
            )
        )[0]
        september = (
            await store.totals(
                [WindowQuery(scope=scope, metric=LimitMetric.COST, period=parse_period("2026-09"))],
                now=just_after.timestamp(),
            )
        )[0]

        # The whole cost belongs to August, netted to the actual.
        assert from_units(LimitMetric.COST, august.units) == Decimal(1)
        assert september.units == 0
