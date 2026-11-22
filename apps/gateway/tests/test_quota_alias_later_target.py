"""A quota rule that names a later alias target must not break the failover to it.

The rules are loaded with ``populate_existing`` (so a fresh ``resets`` collection
wins) and a joined ``LimitRule.model``. That refresh used to reach the very
``ModelDef`` the alias planner had loaded for a later target, and expire its
``prices``; the next attempt then lazy-loaded them from async code and crashed
with ``MissingGreenlet``. Pinned here: attempt 1 fails upstream, and attempt 2
(whose model or provider a rule names, over or under its cap) is still served or
refused cleanly, and attempt 3 still has its prices.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any

import httpx
import pytest
from conftest import FakeUpstream, Seeded
from gateway.models import LimitMetric, LimitRule, LimitScope, ModelDef
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from test_routing_policies import Upstreams, make_route


async def _rule(
    session_factory: async_sessionmaker[AsyncSession], seeded: Seeded, limit: int, **where: Any
) -> None:
    async with session_factory() as db:
        db.add(
            LimitRule(
                name="cap",
                scope=LimitScope.GROUP,
                scope_id=seeded.group.id,
                metric=LimitMetric.REQUESTS,
                window_seconds=3600,
                limit_value=Decimal(limit),
                **where,
            )
        )
        await db.commit()


async def _id_of(session_factory: async_sessionmaker[AsyncSession], name: str) -> Any:
    async with session_factory() as db:
        return (await db.execute(select(ModelDef.id).where(ModelDef.name == name))).scalar_one()


async def _ask(client: httpx.AsyncClient, seeded: Seeded, alias: str) -> httpx.Response:
    return await client.post(
        "/v1/chat/completions",
        json={"model": alias, "messages": [{"role": "user", "content": "hi"}]},
        headers=seeded.auth,
    )


@pytest.mark.parametrize(
    ("kind", "limit", "status"),
    [
        ("model", 0, 200),  # b is over its cap: skipped, c serves
        ("model", 100, 200),  # b is under its cap: b serves
        ("provider", 100, 200),  # a provider rule (every target is on that provider)
    ],
)
async def test_a_rule_naming_a_later_target_does_not_crash_the_failover(
    client: httpx.AsyncClient,
    seeded: Seeded,
    session_factory: async_sessionmaker[AsyncSession],
    fake_upstream: FakeUpstream,
    kind: str,
    limit: int,
    status: int,
) -> None:
    await make_route(session_factory, seeded, ("a", 1), ("b", 1), ("c", 1), name="routed")
    where = (
        {"model_id": await _id_of(session_factory, "b")}
        if kind == "model"
        else {"provider_id": seeded.provider.id}
    )
    await _rule(session_factory, seeded, limit, **where)
    Upstreams(fake_upstream).set("up-a", 500)
    response = await _ask(client, seeded, "routed")
    assert response.status_code == status, response.text
