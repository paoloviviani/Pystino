"""``GET /v1/pystino/usage`` (ADR 0074): a bearer caller's own quotas and spend.

Everything asserted here is a property `caller_usage.py` already owes
`/api/me/limits`, `/api/me/usage` and `/api/me/usage/groups` — this file exists
to pin that a bearer caller, key or token, gets the identical numbers through
the door those routes cannot answer at all (no session cookie). Ground rule 3:
quota logic gets tests of its own, and the sharpest way to catch the shared
factoring silently diverging is to assert the same fact through both doors.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any

import pytest
from conftest import Seeded, make_token
from conftest import bearer_auth as auth
from fastapi import FastAPI
from gateway.models import (
    Group,
    LimitScope,
    Membership,
    MembershipSource,
)
from gateway.quota import QuotaUnavailable
from joserfc.jwk import RSAKey
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from test_my_limits import add_rule
from test_reporting import record


class TestCredential:
    async def test_it_needs_a_credential(self, client: Any) -> None:
        response = await client.get("/v1/pystino/usage")
        assert response.status_code == 401

    async def test_a_bad_key_is_refused(self, client: Any) -> None:
        response = await client.get(
            "/v1/pystino/usage", headers={"authorization": "Bearer gwk_not-a-real-key"}
        )
        assert response.status_code == 401


class TestApiKeyCaller:
    async def test_it_reads_its_own_limits_usage_and_groups(
        self,
        client: Any,
        seeded: Seeded,
        session: AsyncSession,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        await add_rule(session_factory, name="everyone", scope=LimitScope.GLOBAL, scope_id=None)
        await add_rule(
            session_factory, name="my group", scope=LimitScope.GROUP, scope_id=seeded.group.id
        )
        await add_rule(session_factory, name="me", scope=LimitScope.USER, scope_id=seeded.user.id)
        # Excluded: which key a request used is not known until it is made
        # (routers/me.py::my_limits), so a key rule is not a fact about the
        # caller.
        await add_rule(
            session_factory, name="this key", scope=LimitScope.API_KEY, scope_id=seeded.api_key.id
        )

        await record(session, user_id=seeded.user.id, group_id=seeded.group.id, cost="4")
        await record(session, user_id=seeded.user.id, group_id=seeded.group.id, cost="2")

        response = await client.get("/v1/pystino/usage", headers=seeded.auth)
        assert response.status_code == 200, response.text
        body = response.json()

        assert {rule["name"] for rule in body["limits"]} == {"everyone", "my group", "me"}
        assert body["usage"]["requests"] == 2
        assert Decimal(body["usage"]["cost"]) == Decimal(6)
        assert set(body["groups"]) == {"research"}
        assert Decimal(body["groups"]["research"]["cost"]) == Decimal(6)
        assert body["groups"]["research"]["requests"] == 2

    async def test_a_key_rule_never_appears(
        self,
        client: Any,
        seeded: Seeded,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        await add_rule(
            session_factory, name="this key", scope=LimitScope.API_KEY, scope_id=seeded.api_key.id
        )
        response = await client.get("/v1/pystino/usage", headers=seeded.auth)
        assert response.status_code == 200, response.text
        assert response.json()["limits"] == []

    async def test_current_value_is_null_when_the_counter_store_is_unreachable(
        self,
        app: FastAPI,
        client: Any,
        seeded: Seeded,
        session_factory: async_sessionmaker[AsyncSession],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Absent, never zero — the same distinction `/api/me/limits` makes.

        A budget shown as untouched because Valkey is down is worse than one
        shown as unknown, and that is a property of `current_values` this
        route inherits by calling the same function, not by re-implementing
        the check.
        """
        await add_rule(session_factory, name="everyone", scope=LimitScope.GLOBAL, scope_id=None)

        async def broken(*args: object, **kwargs: object) -> None:
            raise QuotaUnavailable("no counter store could answer")

        monkeypatch.setattr(app.state.quota_engine, "_totals", broken)

        response = await client.get("/v1/pystino/usage", headers=seeded.auth)
        assert response.status_code == 200, response.text
        rule = response.json()["limits"][0]
        assert rule["current_value"] is None


class TestBearerCaller:
    async def test_effective_groups_not_the_tokens_claim(
        self,
        bearer_app: FastAPI,
        client: Any,
        seeded: Seeded,
        signing_key: RSAKey,
        session_factory: async_sessionmaker[AsyncSession],
        session: AsyncSession,
    ) -> None:
        """A group granted by hand shows up here too (ADR 0057).

        The token names only "research"; "finance" was granted in this
        deployment and no directory knows about it. Deriving group ids from
        the claim instead of `user.group_ids()` would silently drop it from
        both `limits` and `groups`.
        """
        finance = Group(name="finance")
        session.add(finance)
        await session.commit()
        await session.refresh(finance)
        async with session_factory() as db:
            db.add(
                Membership(
                    user_id=seeded.user.id, group_id=finance.id, source=MembershipSource.MANUAL
                )
            )
            await db.commit()

        await add_rule(
            session_factory, name="finance budget", scope=LimitScope.GROUP, scope_id=finance.id
        )
        await record(session, user_id=seeded.user.id, group_id=finance.id, cost="9")

        token = make_token(signing_key)  # claims only "research"
        response = await client.get("/v1/pystino/usage", headers=auth(token))
        assert response.status_code == 200, response.text
        body = response.json()

        assert {rule["name"] for rule in body["limits"]} == {"finance budget"}
        assert set(body["groups"]) == {"finance"}
        assert Decimal(body["groups"]["finance"]["cost"]) == Decimal(9)

    async def test_it_reads_the_callers_own_usage(
        self,
        bearer_app: FastAPI,
        client: Any,
        seeded: Seeded,
        signing_key: RSAKey,
        session: AsyncSession,
    ) -> None:
        await record(session, user_id=seeded.user.id, group_id=seeded.group.id, cost="3")

        response = await client.get("/v1/pystino/usage", headers=auth(make_token(signing_key)))
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["usage"]["requests"] == 1
        assert Decimal(body["usage"]["cost"]) == Decimal(3)
        assert body["usage"]["currency"] == "EUR"
