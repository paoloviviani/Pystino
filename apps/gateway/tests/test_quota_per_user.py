"""A global rule that caps each user instead of everyone together (issue P7).

A global rule with ``applies_to="each_user"`` gives every person their own
budget, counted over everything they spend: every group they bill to, their
personal usage, and API keys they own. The properties pinned here:

- admission: one person's cap refuses only them; spend through any group
  counts; an API key's spend counts against its owner; a new user is capped
  from their first request with no rule of their own; a subject with no user
  skips the rule;
- coexistence: all-must-pass with an aggregate global rule, a group aggregate,
  a group per-member rule and an explicit ``scope=user`` rule, and no override
  in either direction;
- counting: the rule reads the person's ordinary ``user`` counter (shared with
  a ``scope=user`` rule for the same metric, window and reset), with the same
  reserve -> settle -> refund flow, and the ledger rebuild agrees with live
  counting;
- upkeep: seeding, cold rebuild, reconcile and health reach every user, and do
  it with one grouped ledger query rather than one per person;
- reporting: the refusal says "per user" and names the person's own figure,
  the admin API validates the scope, and notifications go to the person whose
  budget it is.
"""

from __future__ import annotations

import asyncio
import uuid
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

import httpx
import pytest
from conftest import FakeUpstream, Seeded
from gateway.models import (
    LimitAppliesTo,
    LimitMetric,
    LimitRule,
    LimitScope,
    User,
)
from gateway.periods import PeriodKind, period_containing
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
from gateway.quota.notifications import QuotaNotifier
from helpers import completion_body
from sqlalchemy import event
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from test_chat_completions import basic_request
from test_quota_calendar import make_group, spend
from test_quota_engine import add_rule, engine_for
from test_quota_per_member import add_member_rule, make_member


async def add_user_rule(
    session: AsyncSession,
    *,
    metric: LimitMetric = LimitMetric.REQUESTS,
    limit: str = "3",
    window_seconds: int | None = 3600,
    period: PeriodKind | None = None,
    name: str = "each user",
) -> LimitRule:
    rule = LimitRule(
        name=name,
        scope=LimitScope.GLOBAL,
        scope_id=None,
        metric=metric,
        window_seconds=window_seconds if period is None else None,
        period=period,
        limit_value=Decimal(limit),
        applies_to=LimitAppliesTo.EACH_USER,
    )
    session.add(rule)
    await session.commit()
    await session.refresh(rule, attribute_names=["resets"])
    return rule


async def make_user(session: AsyncSession) -> User:
    handle = uuid.uuid4().hex[:8]
    user = User(
        issuer="https://idp.example.test",
        subject=f"s-{handle}",
        email=f"{handle}@example.test",
        display_name=handle,
    )
    session.add(user)
    await session.flush()
    return user


def month() -> Any:
    return period_containing(PeriodKind.MONTH, datetime.now(UTC), timezone="Europe/Rome")


def user_query(user_id: uuid.UUID, metric: LimitMetric = LimitMetric.REQUESTS) -> WindowQuery:
    return WindowQuery(scope=ScopeRef(LimitScope.USER, user_id), metric=metric, period=month())


async def one_request(
    quota: Any, session: AsyncSession, subject: QuotaSubject, **amounts: Decimal
) -> None:
    reservation = await quota.check_and_reserve(session, subject, QuotaAmounts(**amounts))
    await quota.settle(reservation, QuotaAmounts(**amounts))


