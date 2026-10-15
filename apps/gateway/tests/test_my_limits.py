"""The quota rules a person may see about themselves.

Two properties, and the second is the reason this route exists rather than the
console filtering the admin listing: a caller sees the rules that constrain
*them*, and nothing about anybody else's budget.
"""

from __future__ import annotations

import uuid
from decimal import Decimal
from typing import Any

import httpx
import pytest
from conftest import Seeded
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
from gateway.oidc import issue_session_token
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker


def session_cookie(user_id: uuid.UUID, app: Any) -> dict[str, str]:
    token = issue_session_token(
        user_id,
        secret=app.state.settings.session_secret.get_secret_value(),
        ttl_seconds=3600,
    )
    return {"cookie": f"gw_session={token}"}


async def add_rule(
    factory: async_sessionmaker[AsyncSession],
    *,
    name: str,
    scope: LimitScope,
    scope_id: uuid.UUID | None,
    value: str = "100",
    active: bool = True,
    applies_to: LimitAppliesTo = LimitAppliesTo.AGGREGATE,
    metric: LimitMetric = LimitMetric.COST,
) -> None:
    async with factory() as db:
        db.add(
            LimitRule(
                name=name,
                scope=scope,
                scope_id=scope_id,
                metric=metric,
                window_seconds=86400,
                limit_value=Decimal(value),
                is_active=active,
                applies_to=applies_to,
            )
        )
        await db.commit()


