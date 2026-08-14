"""Counter stores behind the quota engine.

Three implementations of one protocol:

* :class:`ValkeyCounterStore` — the hot path. Bucketed integer counters.
* :class:`DatabaseCounterStore` — the fallback and the source of truth. Exact
  aggregates straight off ``usage_records``.
* :class:`InMemoryCounterStore` — tests, and a reference for the bucket semantics.

Everything is stored as **integers**, never floats. ``INCRBYFLOAT`` uses long
double arithmetic and is not exact, and a monetary quota that drifts is a
monetary quota you cannot defend in an audit. Cost is therefore scaled to
integer nano-units at the boundary and scaled back when read.
"""

from __future__ import annotations

import uuid
from collections import defaultdict
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import ROUND_CEILING, ROUND_HALF_UP, Decimal
from typing import Any, Protocol

from sqlalchemy import ColumnElement, Select, func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from gateway.models import LimitMetric, LimitScope, UsageRecord, UsageStatus
from gateway.quota.windows import WindowSpec
from gateway.types import utcnow

# Requests and tokens are already integral. Cost is scaled by 1e9, giving
# nano-currency resolution: ample for per-token prices around 1e-9 and far below
# any amount anyone will ever invoice.
COST_SCALE = 10**9

_METRIC_SCALE: dict[LimitMetric, int] = {
    LimitMetric.REQUESTS: 1,
    LimitMetric.TOKENS: 1,
    LimitMetric.COST: COST_SCALE,
}


def to_units(metric: LimitMetric, value: Decimal, *, round_up: bool = False) -> int:
    """Convert a metric value to integer storage units.

    Reservations round up so that an estimate is never accidentally generous;
    settlements round half-up so that repeated settling does not drift.
    """
    scale = _METRIC_SCALE[metric]
    scaled = value * scale
    rounding = ROUND_CEILING if round_up else ROUND_HALF_UP
    return int(scaled.to_integral_value(rounding=rounding))


def from_units(metric: LimitMetric, units: int) -> Decimal:
    scale = _METRIC_SCALE[metric]
    if scale == 1:
        return Decimal(units)
    return Decimal(units) / Decimal(scale)


@dataclass(frozen=True, slots=True)
class ScopeRef:
    """Who a counter belongs to."""

    scope: LimitScope
    scope_id: uuid.UUID | None = None

    def __post_init__(self) -> None:
        if self.scope is LimitScope.GLOBAL and self.scope_id is not None:
            raise ValueError("global scope must not carry a scope_id")
        if self.scope is not LimitScope.GLOBAL and self.scope_id is None:
            raise ValueError(f"{self.scope} scope requires a scope_id")

    @property
    def key_part(self) -> str:
        return f"{self.scope.value}:{self.scope_id or '-'}"


@dataclass(frozen=True, slots=True)
class WindowQuery:
    scope: ScopeRef
    metric: LimitMetric
    spec: WindowSpec


@dataclass(frozen=True, slots=True)
class WindowTotal:
    units: int
    # Per-bucket values, used to estimate Retry-After. Empty from stores that
    # aggregate without bucketing.
    buckets: dict[int, int] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class CounterDelta:
    """A change to apply to one (scope, metric) at one bucket grid.

    One logical event produces several deltas, because rules with different
    windows use different bucket widths and therefore different keys.
    """

    scope: ScopeRef
    metric: LimitMetric
    spec: WindowSpec
    units: int


class CounterStore(Protocol):
    async def totals(self, queries: Sequence[WindowQuery], *, now: float) -> list[WindowTotal]: ...

    async def apply(self, deltas: Sequence[CounterDelta], *, now: float) -> None: ...

    async def claim_once(self, key: str, *, ttl_seconds: int) -> bool:
        """Atomically claim *key*, returning True only for the first caller.

        Used to elect a single worker to rebuild a cold cache. Must be atomic
        across processes, or every worker rebuilds and their writes sum to N times
        the real total.
        """
        ...