class TestEachUserAdmission:
    async def test_one_user_hits_the_cap_while_another_passes(self, session: AsyncSession) -> None:
        first = await make_user(session)
        second = await make_user(session)
        await add_user_rule(session, limit="2")
        quota = engine_for()

        for _ in range(2):
            await one_request(quota, session, QuotaSubject(first.id, None))
        with pytest.raises(QuotaExceeded):
            await quota.check_and_reserve(session, QuotaSubject(first.id, None), QuotaAmounts())
        reservation = await quota.check_and_reserve(
            session, QuotaSubject(second.id, None), QuotaAmounts()
        )
        assert not reservation.is_empty

    async def test_a_brand_new_user_is_capped_from_the_first_request(
        self, session: AsyncSession
    ) -> None:
        """No per-user rule, no seeding for them: the first request already
        counts, and the cap bites without anyone configuring the person."""
        await add_user_rule(session, limit="1")
        newcomer = await make_user(session)
        quota = engine_for()

        await one_request(quota, session, QuotaSubject(newcomer.id, None))
        with pytest.raises(QuotaExceeded):
            await quota.check_and_reserve(session, QuotaSubject(newcomer.id, None), QuotaAmounts())

    async def test_spend_through_every_group_counts_towards_the_one_ceiling(
        self, session: AsyncSession
    ) -> None:
        """The ceiling is the person's, not the group's: the same user billing
        two groups spends one budget."""
        home = await make_group(session)
        away = await make_group(session)
        user = await make_user(session)
        await add_user_rule(session, limit="3")
        quota = engine_for()

        await one_request(quota, session, QuotaSubject(user.id, home.id))
        await one_request(quota, session, QuotaSubject(user.id, away.id))
        await one_request(quota, session, QuotaSubject(user.id, None))
        for group_id in (home.id, away.id, None):
            with pytest.raises(QuotaExceeded):
                await quota.check_and_reserve(
                    session, QuotaSubject(user.id, group_id), QuotaAmounts()
                )

    async def test_api_key_spend_counts_against_the_owner(self, session: AsyncSession) -> None:
        """A key request arrives as its owner (deps.py never sets api_key_id on
        the subject), so key spend and personal spend are one budget."""
        owner = await make_user(session)
        await add_user_rule(session, limit="2")
        quota = engine_for()

        await one_request(quota, session, QuotaSubject(owner.id, None, api_key_id=None))
        await one_request(quota, session, QuotaSubject(owner.id, None))
        with pytest.raises(QuotaExceeded):
            await quota.check_and_reserve(session, QuotaSubject(owner.id, None), QuotaAmounts())

    async def test_a_subject_with_no_user_skips_the_rule(self, session: AsyncSession) -> None:
        """A limit of zero refuses everything it applies to, so passing here
        proves the rule was skipped rather than evaluated."""
        await add_user_rule(session, limit="0")
        reservation = await engine_for().check_and_reserve(
            session, QuotaSubject(None, None), QuotaAmounts()
        )
        assert reservation.is_empty

    async def test_concurrent_requests_from_one_user_share_one_cap(
        self, session: AsyncSession, session_factory: async_sessionmaker[AsyncSession]
    ) -> None:
        user = await make_user(session)
        await add_user_rule(session, limit="3")
        quota = engine_for()
        subject = QuotaSubject(user.id, None)

        async def attempt() -> bool:
            async with session_factory() as own_session:
                try:
                    await quota.check_and_reserve(own_session, subject, QuotaAmounts())
                    return True
                except QuotaExceeded:
                    return False

        admitted = sum(await asyncio.gather(*(attempt() for _ in range(10))))
        assert admitted == 3, f"admitted {admitted} of 10"

    async def test_a_refund_returns_the_users_budget(self, session: AsyncSession) -> None:
        user = await make_user(session)
        await add_user_rule(session, limit="1")
        quota = engine_for()
        subject = QuotaSubject(user.id, None)

        reservation = await quota.check_and_reserve(session, subject, QuotaAmounts())
        await quota.release(reservation)
        # Released: the budget is whole again.
        again = await quota.check_and_reserve(session, subject, QuotaAmounts())
        assert not again.is_empty


