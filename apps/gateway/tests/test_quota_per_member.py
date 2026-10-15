"""Group quota rules that cap each member instead of the group's total (ADR 0098).

A group rule with ``applies_to="each_member"`` counts a member's own usage
billed to that group — ``(group, user)`` — instead of every member's usage
together. The properties pinned here:

- admission: one member's cap refuses only them; spend billed elsewhere does
  not count; aggregate and per-member rules are both enforced; an API key's
  spend counts against its owner's share; a subject with no user skips
  per-member rules rather than enforcing a budget it cannot attribute;
- counting: the per-member key is separate in every store, with the same
  reserve -> settle -> refund flow, and the ledger rebuild agrees with live
  counting;
- reporting: a refusal names the member's share (never anyone else's usage),
  and the 429 body says which kind ran out.
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
    Group,
    LimitAppliesTo,
    LimitMetric,
    LimitRule,
    LimitScope,
    Membership,
    MembershipSource,
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
from helpers import completion_body
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from test_chat_completions import basic_request
from test_quota_calendar import make_group, spend
from test_quota_engine import add_rule, engine_for


async def add_member_rule(
    session: AsyncSession,
    *,
    scope_id: uuid.UUID,
    metric: LimitMetric = LimitMetric.REQUESTS,
    limit: str = "3",
    window_seconds: int | None = 3600,
    period: PeriodKind | None = None,
    name: str = "per-member",
) -> LimitRule:
    rule = LimitRule(
        name=name,
        scope=LimitScope.GROUP,
        scope_id=scope_id,
        metric=metric,
        window_seconds=window_seconds if period is None else None,
        period=period,
        limit_value=Decimal(limit),
        applies_to=LimitAppliesTo.EACH_MEMBER,
    )
    session.add(rule)
    await session.commit()
    await session.refresh(rule, attribute_names=["resets"])
    return rule


async def make_member(session: AsyncSession, group: Group) -> User:
    handle = uuid.uuid4().hex[:8]
    user = User(
        issuer="https://idp.example.test",
        subject=f"s-{handle}",
        email=f"{handle}@example.test",
        display_name=handle,
    )
    session.add(user)
    await session.flush()
    session.add(Membership(user_id=user.id, group_id=group.id, source=MembershipSource.MANUAL))
    await session.flush()
    return user


def member_query(group_id: uuid.UUID, user_id: uuid.UUID) -> WindowQuery:
    return WindowQuery(
        scope=ScopeRef(LimitScope.GROUP, group_id, member_id=user_id),
        metric=LimitMetric.REQUESTS,
        period=period_containing(PeriodKind.MONTH, datetime.now(UTC), timezone="Europe/Rome"),
    )


class TestScopeRefMember:
    def test_member_key_names_group_and_user(self) -> None:
        group_id = uuid.uuid4()
        user_id = uuid.uuid4()
        ref = ScopeRef(LimitScope.GROUP, group_id, member_id=user_id)
        assert ref.key_part == f"group:{group_id}:member:{user_id}"

    def test_member_id_outside_a_group_is_refused(self) -> None:
        user_id = uuid.uuid4()
        with pytest.raises(ValueError):
            ScopeRef(LimitScope.USER, user_id, member_id=uuid.uuid4())
        with pytest.raises(ValueError):
            ScopeRef(LimitScope.GLOBAL, member_id=uuid.uuid4())

    def test_plain_group_key_is_unchanged(self) -> None:
        group_id = uuid.uuid4()
        assert ScopeRef(LimitScope.GROUP, group_id).key_part == f"group:{group_id}"


class TestPerMemberAdmission:
    async def test_one_member_hits_the_cap_while_another_passes(
        self, session: AsyncSession
    ) -> None:
        """The member's share refuses only them: same group, same window."""
        group = await make_group(session)
        first = await make_member(session, group)
        second = await make_member(session, group)
        await add_member_rule(session, scope_id=group.id, limit="2")
        quota = engine_for()

        for _ in range(2):
            reservation = await quota.check_and_reserve(
                session, QuotaSubject(first.id, group.id), QuotaAmounts()
            )
            await quota.settle(reservation, QuotaAmounts(requests=Decimal(1)))

        with pytest.raises(QuotaExceeded):
            await quota.check_and_reserve(session, QuotaSubject(first.id, group.id), QuotaAmounts())
        # The second member's own budget is untouched.
        reservation = await quota.check_and_reserve(
            session, QuotaSubject(second.id, group.id), QuotaAmounts()
        )
        assert not reservation.is_empty

    async def test_spend_billed_elsewhere_does_not_count(self, session: AsyncSession) -> None:
        """Only usage billed *to this group* counts against the member's share."""
        home = await make_group(session)
        away = await make_group(session)
        user = await make_member(session, home)
        await add_member_rule(session, scope_id=home.id, limit="1", period=PeriodKind.MONTH)
        await add_member_rule(
            session, scope_id=away.id, limit="100", period=PeriodKind.MONTH, name="away share"
        )
        store = InMemoryCounterStore()
        quota = engine_for(store)
        now = datetime.now(UTC).timestamp()

        for _ in range(5):
            reservation = await quota.check_and_reserve(
                session, QuotaSubject(user.id, away.id), QuotaAmounts()
            )
            await quota.settle(reservation, QuotaAmounts(requests=Decimal(1)))

        # Five requests billed elsewhere: nothing landed on the home share.
        totals = await store.totals(
            [member_query(home.id, user.id), member_query(away.id, user.id)], now=now
        )
        assert [total.units for total in totals] == [0, 5]
        reservation = await quota.check_and_reserve(
            session, QuotaSubject(user.id, home.id), QuotaAmounts()
        )
        assert not reservation.is_empty

    async def test_aggregate_and_per_member_are_both_enforced(self, session: AsyncSession) -> None:
        """Both kinds on one group: each can refuse while the other still allows."""
        group = await make_group(session)
        first = await make_member(session, group)
        second = await make_member(session, group)
        await add_rule(
            session,
            scope=LimitScope.GROUP,
            scope_id=group.id,
            metric=LimitMetric.REQUESTS,
            limit="3",
        )
        await add_member_rule(session, scope_id=group.id, limit="2")
        quota = engine_for()

        # Two requests by the first member: their share is spent, the group
        # still has room.
        for _ in range(2):
            reservation = await quota.check_and_reserve(
                session, QuotaSubject(first.id, group.id), QuotaAmounts()
            )
            await quota.settle(reservation, QuotaAmounts(requests=Decimal(1)))
        with pytest.raises(QuotaExceeded) as caught:
            await quota.check_and_reserve(session, QuotaSubject(first.id, group.id), QuotaAmounts())
        assert "per member" in str(caught.value).lower()

        # The second member still has a share — but the group total is now
        # spent (2 used of 3, and the reserve of this very request makes 3),
        # so the aggregate rule refuses them instead.
        reservation = await quota.check_and_reserve(
            session, QuotaSubject(second.id, group.id), QuotaAmounts()
        )
        await quota.settle(reservation, QuotaAmounts(requests=Decimal(1)))
        with pytest.raises(QuotaExceeded) as caught:
            await quota.check_and_reserve(
                session, QuotaSubject(second.id, group.id), QuotaAmounts()
            )
        assert "per member" not in str(caught.value).lower()

    async def test_api_key_spend_counts_against_the_owners_share(
        self, session: AsyncSession
    ) -> None:
        """A key has no budget of its own: its spend is its owner's spend."""
        group = await make_group(session)
        owner = await make_member(session, group)
        await add_member_rule(session, scope_id=group.id, limit="1")
        quota = engine_for()
        # The request path never sets api_key_id on the subject (deps.py): a
        # key request arrives as its owner charging the billing group.
        subject = QuotaSubject(owner.id, group.id)

        reservation = await quota.check_and_reserve(session, subject, QuotaAmounts())
        await quota.settle(reservation, QuotaAmounts(requests=Decimal(1)))
        with pytest.raises(QuotaExceeded):
            await quota.check_and_reserve(session, subject, QuotaAmounts())

    async def test_a_subject_with_no_user_skips_per_member_rules(
        self, session: AsyncSession
    ) -> None:
        """No user to attribute to means no per-member rule can apply.

        A limit of zero refuses everything it applies to, so passing here
        proves the rule was skipped rather than evaluated.
        """
        group = await make_group(session)
        await add_member_rule(session, scope_id=group.id, limit="0")

        reservation = await engine_for().check_and_reserve(
            session, QuotaSubject(None, group.id), QuotaAmounts()
        )
        assert reservation.is_empty

    async def test_concurrent_requests_from_one_member_share_one_cap(
        self, session: AsyncSession, session_factory: async_sessionmaker[AsyncSession]
    ) -> None:
        """Ten requests at once from one member against a ceiling of three."""
        group = await make_group(session)
        user = await make_member(session, group)
        await add_member_rule(session, scope_id=group.id, limit="3")
        quota = engine_for()
        subject = QuotaSubject(user.id, group.id)

        async def attempt() -> bool:
            async with session_factory() as own_session:
                try:
                    await quota.check_and_reserve(own_session, subject, QuotaAmounts())
                    return True
                except QuotaExceeded:
                    return False

        admitted = sum(await asyncio.gather(*(attempt() for _ in range(10))))
        assert admitted == 3, f"admitted {admitted} of 10"


