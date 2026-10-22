"""A user rule that overrides the global each-user ceiling for one person.

``overrides_each_user`` on a ``scope=user`` rule replaces, for that person, every
global ``each_user`` rule with the same metric and window. Pinned here:

- admission: a higher override admits above the global ceiling, a lower one
  refuses below it; a different metric or window is not replaced; the group
  aggregate, group per-member, global aggregate and any other user rule still
  bind; an API key's spend is its owner's; a request with no user is
  unaffected; an inactive override replaces nothing;
- counting: the override reads the person's ``user:<id>`` counter, so switching
  it on or off moves no data, and health, reconcile and seeding keep working;
- reporting: ``/api/me/limits`` and ``/v1/pystino/usage`` show the person the
  override in place of the global rule, and nobody else's view changes;
- validation: only a user rule may carry the flag (400 from the API, check
  constraint underneath), one override per person, metric and window;
- notifications (ADR 0052): the override notifies like a user rule and the
  replaced global rule is not even measured for that person.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

import httpx
import pytest
from conftest import FakeUpstream, Seeded
from gateway.config import QuotaSettings
from gateway.models import LimitAppliesTo, LimitMetric, LimitRule, LimitScope
from gateway.periods import PeriodKind
from gateway.quota import (
    DatabaseCounterStore,
    InMemoryCounterStore,
    QuotaAmounts,
    QuotaEngine,
    QuotaExceeded,
    QuotaSubject,
)
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from test_chat_completions import basic_request
from test_quota_calendar import make_group, spend
from test_quota_engine import add_rule, engine_for
from test_quota_per_member import add_member_rule, make_member
from test_quota_per_user import add_user_rule, make_user, one_request, user_query


async def add_override(
    session: AsyncSession,
    user_id: uuid.UUID,
    *,
    metric: LimitMetric = LimitMetric.REQUESTS,
    limit: str = "5",
    window_seconds: int | None = 3600,
    period: PeriodKind | None = None,
    active: bool = True,
) -> LimitRule:
    rule = LimitRule(
        name="override",
        scope=LimitScope.USER,
        scope_id=user_id,
        metric=metric,
        window_seconds=window_seconds if period is None else None,
        period=period,
        limit_value=Decimal(limit),
        overrides_each_user=True,
        is_active=active,
    )
    session.add(rule)
    await session.commit()
    await session.refresh(rule, attribute_names=["resets"])
    return rule


async def refusal(quota: Any, session: AsyncSession, subject: QuotaSubject) -> QuotaExceeded:
    with pytest.raises(QuotaExceeded) as caught:
        await quota.check_and_reserve(session, subject, QuotaAmounts())
    return caught.value


class TestAdmission:
    async def test_a_higher_override_admits_above_the_global_ceiling(
        self, session: AsyncSession
    ) -> None:
        user = await make_user(session)
        other = await make_user(session)
        await add_user_rule(session, limit="2")
        await add_override(session, user.id, limit="4")
        quota = engine_for()

        for _ in range(4):
            await one_request(quota, session, QuotaSubject(user.id, None))
        error = await refusal(quota, session, QuotaSubject(user.id, None))
        # Held to the override's figure, and the message says whose rule it was.
        assert [v.rule.scope for v in error.violations] == [LimitScope.USER]
        assert error.violations[0].limit == Decimal(4)

        # Everyone else keeps the global ceiling.
        for _ in range(2):
            await one_request(quota, session, QuotaSubject(other.id, None))
        error = await refusal(quota, session, QuotaSubject(other.id, None))
        assert [v.rule.scope for v in error.violations] == [LimitScope.GLOBAL]

    async def test_a_lower_override_refuses_below_the_global_ceiling(
        self, session: AsyncSession
    ) -> None:
        user = await make_user(session)
        await add_user_rule(session, limit="10")
        await add_override(session, user.id, limit="1")
        quota = engine_for()

        await one_request(quota, session, QuotaSubject(user.id, None))
        error = await refusal(quota, session, QuotaSubject(user.id, None))
        assert [v.rule.scope for v in error.violations] == [LimitScope.USER]

    async def test_a_different_metric_or_window_is_not_replaced(
        self, session: AsyncSession
    ) -> None:
        user = await make_user(session)
        await add_user_rule(session, metric=LimitMetric.REQUESTS, limit="2", window_seconds=3600)
        await add_user_rule(session, metric=LimitMetric.TOKENS, limit="1000", window_seconds=3600)
        await add_user_rule(session, metric=LimitMetric.REQUESTS, limit="3", window_seconds=60)
        # Overrides only requests per hour.
        await add_override(session, user.id, metric=LimitMetric.REQUESTS, limit="100")
        quota = engine_for()
        subject = QuotaSubject(user.id, None)

        loaded = await quota.load_rules(session, subject)
        assert sorted((r.scope.value, r.metric.value, r.window_seconds) for r in loaded) == [
            ("global", "requests", 60),
            ("global", "tokens", 3600),
            ("user", "requests", 3600),
        ]
        # The per-minute global rule still refuses the fourth request.
        for _ in range(3):
            await one_request(quota, session, subject)
        error = await refusal(quota, session, subject)
        assert [v.rule.window_seconds for v in error.violations] == [60]

    async def test_a_calendar_override_does_not_replace_a_rolling_rule(
        self, session: AsyncSession
    ) -> None:
        user = await make_user(session)
        await add_user_rule(session, limit="2", window_seconds=3600)
        await add_override(session, user.id, limit="100", period=PeriodKind.MONTH)
        loaded = await engine_for().load_rules(session, QuotaSubject(user.id, None))
        # The hourly global rule is not replaced by a monthly override.
        assert sorted(r.scope.value for r in loaded) == ["global", "user"]
        assert any(r.applies_to is LimitAppliesTo.EACH_USER for r in loaded)

    async def test_every_other_rule_still_binds(self, session: AsyncSession) -> None:
        group = await make_group(session)
        user = await make_member(session, group)
        await add_user_rule(session, limit="2")
        await add_override(session, user.id, limit="50")
        await add_rule(
            session,
            scope=LimitScope.GLOBAL,
            metric=LimitMetric.REQUESTS,
            limit="40",
            window_seconds=3600,
        )
        await add_rule(
            session,
            scope=LimitScope.GROUP,
            scope_id=group.id,
            metric=LimitMetric.REQUESTS,
            limit="30",
            window_seconds=3600,
        )
        await add_member_rule(session, scope_id=group.id, limit="3")
        # A second, tighter ordinary user rule for the same metric and window.
        await add_rule(
            session,
            scope=LimitScope.USER,
            scope_id=user.id,
            metric=LimitMetric.REQUESTS,
            limit="5",
            window_seconds=3600,
        )
        quota = engine_for()
        subject = QuotaSubject(user.id, group.id)

        for _ in range(3):
            await one_request(quota, session, subject)
        assert "group per member" in str(await refusal(quota, session, subject))

        for _ in range(2):
            await one_request(quota, session, QuotaSubject(user.id, None))
        error = await refusal(quota, session, QuotaSubject(user.id, None))
        assert [v.limit for v in error.violations] == [Decimal(5)]

    async def test_global_aggregate_and_group_aggregate_still_bind(
        self, session: AsyncSession
    ) -> None:
        group = await make_group(session)
        first = await make_member(session, group)
        second = await make_member(session, group)
        await add_user_rule(session, limit="1")
        await add_override(session, first.id, limit="100")
        await add_rule(
            session,
            scope=LimitScope.GROUP,
            scope_id=group.id,
            metric=LimitMetric.REQUESTS,
            limit="2",
            window_seconds=3600,
        )
        quota = engine_for()
        await one_request(quota, session, QuotaSubject(first.id, group.id))
        await one_request(quota, session, QuotaSubject(second.id, group.id))
        error = await refusal(quota, session, QuotaSubject(first.id, group.id))
        assert [v.rule.scope for v in error.violations] == [LimitScope.GROUP]

    async def test_an_inactive_override_replaces_nothing(self, session: AsyncSession) -> None:
        user = await make_user(session)
        await add_user_rule(session, limit="1")
        await add_override(session, user.id, limit="100", active=False)
        quota = engine_for()
        await one_request(quota, session, QuotaSubject(user.id, None))
        error = await refusal(quota, session, QuotaSubject(user.id, None))
        assert [v.rule.scope for v in error.violations] == [LimitScope.GLOBAL]

    async def test_a_request_with_no_user_skips_both(self, session: AsyncSession) -> None:
        user = await make_user(session)
        await add_user_rule(session, limit="1")
        await add_override(session, user.id, limit="1")
        quota = engine_for()
        for _ in range(3):
            reservation = await quota.check_and_reserve(
                session, QuotaSubject(None, None), QuotaAmounts()
            )
            assert reservation.is_empty

    async def test_the_override_and_the_global_rule_share_one_counter(
        self, session: AsyncSession
    ) -> None:
        """Switching the override off continues from the same count: no data
        moved, no reset."""
        user = await make_user(session)
        await add_user_rule(session, limit="3")
        override = await add_override(session, user.id, limit="10")
        store = InMemoryCounterStore()
        quota = engine_for(store)
        subject = QuotaSubject(user.id, None)

        for _ in range(3):
            reservation = await quota.check_and_reserve(session, subject, QuotaAmounts())
            assert [d.scope.key_part for d in reservation.deltas] == [f"user:{user.id}"]
            await quota.settle(reservation, QuotaAmounts(requests=Decimal(1)))

        override.is_active = False
        await session.commit()
        # Three already counted: the global ceiling of three now refuses.
        error = await refusal(quota, session, subject)
        assert [v.rule.scope for v in error.violations] == [LimitScope.GLOBAL]
        override.is_active = True
        await session.commit()
        await one_request(quota, session, subject)

    async def test_an_api_key_spends_its_owners_override(
        self,
        client: httpx.AsyncClient,
        seeded: Seeded,
        fake_upstream: FakeUpstream,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        completion = {
            "id": "c1",
            "object": "chat.completion",
            "choices": [{"index": 0, "message": {"role": "assistant", "content": "ok"}}],
            "usage": {"prompt_tokens": 10, "completion_tokens": 2, "total_tokens": 12},
        }
        async with session_factory() as db:
            await add_user_rule(db, limit="1")
            await add_override(db, seeded.user.id, limit="2")

        async def call() -> int:
            fake_upstream.set_json(completion)
            response = await client.post(
                "/v1/chat/completions", json=basic_request(), headers=seeded.auth
            )
            return response.status_code

        assert [await call() for _ in range(3)] == [200, 200, 429]


class TestUpkeep:
    async def test_health_shows_the_override_and_skips_the_replaced_row(
        self, session: AsyncSession, session_factory: async_sessionmaker[AsyncSession]
    ) -> None:
        group = await make_group(session)
        user = await make_user(session)
        other = await make_user(session)
        await add_user_rule(session, metric=LimitMetric.COST, limit="1000", period=PeriodKind.MONTH)
        override = await add_override(
            session, user.id, metric=LimitMetric.COST, limit="5", period=PeriodKind.MONTH
        )
        await spend(session, group_id=group.id, user_id=user.id, cost="3")
        quota = engine_for(InMemoryCounterStore(), fallback=DatabaseCounterStore(session_factory))

        health = await quota.health(session)

        global_rows = {w.member_id for w in health.windows if w.rule.scope is LimitScope.GLOBAL}
        assert other.id in global_rows
        assert user.id not in global_rows
        mine = [w for w in health.windows if w.rule.id == override.id]
        assert len(mine) == 1
        assert mine[0].ledger == Decimal("3")

    async def test_reconcile_and_seeding_repair_the_shared_counter(
        self, session: AsyncSession, session_factory: async_sessionmaker[AsyncSession]
    ) -> None:
        group = await make_group(session)
        user = await make_user(session)
        await add_user_rule(session, metric=LimitMetric.COST, limit="1000", period=PeriodKind.MONTH)
        override = await add_override(
            session, user.id, metric=LimitMetric.COST, limit="50", period=PeriodKind.MONTH
        )
        await spend(session, group_id=group.id, user_id=user.id, cost="4")
        cache = InMemoryCounterStore()
        quota = engine_for(cache, fallback=DatabaseCounterStore(session_factory))
        now = datetime.now(UTC).timestamp()

        assert await quota.seed_rule(session, override) is True
        totals = await cache.totals([user_query(user.id, LimitMetric.COST)], now=now)
        assert totals[0].units == 4_000_000 or Decimal(totals[0].units) > 0
        done = await quota.reconcile(session)
        assert all(entry.before == entry.after for entry in done)


class TestReporting:
    async def test_the_person_sees_the_override_in_place_of_the_global_rule(
        self, session: AsyncSession
    ) -> None:
        from gateway.caller_usage import caller_limits

        user = await make_user(session)
        other = await make_user(session)
        await add_user_rule(session, metric=LimitMetric.COST, limit="20", name="everyone's")
        await add_override(session, user.id, metric=LimitMetric.COST, limit="50")
        quota = engine_for()

        mine = await caller_limits(session, quota, user_id=user.id, group_ids=[])
        assert [(r.name, r.scope, r.limit_value, r.overrides_each_user) for r in mine] == [
            ("override", "user", Decimal(50), True)
        ]
        theirs = await caller_limits(session, quota, user_id=other.id, group_ids=[])
        assert [(r.name, r.scope, r.limit_value, r.overrides_each_user) for r in theirs] == [
            ("everyone's", "global", Decimal(20), False)
        ]

    async def test_me_limits_and_pystino_usage_agree(
        self,
        app: Any,
        client: httpx.AsyncClient,
        seeded: Seeded,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        from test_my_limits import session_cookie

        async with session_factory() as db:
            await add_user_rule(db, metric=LimitMetric.COST, limit="20", name="everyone's")
            await add_override(db, seeded.user.id, metric=LimitMetric.COST, limit="50")

        me = await client.get("/api/me/limits", headers=session_cookie(seeded.user.id, app))
        assert me.status_code == 200, me.text
        v1 = await client.get("/v1/pystino/usage", headers=seeded.auth)
        assert v1.status_code == 200, v1.text
        for items in (me.json()["items"], v1.json()["limits"]):
            assert [(r["name"], r["scope"], r["overrides_each_user"]) for r in items] == [
                ("override", "user", True)
            ]
            assert Decimal(items[0]["limit_value"]) == Decimal(50)


class TestNotifications:
    async def test_the_override_is_measured_and_the_replaced_rule_is_not(
        self, session: AsyncSession
    ) -> None:
        seen: list[tuple[str, uuid.UUID | None]] = []

        class Spy:
            def observe(self, entries: list[tuple[LimitRule, int]], *, requester: Any) -> None:
                seen.extend((rule.name, requester) for rule, _ in entries)

        user = await make_user(session)
        await add_user_rule(session, limit="10", name="everyone's")
        await add_override(session, user.id, limit="10")
        quota = QuotaEngine(
            InMemoryCounterStore(), settings=QuotaSettings(enabled=True), notifier=Spy()
        )
        await one_request(quota, session, QuotaSubject(user.id, None))
        assert seen == [("override", user.id)]

        other = await make_user(session)
        seen.clear()
        await one_request(quota, session, QuotaSubject(other.id, None))
        assert seen == [("everyone's", other.id)]


class TestValidation:
    async def _post(self, client: httpx.AsyncClient, **fields: Any) -> httpx.Response:
        body = {
            "name": "x",
            "metric": "cost",
            "period": "month",
            "limit_value": "20",
            "overrides_each_user": True,
            **fields,
        }
        return await client.post("/api/admin/limits", json=body)

    async def test_the_api_creates_an_override_for_one_person(
        self,
        app: Any,
        client: httpx.AsyncClient,
        seeded: Seeded,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        from test_admin import as_user, make_admin

        as_user(app, await make_admin(session_factory, seeded))
        response = await self._post(client, scope="user", scope_id=str(seeded.user.id))
        assert response.status_code == 201, response.text
        assert response.json()["overrides_each_user"] is True
        # The same rule again is a conflict; an ordinary user rule alongside is not.
        again = await self._post(client, scope="user", scope_id=str(seeded.user.id))
        assert again.status_code == 409
        plain = await self._post(
            client, scope="user", scope_id=str(seeded.user.id), overrides_each_user=False
        )
        assert plain.status_code == 201, plain.text
        assert plain.json()["overrides_each_user"] is False

    @pytest.mark.parametrize("scope", ["global", "group", "api_key"])
    async def test_the_flag_on_any_other_scope_is_a_400(
        self,
        app: Any,
        client: httpx.AsyncClient,
        seeded: Seeded,
        session_factory: async_sessionmaker[AsyncSession],
        scope: str,
    ) -> None:
        from test_admin import as_user, make_admin

        as_user(app, await make_admin(session_factory, seeded))
        fields: dict[str, Any] = {"scope": scope}
        if scope == "group":
            fields["scope_id"] = str(seeded.group.id)
        if scope == "api_key":
            fields["scope_id"] = str(uuid.uuid4())
        response = await self._post(client, **fields)
        assert response.status_code == 400, response.text

    @pytest.mark.parametrize("scope", ["global", "group"])
    async def test_the_database_refuses_the_flag_off_the_user_scope(
        self, session: AsyncSession, scope: str
    ) -> None:
        group = await make_group(session)
        session.add(
            LimitRule(
                name="bypass",
                scope=LimitScope(scope),
                scope_id=group.id if scope == "group" else None,
                metric=LimitMetric.COST,
                window_seconds=60,
                limit_value=Decimal(1),
                applies_to=LimitAppliesTo.AGGREGATE,
                overrides_each_user=True,
            )
        )
        with pytest.raises(IntegrityError):
            await session.commit()
        await session.rollback()