class TestCoexistence:
    async def test_all_must_pass_with_the_other_kinds(self, session: AsyncSession) -> None:
        """Each kind can refuse while the others still allow, and none of them
        raises another's ceiling."""
        group = await make_group(session)
        user = await make_member(session, group)
        peer = await make_member(session, group)
        await add_user_rule(session, limit="4", name="each user")
        await add_rule(
            session,
            scope=LimitScope.GLOBAL,
            metric=LimitMetric.REQUESTS,
            limit="100",
            window_seconds=3600,
        )
        await add_rule(
            session,
            scope=LimitScope.GROUP,
            scope_id=group.id,
            metric=LimitMetric.REQUESTS,
            limit="6",
            window_seconds=3600,
        )
        await add_member_rule(session, scope_id=group.id, limit="3")
        quota = engine_for()
        subject = QuotaSubject(user.id, group.id)

        # Three requests: the per-member share (3) is the binding one.
        for _ in range(3):
            await one_request(quota, session, subject)
        with pytest.raises(QuotaExceeded) as caught:
            await quota.check_and_reserve(session, subject, QuotaAmounts())
        assert "group per member" in str(caught.value)
        assert "per user" not in str(caught.value)

        # Spent outside the group (billed to nobody), the same person reaches
        # their personal ceiling of 4 although the group share is not what
        # refuses them there.
        await one_request(quota, session, QuotaSubject(user.id, None))
        with pytest.raises(QuotaExceeded) as caught:
            await quota.check_and_reserve(session, QuotaSubject(user.id, None), QuotaAmounts())
        assert "global per user" in str(caught.value)

        # The peer is untouched by either personal ceiling.
        reservation = await quota.check_and_reserve(
            session, QuotaSubject(peer.id, group.id), QuotaAmounts()
        )
        assert not reservation.is_empty

    async def test_a_looser_user_rule_does_not_raise_the_global_ceiling(
        self, session: AsyncSession
    ) -> None:
        """No override: an explicit scope=user rule above the each-user ceiling
        changes nothing, the lower of the two refuses."""
        user = await make_user(session)
        await add_user_rule(session, limit="2")
        await add_rule(
            session,
            scope=LimitScope.USER,
            scope_id=user.id,
            metric=LimitMetric.REQUESTS,
            limit="100",
            window_seconds=3600,
        )
        quota = engine_for()
        subject = QuotaSubject(user.id, None)

        for _ in range(2):
            await one_request(quota, session, subject)
        with pytest.raises(QuotaExceeded) as caught:
            await quota.check_and_reserve(session, subject, QuotaAmounts())
        assert [v.rule.scope for v in caught.value.violations] == [LimitScope.GLOBAL]

    async def test_a_tighter_user_rule_still_binds_below_the_global_ceiling(
        self, session: AsyncSession
    ) -> None:
        user = await make_user(session)
        await add_user_rule(session, limit="100")
        await add_rule(
            session,
            scope=LimitScope.USER,
            scope_id=user.id,
            metric=LimitMetric.REQUESTS,
            limit="1",
            window_seconds=3600,
        )
        quota = engine_for()
        subject = QuotaSubject(user.id, None)

        await one_request(quota, session, subject)
        with pytest.raises(QuotaExceeded) as caught:
            await quota.check_and_reserve(session, subject, QuotaAmounts())
        assert [v.rule.scope for v in caught.value.violations] == [LimitScope.USER]

    async def test_the_same_window_shares_the_users_counter(self, session: AsyncSession) -> None:
        """A scope=user rule and an each-user rule on one metric and window
        read one counter: a single delta per request, not two."""
        user = await make_user(session)
        await add_user_rule(session, limit="100")
        await add_rule(
            session,
            scope=LimitScope.USER,
            scope_id=user.id,
            metric=LimitMetric.REQUESTS,
            limit="50",
            window_seconds=3600,
        )
        store = InMemoryCounterStore()
        quota = engine_for(store)

        reservation = await quota.check_and_reserve(
            session, QuotaSubject(user.id, None), QuotaAmounts()
        )
        assert [d.scope.key_part for d in reservation.deltas] == [f"user:{user.id}"]
        await quota.settle(reservation, QuotaAmounts(requests=Decimal(1)))

    async def test_a_global_aggregate_and_each_user_rule_are_independent(
        self, session: AsyncSession
    ) -> None:
        first = await make_user(session)
        second = await make_user(session)
        await add_user_rule(session, limit="5")
        await add_rule(
            session,
            scope=LimitScope.GLOBAL,
            metric=LimitMetric.REQUESTS,
            limit="3",
            window_seconds=3600,
        )
        quota = engine_for()

        await one_request(quota, session, QuotaSubject(first.id, None))
        await one_request(quota, session, QuotaSubject(second.id, None))
        await one_request(quota, session, QuotaSubject(first.id, None))
        # Everyone together is at 3: the aggregate refuses the second user
        # although their own budget has plenty left.
        with pytest.raises(QuotaExceeded) as caught:
            await quota.check_and_reserve(session, QuotaSubject(second.id, None), QuotaAmounts())
        assert "per user" not in str(caught.value)


