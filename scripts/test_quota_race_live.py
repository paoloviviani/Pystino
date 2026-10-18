#!/usr/bin/env python3
"""Quota admission under concurrency, against a real Valkey.

What only this can prove (ADR 0009):

The unit suite exercises `InMemoryCounterStore`, which is atomic for an
uninteresting reason — it never awaits, so the event loop cannot interleave
between the increment and the read. That says nothing about the store that
actually runs in production, where the same guarantee rests on `MULTI`/`EXEC`
and has to hold across separate TCP connections. A plain pipeline would pass
every test in the suite and still let another client's `INCRBY` land between
our writes and our read.

So this drives `ValkeyCounterStore` from many clients at once, over real
sockets, and asserts the two things that matter:

* exactly `ceil(limit / estimate)` callers are admitted, not "one to three";
* the refused callers leave nothing behind — admission increments *before* it
  decides, so a refusal that failed to undo itself would bury the window and
  refuse the next legitimate request too.

Usage:
    # deploy/compose.yaml does not publish Valkey on the host, so name the
    # container's address: with the stack up (the project name is
    # COMPOSE_PROJECT_NAME in deploy/.env),
    ip=$(docker inspect -f '{{range .NetworkSettings.Networks}}{{.IPAddress}}{{end}}' \\
      <project>-valkey-1)
    VALKEY_URL=redis://$ip:6379/9 uv run python scripts/test_quota_race_live.py

VALKEY_URL defaults to redis://localhost:6379/9. Database 9 is used and flushed.
"""

from __future__ import annotations

import asyncio
import os
import sys
import uuid
from decimal import Decimal

try:
    import redis.asyncio as redis
except ImportError:  # pragma: no cover - the dependency is the gateway's
    print("error: redis is not installed in this environment", file=sys.stderr)
    raise SystemExit(2) from None

from gateway.models import LimitMetric, LimitScope
from gateway.quota.counters import (
    CounterDelta,
    ScopeRef,
    ValkeyCounterStore,
    WindowQuery,
    to_units,
)
from gateway.quota.windows import WindowSpec
from gateway.types import utcnow

VALKEY_URL = os.environ.get("VALKEY_URL", "redis://localhost:6379/9")
LIMIT = 1000
EACH = 400
CALLERS = 40
EXPECTED = -(-LIMIT // EACH)  # ceil, without importing math for one division

FAILURES: list[str] = []


def expect(label: str, condition: bool, detail: str = "") -> None:
    print(f"  [{'ok  ' if condition else 'FAIL'}] {label}" + (f" — {detail}" if detail else ""))
    if not condition:
        FAILURES.append(label)


async def main() -> int:
    spec = WindowSpec(window_seconds=3600, granularity_seconds=60)
    scope = ScopeRef(LimitScope.GROUP, uuid.uuid4())
    query = WindowQuery(scope=scope, metric=LimitMetric.TOKENS, spec=spec)
    now = utcnow().timestamp()
    units = to_units(LimitMetric.TOKENS, Decimal(EACH), round_up=True)

    def delta(amount: int) -> CounterDelta:
        return CounterDelta(scope=scope, metric=LimitMetric.TOKENS, units=amount, spec=spec)

    # The per-member half (ADR 0098): a second group whose budget belongs to
    # each member separately. Forty callers, but only a quarter of them are
    # the same member — so the member's cap governs, not the group's, and the
    # arithmetic number admitted is per member, not per group.
    member = uuid.uuid4()
    member_scope = ScopeRef(LimitScope.GROUP, uuid.uuid4(), member_id=member)
    member_query = WindowQuery(scope=member_scope, metric=LimitMetric.TOKENS, spec=spec)

    def member_delta(amount: int) -> CounterDelta:
        return CounterDelta(scope=member_scope, metric=LimitMetric.TOKENS, units=amount, spec=spec)

    # One client per caller. Sharing a connection would serialise them inside
    # the client library and prove nothing about the server.
    clients = [redis.from_url(VALKEY_URL) for _ in range(CALLERS)]
    stores = [ValkeyCounterStore(client, namespace="racecheck") for client in clients]

    try:
        try:
            await clients[0].ping()
        except Exception as exc:
            print(f"error: no Valkey at {VALKEY_URL}: {exc}", file=sys.stderr)
            return 2
        await clients[0].flushdb()

        async def attempt(store: ValkeyCounterStore) -> bool:
            # The engine's admission, reduced to the part that races.
            totals = await store.apply_and_total([delta(units)], [query], now=now)
            before = max(0, totals[0].units - units)
            if before >= LIMIT:
                await store.apply([delta(-units)], now=now)
                return False
            return True

        print(f"=== {CALLERS} concurrent callers, {EACH} each, ceiling {LIMIT} ===")
        admitted = sum(await asyncio.gather(*(attempt(store) for store in stores)))
        held = (await stores[0].totals([query], now=now))[0].units

        expect(
            "exactly the arithmetic number are admitted",
            admitted == EXPECTED,
            f"{admitted} of {CALLERS}, expected {EXPECTED}",
        )
        expect(
            "and the refused leave nothing on the counter",
            held == EXPECTED * EACH,
            f"counter holds {held}, expected {EXPECTED * EACH}",
        )
        await clients[0].flushdb()

        # The same race, per member (ADR 0098): every caller is the same
        # member, so the per-member key races exactly like a scope key did.
        async def member_attempt(store: ValkeyCounterStore) -> bool:
            totals = await store.apply_and_total([member_delta(units)], [member_query], now=now)
            before = max(0, totals[0].units - units)
            if before >= LIMIT:
                await store.apply([member_delta(-units)], now=now)
                return False
            return True

        print(f"=== {CALLERS} concurrent callers, one member, ceiling {LIMIT} ===")
        member_admitted = sum(await asyncio.gather(*(member_attempt(store) for store in stores)))
        member_held = (await stores[0].totals([member_query], now=now))[0].units
        expect(
            "the member's own budget admits exactly the arithmetic number",
            member_admitted == EXPECTED,
            f"{member_admitted} of {CALLERS}, expected {EXPECTED}",
        )
        expect(
            "and the refused leave nothing on the member's counter",
            member_held == EXPECTED * EACH,
            f"counter holds {member_held}, expected {EXPECTED * EACH}",
        )
        await clients[0].flushdb()
    finally:
        for client in clients:
            await client.aclose()

    print()
    if FAILURES:
        print(f"FAILED: {len(FAILURES)}")
        for name in FAILURES:
            print(f"  - {name}")
        return 1
    print("all checks passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