class TestPerMemberViolation:
    async def test_the_refusal_names_the_members_share(self, session: AsyncSession) -> None:
        """The message says whose budget ran out — the share, not the total.

        The member's own current is theirs to know; the group's total is
        deliberately absent, so one member's 429 never reports the group's
        spend to another member.
        """
        group = await make_group(session)
        user = await make_member(session, group)
        other = await make_member(session, group)
        await add_member_rule(
            session, scope_id=group.id, metric=LimitMetric.COST, limit="50", window_seconds=3600
        )
        quota = engine_for()
        for subject_user in (user, other):
            reservation = await quota.check_and_reserve(
                session,
                QuotaSubject(subject_user.id, group.id),
                QuotaAmounts(cost=Decimal("750")),
            )
            await quota.settle(reservation, QuotaAmounts(cost=Decimal("750")))

        with pytest.raises(QuotaExceeded) as caught:
            await quota.check_and_reserve(
                session, QuotaSubject(user.id, group.id), QuotaAmounts(cost=Decimal("1"))
            )
        message = str(caught.value)
        assert "per member" in message.lower()
        # The group's total (€1500) must not appear; the member's own €750 may.
        assert "1500" not in message
        assert "750" in message


class TestPerMemberCounters:
    async def test_reserve_settle_refund_touch_only_the_members_key(
        self, session: AsyncSession
    ) -> None:
        group = await make_group(session)
        first = await make_member(session, group)
        second = await make_member(session, group)
        await add_member_rule(session, scope_id=group.id, limit="100", period=PeriodKind.MONTH)
        await add_rule(
            session,
            scope=LimitScope.GROUP,
            scope_id=group.id,
            metric=LimitMetric.REQUESTS,
            limit="100",
        )
        store = InMemoryCounterStore()
        quota = engine_for(store)
        now = datetime.now(UTC).timestamp()

        reservation = await quota.check_and_reserve(
            session, QuotaSubject(first.id, group.id), QuotaAmounts()
        )
        parts = {delta.scope.key_part for delta in reservation.deltas}
        assert parts == {f"group:{group.id}:member:{first.id}", f"group:{group.id}"}
        await quota.settle(reservation, QuotaAmounts(requests=Decimal(1)))
        totals = await store.totals(
            [member_query(group.id, first.id), member_query(group.id, second.id)], now=now
        )
        assert [total.units for total in totals] == [1, 0]

        # A second request reserved and then released leaves the first
        # request's settled unit behind and nothing else.
        again = await quota.check_and_reserve(
            session, QuotaSubject(first.id, group.id), QuotaAmounts()
        )
        await quota.release(again, now=now)
        totals = await store.totals([member_query(group.id, first.id)], now=now)
        assert [total.units for total in totals] == [1]

    async def test_rebuild_from_the_ledger_matches_live_counting(
        self, session: AsyncSession, session_factory: async_sessionmaker[AsyncSession]
    ) -> None:
        """The ledger filtered by group AND user rebuilds the member key.

        Live counting (reserve+settle into one store) and a ledger rebuild
        (rows into a second store) must agree, or the cold-cache path silently
        hands members a fresh budget. Spend billed to another group, and
        another member's spend, land elsewhere.
        """
        group = await make_group(session)
        away = await make_group(session)
        user = await make_member(session, group)
        other = await make_member(session, group)
        await add_member_rule(
            session,
            scope_id=group.id,
            metric=LimitMetric.COST,
            limit="1000",
            period=PeriodKind.MONTH,
        )
        live = InMemoryCounterStore()
        quota = engine_for(live)
        subject = QuotaSubject(user.id, group.id)

        for _ in range(3):
            reservation = await quota.check_and_reserve(
                session, subject, QuotaAmounts(cost=Decimal("2"))
            )
            await quota.settle(reservation, QuotaAmounts(cost=Decimal("2")))

        now = datetime.now(UTC).timestamp()
        period = period_containing(PeriodKind.MONTH, datetime.now(UTC), timezone="Europe/Rome")

        def cost_query(member: User) -> WindowQuery:
            return WindowQuery(
                scope=ScopeRef(LimitScope.GROUP, group.id, member_id=member.id),
                metric=LimitMetric.COST,
                period=period,
            )

        # What live counting holds for the member...
        live_totals = await live.totals([cost_query(user)], now=now)
        assert from_units(LimitMetric.COST, live_totals[0].units) == Decimal("6")

        # ...against what the ledger holds for the same member: the same €6
        # written as rows, plus another member's €100 and this member's €100
        # billed to a different group.
        for _ in range(3):
            await spend(session, group_id=group.id, user_id=user.id, cost="2")
        await spend(session, group_id=group.id, user_id=other.id, cost="100")
        await spend(session, group_id=away.id, user_id=user.id, cost="100")
        await session.commit()

        rebuilt = InMemoryCounterStore()
        db = DatabaseCounterStore(session_factory)
        await db.rebuild_into(rebuilt, [cost_query(user), cost_query(other)], now=now)
        totals = await rebuilt.totals([cost_query(user), cost_query(other)], now=now)
        assert [from_units(LimitMetric.COST, total.units) for total in totals] == [
            Decimal("6"),
            Decimal("100"),
        ]