class TestEachUserCounters:
    async def test_reserve_settle_use_the_users_own_key(self, session: AsyncSession) -> None:
        first = await make_user(session)
        second = await make_user(session)
        await add_user_rule(session, limit="100", period=PeriodKind.MONTH)
        store = InMemoryCounterStore()
        quota = engine_for(store)
        now = datetime.now(UTC).timestamp()

        reservation = await quota.check_and_reserve(
            session, QuotaSubject(first.id, None), QuotaAmounts()
        )
        assert {d.scope.key_part for d in reservation.deltas} == {f"user:{first.id}"}
        await quota.settle(reservation, QuotaAmounts(requests=Decimal(1)))
        totals = await store.totals([user_query(first.id), user_query(second.id)], now=now)
        assert [total.units for total in totals] == [1, 0]

    async def test_rebuild_from_the_ledger_matches_live_counting(
        self, session: AsyncSession, session_factory: async_sessionmaker[AsyncSession]
    ) -> None:
        """The ledger filtered by user alone, across every group, rebuilds the
        key live counting wrote."""
        user = await make_user(session)
        other = await make_user(session)
        group = await make_group(session)
        away = await make_group(session)
        await add_user_rule(session, metric=LimitMetric.COST, limit="1000", period=PeriodKind.MONTH)
        live = InMemoryCounterStore()
        quota = engine_for(live)
        for group_id in (group.id, away.id, None):
            await one_request(quota, session, QuotaSubject(user.id, group_id), cost=Decimal("2"))
        now = datetime.now(UTC).timestamp()
        cost = user_query(user.id, LimitMetric.COST)
        assert from_units(
            LimitMetric.COST, (await live.totals([cost], now=now))[0].units
        ) == Decimal("6")

        for group_id in (group.id, away.id, None):
            await spend(session, group_id=group_id, user_id=user.id, cost="2")
        await spend(session, group_id=group.id, user_id=other.id, cost="100")
        rebuilt = InMemoryCounterStore()
        await DatabaseCounterStore(session_factory).rebuild_into(
            rebuilt, [cost, user_query(other.id, LimitMetric.COST)], now=now
        )
        totals = await rebuilt.totals([cost, user_query(other.id, LimitMetric.COST)], now=now)
        assert [from_units(LimitMetric.COST, t.units) for t in totals] == [
            Decimal("6"),
            Decimal("100"),
        ]