class ValkeyCounterStore:
    """Bucketed counters in Valkey (or any Redis-protocol server).

    Reads are a single MGET across every bucket of every rule being evaluated, so
    checking a request against ten rules is one round trip.
    """

    def __init__(self, client: object, *, namespace: str = "q") -> None:
        # Typed as object to avoid importing redis at module import time; the
        # client only needs `pipeline` and `mget`.
        self._client = client
        self._namespace = namespace

    def _key(self, scope: ScopeRef, metric: LimitMetric, spec: WindowSpec, bucket: int) -> str:
        # Granularity is in the key: changing max_buckets_per_window must not
        # make new reads land on buckets written under the old grid.
        return (
            f"{self._namespace}:{scope.key_part}:{metric.value}"
            f":g{spec.granularity_seconds}:{bucket}"
        )

    async def totals(self, queries: Sequence[WindowQuery], *, now: float) -> list[WindowTotal]:
        if not queries:
            return []

        keys: list[str] = []
        spans: list[tuple[int, list[int]]] = []
        for query in queries:
            indices = query.spec.indices_for(now)
            spans.append((len(keys), indices))
            keys.extend(
                self._key(query.scope, query.metric, query.spec, index) for index in indices
            )

        raw = await self._client.mget(keys)  # type: ignore[attr-defined]

        results: list[WindowTotal] = []
        for start, indices in spans:
            buckets: dict[int, int] = {}
            for offset, index in enumerate(indices):
                value = raw[start + offset]
                if value is None:
                    continue
                buckets[index] = int(value)
            # Clamp at zero: a settle that lands after its bucket expired can
            # leave a stray negative, and a negative total is meaningless.
            results.append(WindowTotal(units=max(0, sum(buckets.values())), buckets=buckets))
        return results

    async def claim_once(self, key: str, *, ttl_seconds: int) -> bool:
        # SET NX is atomic across processes, which is the whole point: two workers
        # starting together must not both decide to rebuild.
        claimed = await self._client.set(  # type: ignore[attr-defined]
            key, "1", nx=True, ex=ttl_seconds
        )
        return bool(claimed)

    async def apply(self, deltas: Sequence[CounterDelta], *, now: float) -> None:
        if not deltas:
            return
        pipe = self._client.pipeline(transaction=False)  # type: ignore[attr-defined]
        for delta in deltas:
            if delta.units == 0:
                continue
            bucket = delta.spec.bucket_index(now)
            key = self._key(delta.scope, delta.metric, delta.spec, bucket)
            pipe.incrby(key, delta.units)
            # Refreshing the TTL on every write only ever extends a bucket's
            # life, never shortens it below what a reader needs.
            pipe.expire(key, delta.spec.ttl_seconds())
        await pipe.execute()