class TestPerMemberSeed:
    async def test_seeding_primes_the_members_who_spent(
        self, session: AsyncSession, session_factory: async_sessionmaker[AsyncSession]
    ) -> None:
        """A new per-member rule starts its spenders where the ledger says.

        Members with no usage seed to nothing — a scope with no history costs
        no keys — so only the spenders' keys exist afterwards.
        """
        group = await make_group(session)
        user = await make_member(session, group)
        idle = await make_member(session, group)
        rule = await add_member_rule(
            session,
            scope_id=group.id,
            metric=LimitMetric.COST,
            limit="1000",
            period=PeriodKind.MONTH,
        )
        await spend(session, group_id=group.id, user_id=user.id, cost="7")
        await session.commit()

        cache = InMemoryCounterStore()
        quota = engine_for(cache, fallback=DatabaseCounterStore(session_factory))
        assert await quota.seed_rule(session, rule) is True

        now = datetime.now(UTC).timestamp()
        period = period_containing(PeriodKind.MONTH, datetime.now(UTC), timezone="Europe/Rome")

        def cost_query(member: User) -> WindowQuery:
            return WindowQuery(
                scope=ScopeRef(LimitScope.GROUP, group.id, member_id=member.id),
                metric=LimitMetric.COST,
                period=period,
            )

        totals = await cache.totals([cost_query(user), cost_query(idle)], now=now)
        assert [from_units(LimitMetric.COST, total.units) for total in totals] == [
            Decimal("7"),
            Decimal("0"),
        ]


