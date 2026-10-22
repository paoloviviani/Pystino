"""An each-user rule's admission against a real Valkey.

The in-memory store is atomic only because it never awaits, so it proves
nothing about the MULTI/EXEC that makes reserve-then-judge race-free. This
drives a global ``each_user`` rule through ``ValkeyCounterStore``: twenty
concurrent requests from one person against a ceiling of three admit exactly
three, a second person is unaffected, and a refund gives the budget back.
Skipped unless ``TEST_VALKEY_URL`` points at a throwaway server (see
``test_quota_valkey_replace.py``).
"""

from __future__ import annotations

import asyncio
import os
import uuid
from collections.abc import AsyncIterator

import pytest
import pytest_asyncio
from gateway.quota import QuotaAmounts, QuotaExceeded, QuotaSubject, ValkeyCounterStore
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from test_quota_engine import engine_for
from test_quota_per_user import add_user_rule, make_user

URL = os.environ.get("TEST_VALKEY_URL")

pytestmark = pytest.mark.skipif(not URL, reason="TEST_VALKEY_URL is not set")


@pytest_asyncio.fixture
async def store() -> AsyncIterator[ValkeyCounterStore]:
    import redis.asyncio as redis_asyncio

    client = redis_asyncio.from_url(URL, decode_responses=True)
    namespace = f"qt{uuid.uuid4().hex[:8]}"
    yield ValkeyCounterStore(client, namespace=namespace)
    async for key in client.scan_iter(f"{namespace}:*"):
        await client.delete(key)
    await client.aclose()


async def test_concurrent_admission_is_exact_per_user(
    session: AsyncSession,
    session_factory: async_sessionmaker[AsyncSession],
    store: ValkeyCounterStore,
) -> None:
    first = await make_user(session)
    second = await make_user(session)
    await add_user_rule(session, limit="3")
    quota = engine_for(store)

    async def attempt(user_id: uuid.UUID) -> bool:
        async with session_factory() as own_session:
            try:
                await quota.check_and_reserve(
                    own_session, QuotaSubject(user_id, None), QuotaAmounts()
                )
                return True
            except QuotaExceeded:
                return False

    admitted = sum(await asyncio.gather(*(attempt(first.id) for _ in range(20))))
    assert admitted == 3, f"admitted {admitted} of 20"
    # The refused attempts undid their reservations: the second person still
    # has their whole budget.
    assert sum(await asyncio.gather(*(attempt(second.id) for _ in range(5)))) == 3


async def test_a_release_gives_the_budget_back(
    session: AsyncSession, store: ValkeyCounterStore
) -> None:
    user = await make_user(session)
    await add_user_rule(session, limit="1")
    quota = engine_for(store)
    subject = QuotaSubject(user.id, None)

    reservation = await quota.check_and_reserve(session, subject, QuotaAmounts())
    with pytest.raises(QuotaExceeded):
        await quota.check_and_reserve(session, subject, QuotaAmounts())
    await quota.release(reservation)
    assert not (await quota.check_and_reserve(session, subject, QuotaAmounts())).is_empty