class TestEachUserUpkeep:
    async def test_seeding_primes_every_spender_with_one_grouped_query(
        self, session: AsyncSession, session_factory: async_sessionmaker[AsyncSession]
    ) -> None:
        group = await make_group(session)
        spenders = [await make_user(session) for _ in range(5)]
        idle = await make_user(session)
        rule = await add_user_rule(
            session, metric=LimitMetric.COST, limit="1000", period=PeriodKind.MONTH
        )
        for index, spender in enumerate(spenders):
            await spend(session, group_id=group.id, user_id=spender.id, cost=str(index + 1))
        await spend(session, group_id=None, user_id=spenders[0].id, cost="10")
        await session.commit()

        cache = InMemoryCounterStore()
        quota = engine_for(cache, fallback=DatabaseCounterStore(session_factory))
        statements: list[str] = []
        bind = session_factory.kw["bind"].sync_engine

        def record(conn: Any, cursor: Any, statement: str, *_: Any) -> None:
            statements.append(statement)

        event.listen(bind, "before_cursor_execute", record)
        try:
            assert await quota.seed_rule(session, rule) is True
        finally:
            event.remove(bind, "before_cursor_execute", record)

        now = datetime.now(UTC).timestamp()
        totals = await cache.totals(
            [user_query(u.id, LimitMetric.COST) for u in [*spenders, idle]], now=now
        )
        assert [from_units(LimitMetric.COST, t.units) for t in totals] == [
            Decimal("11"),
            Decimal("2"),
            Decimal("3"),
            Decimal("4"),
            Decimal("5"),
            Decimal("0"),
        ]
        # Who spent (distinct), then every spender's total in one GROUP BY:
        # two ledger reads, however many users there are.
        ledger = [s for s in statements if "usage_records" in s.lower()]
        assert len(ledger) == 2, ledger

    async def test_a_cold_cache_is_rebuilt_for_each_user(
        self, session: AsyncSession, session_factory: async_sessionmaker[AsyncSession]
    ) -> None:
        group = await make_group(session)
        user = await make_user(session)
        await add_user_rule(session, metric=LimitMetric.COST, limit="1000", period=PeriodKind.MONTH)
        await spend(session, group_id=group.id, user_id=user.id, cost="7")
        cache = InMemoryCounterStore()
        quota = engine_for(cache, fallback=DatabaseCounterStore(session_factory))

        assert await quota.rebuild_if_cache_is_cold(session) is True
        now = datetime.now(UTC).timestamp()
        totals = await cache.totals([user_query(user.id, LimitMetric.COST)], now=now)
        assert from_units(LimitMetric.COST, totals[0].units) == Decimal("7")

    async def test_reconcile_repairs_drift_for_users_with_and_without_usage(
        self, session: AsyncSession, session_factory: async_sessionmaker[AsyncSession]
    ) -> None:
        group = await make_group(session)
        spender = await make_user(session)
        idle = await make_user(session)
        await add_user_rule(session, metric=LimitMetric.COST, limit="1000", period=PeriodKind.MONTH)
        await spend(session, group_id=group.id, user_id=spender.id, cost="4")
        cache = InMemoryCounterStore()
        now = datetime.now(UTC).timestamp()
        # Drift both ways: too low for the spender, a phantom for the idle user.
        from gateway.quota import CounterDelta
        from gateway.quota.counters import to_units

        for user, amount in ((spender, "1"), (idle, "9")):
            query = user_query(user.id, LimitMetric.COST)
            await cache.apply(
                [
                    CounterDelta(
                        scope=query.scope,
                        metric=query.metric,
                        units=to_units(LimitMetric.COST, Decimal(amount)),
                        period=query.period,
                    )
                ],
                now=now,
            )
        quota = engine_for(cache, fallback=DatabaseCounterStore(session_factory))

        done = await quota.reconcile(session)

        by_user = {entry.member_id: entry for entry in done}
        assert by_user[spender.id].before == Decimal("1")
        assert by_user[spender.id].after == Decimal("4")
        assert by_user[idle.id].before == Decimal("9")
        assert by_user[idle.id].after == Decimal("0")

    async def test_health_reports_one_window_per_user(
        self, session: AsyncSession, session_factory: async_sessionmaker[AsyncSession]
    ) -> None:
        group = await make_group(session)
        user = await make_user(session)
        other = await make_user(session)
        await add_user_rule(session, metric=LimitMetric.COST, limit="1000", period=PeriodKind.MONTH)
        await spend(session, group_id=group.id, user_id=user.id, cost="3")
        quota = engine_for(InMemoryCounterStore(), fallback=DatabaseCounterStore(session_factory))

        health = await quota.health(session)

        by_user = {window.member_id: window for window in health.windows}
        assert {user.id, other.id} <= set(by_user)
        assert by_user[user.id].ledger == Decimal("3")
        assert by_user[other.id].ledger == Decimal("0")

    async def test_the_grouped_ledger_agrees_with_the_single_query_path(
        self, session: AsyncSession, session_factory: async_sessionmaker[AsyncSession]
    ) -> None:
        """Every metric, grouped against one at a time: the same numbers."""
        group = await make_group(session)
        users = [await make_user(session) for _ in range(3)]
        for index, user in enumerate(users):
            for _ in range(index + 1):
                await spend(session, group_id=group.id, user_id=user.id, cost="1.5")
        db = DatabaseCounterStore(session_factory)
        now = datetime.now(UTC).timestamp()
        for metric in (LimitMetric.REQUESTS, LimitMetric.TOKENS, LimitMetric.COST):
            queries = [user_query(u.id, metric) for u in users]
            grouped = await db.totals(queries, now=now)
            single = [(await db.totals([query], now=now))[0] for query in queries]
            assert [t.units for t in grouped] == [t.units for t in single]
            assert any(t.units for t in grouped)


