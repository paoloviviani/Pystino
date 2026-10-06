"""``ValkeyCounterStore.replace`` against a real server.

The in-memory store proves the semantics; only Valkey proves the commands (DEL of
the window's bucket keys and SET with a TTL inside one MULTI). Skipped unless
``TEST_VALKEY_URL`` points at a throwaway server, because the unit suite is meant
to need none:

    docker run -d --rm --name qv -p 16379:6379 valkey/valkey:8-alpine
    TEST_VALKEY_URL=redis://127.0.0.1:16379/15 uv run pytest \\
        apps/gateway/tests/test_quota_valkey_replace.py
"""

from __future__ import annotations

import os
import time
import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime

import pytest
import pytest_asyncio
from gateway.models import LimitMetric, LimitScope
from gateway.periods import PeriodKind, period_containing
from gateway.quota import CounterDelta, ScopeRef, ValkeyCounterStore, WindowQuery, WindowSpec

URL = os.environ.get("TEST_VALKEY_URL")

pytestmark = pytest.mark.skipif(not URL, reason="TEST_VALKEY_URL is not set")


@pytest_asyncio.fixture
async def store() -> AsyncIterator[ValkeyCounterStore]:
    import redis.asyncio as redis_asyncio

    client = redis_asyncio.from_url(URL, decode_responses=True)
    # A namespace of its own, so a leftover from another run cannot count.
    namespace = f"qt{uuid.uuid4().hex[:8]}"
    yield ValkeyCounterStore(client, namespace=namespace)
    async for key in client.scan_iter(f"{namespace}:*"):
        await client.delete(key)
    await client.aclose()


def delta(query: WindowQuery, units: int) -> CounterDelta:
    return CounterDelta(
        scope=query.scope,
        metric=query.metric,
        units=units,
        spec=query.spec,
        period=query.period,
        reset_epoch=query.reset_epoch,
    )


async def test_calendar_replace_sets_the_value_and_a_ttl(store: ValkeyCounterStore) -> None:
    period = period_containing(PeriodKind.MONTH, datetime.now(UTC), timezone="Europe/Rome")
    query = WindowQuery(
        scope=ScopeRef(LimitScope.USER, uuid.uuid4()), metric=LimitMetric.COST, period=period
    )
    now = time.time()
    await store.apply([delta(query, 380)], now=now)

    await store.replace([delta(query, 283)], now=now)

    assert (await store.totals([query], now=now))[0].units == 283
    (ttl,) = await store.ttls([query], now=now)
    assert ttl is not None and ttl > 0

    await store.replace([delta(query, 0)], now=now)
    assert (await store.totals([query], now=now))[0].units == 0
    assert await store.ttls([query], now=now) == [None]


async def test_rolling_replace_deletes_old_buckets_and_fills_the_newest(
    store: ValkeyCounterStore,
) -> None:
    spec = WindowSpec.for_window(3600)
    query = WindowQuery(scope=ScopeRef(LimitScope.GLOBAL), metric=LimitMetric.REQUESTS, spec=spec)
    now = time.time()
    for age in (0, 600, 1800, 3000):
        await store.apply([delta(query, 10)], now=now - age)
    assert (await store.totals([query], now=now))[0].units == 40

    await store.replace([delta(query, 25)], now=now)

    total = (await store.totals([query], now=now))[0]
    assert total.units == 25
    assert total.buckets == {spec.bucket_index(now): 25}


async def test_key_ttl_reports_absent_as_none(store: ValkeyCounterStore) -> None:
    assert await store.key_ttl("q:definitely-not-there") is None
    assert await store.claim_once("q:lock-test", ttl_seconds=30) is True
    ttl = await store.key_ttl("q:lock-test")
    assert ttl is not None and 0 < ttl <= 30