class TestPerMemberHealth:
    async def test_health_reports_one_window_per_member(
        self, session: AsyncSession, session_factory: async_sessionmaker[AsyncSession]
    ) -> None:
        """Drift is per member, so health is per member — with the member named."""
        group = await make_group(session)
        user = await make_member(session, group)
        other = await make_member(session, group)
        await add_member_rule(
            session,
            scope_id=group.id,
            metric=LimitMetric.COST,
            limit="1000",
            period=PeriodKind.MONTH,
        )
        await spend(session, group_id=group.id, user_id=user.id, cost="3")
        await session.commit()
        quota = engine_for(InMemoryCounterStore(), fallback=DatabaseCounterStore(session_factory))

        health = await quota.health(session)

        assert {window.member_id for window in health.windows} == {user.id, other.id}
        by_member = {window.member_id: window for window in health.windows}
        assert by_member[user.id].ledger == Decimal("3")
        assert by_member[other.id].ledger == Decimal("0")


class TestPerMemberAdminApi:
    async def test_creating_a_per_member_group_rule(
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
                "name": "member share",
                "scope": "group",
                "scope_id": str(seeded.group.id),
                "metric": "cost",
                "period": "month",
                "limit_value": "50",
                "applies_to": "each_member",
            },
        )
        assert created.status_code == 201, created.text
        assert created.json()["applies_to"] == "each_member"

    async def test_each_member_outside_a_group_is_a_400(
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
                "name": "personal share",
                "scope": "user",
                "scope_id": str(seeded.user.id),
                "metric": "cost",
                "period": "month",
                "limit_value": "50",
                "applies_to": "each_member",
            },
        )
        assert response.status_code == 400, response.text

    async def test_one_rule_of_each_kind_per_window(
        self,
        app: Any,
        client: httpx.AsyncClient,
        seeded: Seeded,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        """The widened uniqueness: aggregate + per-member together is fine,
        a second of either kind is a 409."""
        from test_admin import as_user, make_admin

        as_user(app, await make_admin(session_factory, seeded))

        async def create(applies_to: str) -> httpx.Response:
            return await client.post(
                "/api/admin/limits",
                json={
                    "name": f"share {applies_to}",
                    "scope": "group",
                    "scope_id": str(seeded.group.id),
                    "metric": "cost",
                    "period": "month",
                    "limit_value": "50",
                    "applies_to": applies_to,
                },
            )

        assert (await create("aggregate")).status_code == 201
        assert (await create("each_member")).status_code == 201
        assert (await create("aggregate")).status_code == 409
        assert (await create("each_member")).status_code == 409


class TestPerMemberRefusal:
    async def test_429_says_the_members_share_ran_out(
        self,
        client: httpx.AsyncClient,
        seeded: Seeded,
        fake_upstream: FakeUpstream,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        """End to end: a spent share refuses with the member wording."""
        async with session_factory() as db:
            db.add(
                LimitRule(
                    name="member cost ceiling",
                    scope=LimitScope.GROUP,
                    scope_id=seeded.group.id,
                    metric=LimitMetric.COST,
                    window_seconds=3600,
                    limit_value=Decimal(0),
                    applies_to=LimitAppliesTo.EACH_MEMBER,
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
        assert "per member" in body["error"]["message"].lower()

    async def test_429_for_the_group_total_says_so(
        self,
        client: httpx.AsyncClient,
        seeded: Seeded,
        fake_upstream: FakeUpstream,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        """The aggregate wording is unchanged: the group's total ran out."""
        async with session_factory() as db:
            db.add(
                LimitRule(
                    name="group cost ceiling",
                    scope=LimitScope.GROUP,
                    scope_id=seeded.group.id,
                    metric=LimitMetric.COST,
                    window_seconds=3600,
                    limit_value=Decimal(0),
                )
            )
            await db.commit()
        fake_upstream.set_json(completion_body())
        response = await client.post(
            "/v1/chat/completions", json=basic_request(), headers=seeded.auth
        )
        assert response.status_code == 429
        assert "per member" not in response.json()["error"]["message"].lower()