class TestEachUserNotifications:
    async def test_only_the_person_whose_budget_it_is_is_told(
        self, session_factory: async_sessionmaker[AsyncSession]
    ) -> None:
        """The percentage belongs to the requester: another subscriber to the
        same rule must not be mailed for it."""
        rule = LimitRule(
            id=uuid.uuid4(),
            name="each user",
            scope=LimitScope.GLOBAL,
            metric=LimitMetric.REQUESTS,
            window_seconds=3600,
            limit_value=Decimal(10),
            applies_to=LimitAppliesTo.EACH_USER,
        )
        asker, bystander = uuid.uuid4(), uuid.uuid4()
        notifier = QuotaNotifier(session_factory, None, None)  # type: ignore[arg-type]
        notifier._subscriptions = {rule.id: [(asker, 80), (bystander, 80)]}
        sent: list[uuid.UUID] = []
        notifier._schedule_send = lambda rule, user_id, threshold, pct: sent.append(user_id)  # type: ignore[method-assign]

        notifier.observe([(rule, 90)], requester=asker)

        assert sent == [asker]


class TestEachUserAdminApi:
    async def test_creating_a_global_each_user_rule(
        self,
        app: Any,
        client: httpx.AsyncClient,
        seeded: Seeded,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        from test_admin import as_user, make_admin

        as_user(app, await make_admin(session_factory, seeded))
        created = await client.post(
            "/api/admin/limits",
            json={
                "name": "personal ceiling",
                "scope": "global",
                "metric": "cost",
                "period": "month",
                "limit_value": "20",
                "applies_to": "each_user",
            },
        )
        assert created.status_code == 201, created.text
        body = created.json()
        assert body["applies_to"] == "each_user"
        assert body["scope"] == "global"
        # No single figure for a per-person rule, as for a per-member one.
        assert body["current_value"] is None

    @pytest.mark.parametrize("scope", ["group", "user"])
    async def test_each_user_outside_the_global_scope_is_a_400(
        self,
        app: Any,
        client: httpx.AsyncClient,
        seeded: Seeded,
        session_factory: async_sessionmaker[AsyncSession],
        scope: str,
    ) -> None:
        from test_admin import as_user, make_admin

        as_user(app, await make_admin(session_factory, seeded))
        scope_id = seeded.group.id if scope == "group" else seeded.user.id
        response = await client.post(
            "/api/admin/limits",
            json={
                "name": "nope",
                "scope": scope,
                "scope_id": str(scope_id),
                "metric": "cost",
                "period": "month",
                "limit_value": "20",
                "applies_to": "each_user",
            },
        )
        assert response.status_code == 400, response.text

    async def test_each_member_on_a_global_rule_is_still_refused(
        self,
        app: Any,
        client: httpx.AsyncClient,
        seeded: Seeded,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        from test_admin import as_user, make_admin

        as_user(app, await make_admin(session_factory, seeded))
        response = await client.post(
            "/api/admin/limits",
            json={
                "name": "nope",
                "scope": "global",
                "metric": "cost",
                "period": "month",
                "limit_value": "20",
                "applies_to": "each_member",
            },
        )
        assert response.status_code == 400, response.text

    async def test_the_database_refuses_each_user_off_the_global_scope(
        self, session: AsyncSession
    ) -> None:
        from sqlalchemy.exc import IntegrityError

        group = await make_group(session)
        session.add(
            LimitRule(
                name="bypass",
                scope=LimitScope.GROUP,
                scope_id=group.id,
                metric=LimitMetric.COST,
                window_seconds=60,
                limit_value=Decimal(1),
                applies_to=LimitAppliesTo.EACH_USER,
            )
        )
        with pytest.raises(IntegrityError):
            await session.commit()
        await session.rollback()

    async def test_one_rule_of_each_kind_per_window(
        self,
        app: Any,
        client: httpx.AsyncClient,
        seeded: Seeded,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        from test_admin import as_user, make_admin

        as_user(app, await make_admin(session_factory, seeded))

        async def create(applies_to: str) -> httpx.Response:
            return await client.post(
                "/api/admin/limits",
                json={
                    "name": f"global {applies_to}",
                    "scope": "global",
                    "metric": "cost",
                    "period": "month",
                    "limit_value": "50",
                    "applies_to": applies_to,
                },
            )

        assert (await create("aggregate")).status_code == 201
        assert (await create("each_user")).status_code == 201
        assert (await create("aggregate")).status_code == 409
        assert (await create("each_user")).status_code == 409


class TestEachUserRefusal:
    async def test_429_names_the_per_user_rule(
        self,
        client: httpx.AsyncClient,
        seeded: Seeded,
        fake_upstream: FakeUpstream,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        async with session_factory() as db:
            db.add(
                LimitRule(
                    name="personal ceiling",
                    scope=LimitScope.GLOBAL,
                    metric=LimitMetric.COST,
                    window_seconds=3600,
                    limit_value=Decimal(0),
                    applies_to=LimitAppliesTo.EACH_USER,
                )
            )
            await db.commit()
        fake_upstream.set_json(completion_body())
        response = await client.post(
            "/v1/chat/completions", json=basic_request(), headers=seeded.auth
        )
        assert response.status_code == 429
        body = response.json()
        assert body["error"]["code"] == "quota_exceeded"
        assert "global per user limit" in body["error"]["message"]

    async def test_a_users_own_limits_list_shows_the_rule_with_their_figure(
        self,
        app: Any,
        client: httpx.AsyncClient,
        seeded: Seeded,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        from test_my_limits import session_cookie

        async with session_factory() as db:
            db.add(
                LimitRule(
                    name="personal ceiling",
                    scope=LimitScope.GLOBAL,
                    metric=LimitMetric.REQUESTS,
                    window_seconds=3600,
                    limit_value=Decimal(100),
                    applies_to=LimitAppliesTo.EACH_USER,
                )
            )
            await db.commit()
        fake = app.state.quota_engine
        subject = QuotaSubject(seeded.user.id, None)
        async with session_factory() as db:
            await one_request(fake, db, subject)

        response = await client.get("/api/me/limits", headers=session_cookie(seeded.user.id, app))
        assert response.status_code == 200, response.text
        (entry,) = [r for r in response.json()["items"] if r["name"] == "personal ceiling"]
        assert entry["applies_to"] == "each_user"
        assert entry["scope"] == "global"
        assert Decimal(entry["current_value"]) == Decimal(1)