class DatabaseCounterStore:
    """Exact aggregates over the ledger.

    This is what makes Valkey disposable: if the counter cache is lost or
    unreachable, quotas still evaluate correctly, just with a database query per
    rule instead of one batched cache read.

    One documented limitation: it sees only *settled* usage. Rows still
    ``in_progress`` have zero tokens, so concurrent in-flight requests are
    invisible here in a way they are not in Valkey. Overshoot while running on
    the fallback is therefore bounded by
    ``concurrent_requests * default_max_output_tokens`` rather than by a single
    request. See docs/adr/0009-quota-model.md
    """

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    async def totals(self, queries: Sequence[WindowQuery], *, now: float) -> list[WindowTotal]:
        if not queries:
            return []
        async with self._session_factory() as session:
            return [await self._one(session, query, now) for query in queries]

    async def _one(self, session: AsyncSession, query: WindowQuery, now: float) -> WindowTotal:
        since = datetime.fromtimestamp(now - query.spec.window_seconds, tz=UTC)
        stmt = self._select_for(query, since)
        value = (await session.execute(stmt)).scalar_one_or_none()
        if value is None:
            return WindowTotal(units=0)
        # str() first: going through float would defeat the point of Numeric.
        return WindowTotal(units=to_units(query.metric, Decimal(str(value))))

    def _select_for(self, query: WindowQuery, since: datetime) -> Select[tuple[Any]]:
        columns: dict[LimitMetric, ColumnElement[Any]] = {
            LimitMetric.REQUESTS: func.count(UsageRecord.id),
            LimitMetric.TOKENS: func.coalesce(func.sum(UsageRecord.total_tokens), 0),
            LimitMetric.COST: func.coalesce(func.sum(UsageRecord.cost), 0),
        }
        column = columns[query.metric]

        stmt = select(column).where(
            UsageRecord.created_at >= since,
            # Refused requests never reach the ledger, but a row that is still
            # in progress has nothing to contribute yet.
            UsageRecord.status != UsageStatus.IN_PROGRESS,
        )

        match query.scope.scope:
            case LimitScope.GROUP:
                stmt = stmt.where(UsageRecord.group_id == query.scope.scope_id)
            case LimitScope.USER:
                stmt = stmt.where(UsageRecord.user_id == query.scope.scope_id)
            case LimitScope.GLOBAL:
                pass
        return stmt

    async def apply(self, deltas: Sequence[CounterDelta], *, now: float) -> None:
        # Nothing to do: this store is derived from usage_records, which the
        # accounting path writes anyway.
        return None

    async def claim_once(self, key: str, *, ttl_seconds: int) -> bool:
        # This store *is* the source of truth, so it is never cold and never needs
        # rebuilding into.
        return False

    async def rebuild_into(
        self,
        target: CounterStore,
        queries: Sequence[WindowQuery],
        *,
        now: float | None = None,
    ) -> None:
        """Repopulate *target* from the ledger.

        Run this after losing the counter cache. It writes each window's exact
        total into the newest bucket rather than reconstructing history bucket by
        bucket: the total is then correct now, and self-corrects as the
        artificially-placed traffic ages out of the window.
        """
        moment = now if now is not None else utcnow().timestamp()
        totals = await self.totals(queries, now=moment)
        deltas = [
            CounterDelta(scope=query.scope, metric=query.metric, spec=query.spec, units=total.units)
            for query, total in zip(queries, totals, strict=True)
            if total.units
        ]
        await target.apply(deltas, now=moment)


class InMemoryCounterStore:
    """Reference implementation with the same bucket semantics as Valkey.

    Used by the tests, and by ``GATEWAY_QUOTA__...`` single-process deployments
    where a shared counter store would be pointless. Not suitable for more than
    one worker process, since nothing is shared between them.
    """

    def __init__(self) -> None:
        self._buckets: dict[tuple[str, str, int], dict[int, int]] = defaultdict(dict)
        self._claims: set[str] = set()

    async def claim_once(self, key: str, *, ttl_seconds: int) -> bool:
        if key in self._claims:
            return False
        self._claims.add(key)
        return True

    def _slot(self, scope: ScopeRef, metric: LimitMetric, spec: WindowSpec) -> dict[int, int]:
        return self._buckets[(scope.key_part, metric.value, spec.granularity_seconds)]

    async def totals(self, queries: Sequence[WindowQuery], *, now: float) -> list[WindowTotal]:
        results: list[WindowTotal] = []
        for query in queries:
            slot = self._slot(query.scope, query.metric, query.spec)
            indices = query.spec.indices_for(now)
            buckets = {index: slot[index] for index in indices if index in slot}
            results.append(WindowTotal(units=max(0, sum(buckets.values())), buckets=buckets))
        return results

    async def apply(self, deltas: Sequence[CounterDelta], *, now: float) -> None:
        for delta in deltas:
            if delta.units == 0:
                continue
            slot = self._slot(delta.scope, delta.metric, delta.spec)
            bucket = delta.spec.bucket_index(now)
            slot[bucket] = slot.get(bucket, 0) + delta.units

    def expire_before(self, cutoff: float) -> None:
        """Drop buckets that ended before *cutoff*, emulating TTL expiry."""
        for (_, _, granularity), slot in self._buckets.items():
            for index in list(slot):
                if (index + 1) * granularity < cutoff:
                    del slot[index]