class TestMyLimits:
    @pytest.mark.asyncio
    async def test_it_returns_rules_that_apply_to_the_caller(
        self,
        app: Any,
        client: httpx.AsyncClient,
        seeded: Seeded,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        await add_rule(session_factory, name="everyone", scope=LimitScope.GLOBAL, scope_id=None)
        await add_rule(
            session_factory, name="my group", scope=LimitScope.GROUP, scope_id=seeded.group.id
        )
        await add_rule(session_factory, name="me", scope=LimitScope.USER, scope_id=seeded.user.id)

        response = await client.get("/api/me/limits", headers=session_cookie(seeded.user.id, app))
        assert response.status_code == 200, response.text
        assert {rule["name"] for rule in response.json()["items"]} == {"everyone", "my group", "me"}

    @pytest.mark.asyncio
    async def test_it_hides_other_peoples_budgets(
        self,
        app: Any,
        client: httpx.AsyncClient,
        seeded: Seeded,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        """The reason this is a route and not a filter in the browser.

        Reusing the admin listing and hiding the rest client-side would hand a
        non-admin the shape of every budget in the organisation.
        """
        async with session_factory() as db:
            other_group = Group(name="finance")
            other_user = User(issuer="https://idp.test", subject="someone-else")
            db.add_all([other_group, other_user])
            await db.commit()
            other_group_id, other_user_id = other_group.id, other_user.id

        await add_rule(
            session_factory, name="their group", scope=LimitScope.GROUP, scope_id=other_group_id
        )
        await add_rule(session_factory, name="them", scope=LimitScope.USER, scope_id=other_user_id)

        response = await client.get("/api/me/limits", headers=session_cookie(seeded.user.id, app))
        assert [rule["name"] for rule in response.json()["items"]] == []

    @pytest.mark.asyncio
    async def test_an_inactive_rule_is_not_a_ceiling(
        self,
        app: Any,
        client: httpx.AsyncClient,
        seeded: Seeded,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        await add_rule(
            session_factory,
            name="switched off",
            scope=LimitScope.GLOBAL,
            scope_id=None,
            active=False,
        )
        response = await client.get("/api/me/limits", headers=session_cookie(seeded.user.id, app))
        assert [rule["name"] for rule in response.json()["items"]] == []

    @pytest.mark.asyncio
    async def test_a_key_rule_is_not_a_fact_about_the_person(
        self,
        app: Any,
        client: httpx.AsyncClient,
        seeded: Seeded,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        """Which key a request uses is not known until the request is made, so a
        key's ceiling beside a person's consumption would suggest a limit they
        are not necessarily near."""
        await add_rule(
            session_factory,
            name="one key",
            scope=LimitScope.API_KEY,
            scope_id=seeded.api_key.id,
        )
        response = await client.get("/api/me/limits", headers=session_cookie(seeded.user.id, app))
        assert [rule["name"] for rule in response.json()["items"]] == []

    @pytest.mark.asyncio
    async def test_it_carries_consumption_and_the_window(
        self,
        app: Any,
        client: httpx.AsyncClient,
        seeded: Seeded,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        await add_rule(
            session_factory, name="everyone", scope=LimitScope.GLOBAL, scope_id=None, value="50"
        )
        rule = (
            await client.get("/api/me/limits", headers=session_cookie(seeded.user.id, app))
        ).json()["items"][0]
        # Money is a string end to end, in this envelope as everywhere else.
        assert rule["limit_value"] == "50.000000000000"
        # A duration, not a raw second count: the label is for a person
        # reading a budget, and "86400s" is a value where they wanted a unit.
        assert rule["window_label"] == "1 day"
        assert rule["metric"] == "cost"
        # Present and zero here; absent would mean the counter store was
        # unreachable, which is a different fact and must not read as untouched.
        assert rule["current_value"] is not None

    @pytest.mark.asyncio
    async def test_it_says_nothing_about_a_scope_id(
        self,
        app: Any,
        client: httpx.AsyncClient,
        seeded: Seeded,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        """The envelope is narrower than the admin one on purpose."""
        await add_rule(
            session_factory, name="my group", scope=LimitScope.GROUP, scope_id=seeded.group.id
        )
        rule = (
            await client.get("/api/me/limits", headers=session_cookie(seeded.user.id, app))
        ).json()["items"][0]
        assert "scope_id" not in rule
        assert rule["scope"] == "group"

    @pytest.mark.asyncio
    async def test_no_session_is_401(self, client: httpx.AsyncClient, seeded: Seeded) -> None:
        assert (await client.get("/api/me/limits")).status_code == 401

    @pytest.mark.asyncio
    async def test_a_per_member_rule_is_the_callers_own_limit(
        self,
        app: Any,
        client: httpx.AsyncClient,
        seeded: Seeded,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        """A per-member rule answers for the caller, labelled as such.

        The current value is the caller's own spend billed to the group — not
        the group's total — and the additive ``applies_to`` says which kind
        each entry is, so existing consumers keep parsing.
        """
        from gateway.quota import QuotaAmounts, QuotaSubject

        await add_rule(
            session_factory,
            name="my share",
            scope=LimitScope.GROUP,
            scope_id=seeded.group.id,
            value="100",
            applies_to=LimitAppliesTo.EACH_MEMBER,
            metric=LimitMetric.REQUESTS,
        )
        await add_rule(
            session_factory,
            name="group total",
            scope=LimitScope.GROUP,
            scope_id=seeded.group.id,
            metric=LimitMetric.REQUESTS,
        )
        async with session_factory() as db:
            teammate = User(issuer="https://idp.test", subject="teammate")
            db.add(teammate)
            await db.flush()
            db.add(
                Membership(
                    user_id=teammate.id,
                    group_id=seeded.group.id,
                    source=MembershipSource.MANUAL,
                )
            )
            await db.commit()
            teammate_id = teammate.id
        # One request as the caller, two as a teammate — through the engine,
        # so the counters (which is what current_value reads) move.
        engine = app.state.quota_engine
        async with session_factory() as db:
            reservation = await engine.check_and_reserve(
                db, QuotaSubject(seeded.user.id, seeded.group.id), QuotaAmounts()
            )
            await engine.settle(reservation, QuotaAmounts())
        async with session_factory() as db:
            for _ in range(2):
                reservation = await engine.check_and_reserve(
                    db, QuotaSubject(teammate_id, seeded.group.id), QuotaAmounts()
                )
                await engine.settle(reservation, QuotaAmounts())

        rules = (
            await client.get("/api/me/limits", headers=session_cookie(seeded.user.id, app))
        ).json()["items"]
        share = next(rule for rule in rules if rule["name"] == "my share")
        total = next(rule for rule in rules if rule["name"] == "group total")
        assert share["applies_to"] == "each_member"
        assert total["applies_to"] == "aggregate"
        # 1 of my own, not the 3 the group spent together.
        assert Decimal(share["current_value"]) == Decimal(1)
        assert Decimal(total["current_value"]) == Decimal(3)
