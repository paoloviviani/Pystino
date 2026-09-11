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
from gateway.periods import Period
from gateway.quota.windows import WindowSpec
from gateway.types import utcnow

# Requests, tokens and our own search calls are already integral. Cost is scaled
# by 1e9, giving nano-currency resolution: ample for per-token prices around
# 1e-9 and far below any amount anyone will ever invoice.
COST_SCALE = 10**9

# How long a calendar counter outlives its period, so a settlement arriving after the
# boundary still finds the key it reserved against.
_PERIOD_TTL_SLACK = 86_400

_METRIC_SCALE: dict[LimitMetric, int] = {
    LimitMetric.REQUESTS: 1,
    LimitMetric.TOKENS: 1,
    LimitMetric.COST: COST_SCALE,
    # A whole call or no call. Scaling it would let a fraction of a search
    # accumulate, and there is no such thing.
    LimitMetric.OWN_SEARCH_REQUESTS: 1,
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
    """What to read: one scope and metric over either kind of window.

    Exactly one of ``spec`` (rolling) and ``period`` (calendar) is set, mirroring
    the rule it came from.

    ``reset_epoch`` is the watermark from ADR 0025, carried in the *key namespace*
    rather than applied afterwards. A reset therefore renames the keys, the old
    values orphan and expire on their own TTL, and there is no deletion to race.
    """

    scope: ScopeRef
    metric: LimitMetric
    spec: WindowSpec | None = None
    period: Period | None = None
    reset_epoch: int = 0

    def __post_init__(self) -> None:
        if (self.spec is None) == (self.period is None):
            raise ValueError("a window is either rolling (spec) or calendar (period)")

    @property
    def window_id(self) -> str:
        if self.period is not None:
            return f"p{self.period.label}"
        assert self.spec is not None
        return f"g{self.spec.granularity_seconds}"


@dataclass(frozen=True, slots=True)
class WindowTotal:
    units: int
    # Per-bucket values, used to estimate Retry-After. Empty from stores that
    # aggregate without bucketing.
    buckets: dict[int, int] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class CounterDelta:
    """A change to apply to one (scope, metric) at one window.

    One logical event produces several deltas, because rules with different windows
    use different keys. A settlement reuses the delta the reservation created, which
    is how a stream that crosses a period boundary settles into the period it
    started in rather than corrupting both.
    """

    scope: ScopeRef
    metric: LimitMetric
    units: int
    spec: WindowSpec | None = None
    period: Period | None = None
    reset_epoch: int = 0

    @property
    def window_id(self) -> str:
        if self.period is not None:
            return f"p{self.period.label}"
        assert self.spec is not None
        return f"g{self.spec.granularity_seconds}"


class CounterStore(Protocol):
    async def totals(self, queries: Sequence[WindowQuery], *, now: float) -> list[WindowTotal]: ...

    async def apply(self, deltas: Sequence[CounterDelta], *, now: float) -> None: ...

    async def apply_and_total(
        self,
        deltas: Sequence[CounterDelta],
        queries: Sequence[WindowQuery],
        *,
        now: float,
    ) -> list[WindowTotal]:
        """Apply *deltas*, then report the totals for *queries*, **atomically**.

        The reason quota admission is not a race. Reading a total, deciding,
        and then incrementing leaves a window in which every concurrent caller
        reads the same pre-increment figure and they all decide yes.
        Incrementing first and reading back the result closes it: each caller
        sees a total that already contains every increment that preceded its
        own, so it can tell exactly what the window held before it arrived.

        A caller that turns out not to be admitted undoes its own delta.
        Reserving before knowing is the price of atomicity, and it is a cheap
        one — the undo is another increment, and the reservation was going to
        be corrected by ``settle`` regardless.
        """
        ...

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

    def _key(
        self,
        scope: ScopeRef,
        metric: LimitMetric,
        window_id: str,
        reset_epoch: int,
        bucket: int | None = None,
    ) -> str:
        """Build a counter key.

        Two things live in the namespace rather than being applied afterwards, and
        both make invalidation free:

        * the **granularity or period label**, so changing max_buckets_per_window
          cannot make new reads land on buckets written under the old grid, and a
          calendar period resets simply by rolling its label over;
        * the **reset watermark**, so a reset renames the keys and the old values
          orphan and expire rather than needing a delete.
        """
        suffix = f":{bucket}" if bucket is not None else ""
        return (
            f"{self._namespace}:{scope.key_part}:{metric.value}:{window_id}:r{reset_epoch}{suffix}"
        )

    def _plan(
        self, queries: Sequence[WindowQuery], *, now: float
    ) -> tuple[list[str], list[tuple[int, list[int]]]]:
        """The keys to read, and where each query's slice of them begins."""
        keys: list[str] = []
        spans: list[tuple[int, list[int]]] = []
        for query in queries:
            if query.period is not None:
                # A calendar period is a single exact counter, not a bucket grid.
                spans.append((len(keys), []))
                keys.append(
                    self._key(query.scope, query.metric, query.window_id, query.reset_epoch)
                )
                continue
            assert query.spec is not None
            indices = query.spec.indices_for(now)
            spans.append((len(keys), indices))
            keys.extend(
                self._key(query.scope, query.metric, query.window_id, query.reset_epoch, index)
                for index in indices
            )
        return keys, spans

    def _delta_key(self, delta: CounterDelta, *, now: float) -> tuple[str, int]:
        """The key a delta lands on, and how long it should outlive its window."""
        if delta.period is not None:
            key = self._key(delta.scope, delta.metric, delta.window_id, delta.reset_epoch)
            # Outlive the period so a late settle still lands, then expire on
            # their own rather than needing a sweep.
            ttl = max(60, int(delta.period.end.timestamp() - now) + _PERIOD_TTL_SLACK)
            return key, ttl
        assert delta.spec is not None
        key = self._key(
            delta.scope,
            delta.metric,
            delta.window_id,
            delta.reset_epoch,
            delta.spec.bucket_index(now),
        )
        # Refreshing the TTL on every write only ever extends a bucket's life,
        # never shortens it below what a reader needs.
        return key, delta.spec.ttl_seconds()

    def _totals_from(
        self, raw: Sequence[Any], spans: Sequence[tuple[int, list[int]]]
    ) -> list[WindowTotal]:
        results: list[WindowTotal] = []
        for start, indices in spans:
            if not indices:
                # Calendar: one key, no per-bucket detail (and none is needed —
                # Retry-After comes from the period's own end, exactly).
                value = raw[start]
                results.append(WindowTotal(units=max(0, int(value)) if value else 0))
                continue
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

    async def totals(self, queries: Sequence[WindowQuery], *, now: float) -> list[WindowTotal]:
        if not queries:
            return []
        keys, spans = self._plan(queries, now=now)
        raw = await self._client.mget(keys)  # type: ignore[attr-defined]
        return self._totals_from(raw, spans)

    async def apply_and_total(
        self,
        deltas: Sequence[CounterDelta],
        queries: Sequence[WindowQuery],
        *,
        now: float,
    ) -> list[WindowTotal]:
        """One MULTI/EXEC, so the increments and the read cannot interleave.

        ``transaction=True`` is load-bearing here. A plain pipeline is only a
        batching optimisation — another client's INCRBY can land between our
        writes and our read, which is precisely the interleaving this exists to
        prevent.
        """
        keys, spans = self._plan(queries, now=now)
        pipe = self._client.pipeline(transaction=True)  # type: ignore[attr-defined]
        for delta in deltas:
            if delta.units == 0:
                continue
            key, ttl = self._delta_key(delta, now=now)
            pipe.incrby(key, delta.units)
            pipe.expire(key, ttl)
        if keys:
            pipe.mget(keys)
        results = await pipe.execute()
        return self._totals_from(results[-1] if keys else [], spans)

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
        # No transaction: a settle or a release is a blind increment that reads
        # nothing back, so there is nothing for another client to interleave
        # with and MULTI would only cost a round trip.
        pipe = self._client.pipeline(transaction=False)  # type: ignore[attr-defined]
        for delta in deltas:
            if delta.units == 0:
                continue
            key, ttl = self._delta_key(delta, now=now)
            pipe.incrby(key, delta.units)
            pipe.expire(key, ttl)
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
    request. See ADR 0009
    """

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    async def totals(self, queries: Sequence[WindowQuery], *, now: float) -> list[WindowTotal]:
        if not queries:
            return []
        async with self._session_factory() as session:
            return [await self._one(session, query, now) for query in queries]

    async def _one(self, session: AsyncSession, query: WindowQuery, now: float) -> WindowTotal:
        if query.period is not None:
            since, until = query.period.start, query.period.end
        else:
            assert query.spec is not None
            since = datetime.fromtimestamp(now - query.spec.window_seconds, tz=UTC)
            until = None

        # The reset watermark (ADR 0025). Applying it here is what makes a reset
        # survive a counter-cache rebuild: this store is the rebuild source, so a
        # reset it ignored would be undone the moment the cache went cold.
        if query.reset_epoch:
            watermark = datetime.fromtimestamp(query.reset_epoch, tz=UTC)
            since = max(since, watermark)

        stmt = self._select_for(query, since, until)
        value = (await session.execute(stmt)).scalar_one_or_none()
        if value is None:
            return WindowTotal(units=0)
        # str() first: going through float would defeat the point of Numeric.
        return WindowTotal(units=to_units(query.metric, Decimal(str(value))))

    def _select_for(
        self, query: WindowQuery, since: datetime, until: datetime | None = None
    ) -> Select[tuple[Any]]:
        columns: dict[LimitMetric, ColumnElement[Any]] = {
            LimitMetric.REQUESTS: func.count(UsageRecord.id),
            LimitMetric.TOKENS: func.coalesce(func.sum(UsageRecord.total_tokens), 0),
            LimitMetric.COST: func.coalesce(func.sum(UsageRecord.cost), 0),
            # `own_search_requests`, never `search_count`: this metric limits the
            # searches *we* ran, and rebuilding it from the counterparty's
            # server-side searches would refill a cold cache with somebody
            # else's number — silently, and only after a Valkey restart, which
            # is the worst moment to discover a counter means something new.
            LimitMetric.OWN_SEARCH_REQUESTS: func.coalesce(
                func.sum(UsageRecord.own_search_requests), 0
            ),
        }
        column = columns[query.metric]

        stmt = select(column).where(
            UsageRecord.created_at >= since,
            # Refused requests never reach the ledger, but a row that is still
            # in progress has nothing to contribute yet.
            UsageRecord.status != UsageStatus.IN_PROGRESS,
        )
        if until is not None:
            # Half-open, so consecutive periods neither overlap nor leave a gap.
            stmt = stmt.where(UsageRecord.created_at < until)

        match query.scope.scope:
            case LimitScope.GROUP:
                stmt = stmt.where(UsageRecord.group_id == query.scope.scope_id)
            case LimitScope.USER:
                stmt = stmt.where(UsageRecord.user_id == query.scope.scope_id)
            case LimitScope.API_KEY:
                stmt = stmt.where(UsageRecord.api_key_id == query.scope.scope_id)
            case LimitScope.GLOBAL:
                pass
        return stmt

    async def apply(self, deltas: Sequence[CounterDelta], *, now: float) -> None:
        # Nothing to do: this store is derived from usage_records, which the
        # accounting path writes anyway.
        return None

    async def apply_and_total(
        self,
        deltas: Sequence[CounterDelta],
        queries: Sequence[WindowQuery],
        *,
        now: float,
    ) -> list[WindowTotal]:
        """Read the ledger, then add the caller's own deltas arithmetically.

        There is nothing here to increment — this store counts settled ledger
        rows, and an in-flight request contributes nothing until it finishes.
        But the contract is "the totals as they will be once your deltas are
        applied", and the engine subtracts its own contribution back out to
        decide. Returning the raw ledger figure would make that subtraction go
        negative and admit a request that should have been refused.

        What this cannot do is make the reservation visible to *other*
        concurrent requests, which is the wider overshoot bound already
        documented on this class. That is a property of running degraded, not
        something atomicity here could fix.
        """
        totals = await self.totals(queries, now=now)
        if not deltas:
            return totals

        added: dict[tuple[str, str, str, int], int] = defaultdict(int)
        for delta in deltas:
            added[
                (delta.scope.key_part, delta.metric.value, delta.window_id, delta.reset_epoch)
            ] += delta.units

        return [
            WindowTotal(
                units=max(
                    0,
                    total.units
                    + added.get(
                        (
                            query.scope.key_part,
                            query.metric.value,
                            query.window_id,
                            query.reset_epoch,
                        ),
                        0,
                    ),
                ),
                buckets=total.buckets,
            )
            for query, total in zip(queries, totals, strict=True)
        ]

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
            # Every field of the window travels with the delta. Dropping `period`
            # here made the rebuilt delta look rolling-with-no-spec, which failed for
            # calendar rules; dropping `reset_epoch` would have written the total
            # under the pre-reset key and quietly restored consumption a reset had
            # cleared. Both are the kind of failure that only shows up in the hour
            # after losing the cache.
            CounterDelta(
                scope=query.scope,
                metric=query.metric,
                units=total.units,
                spec=query.spec,
                period=query.period,
                reset_epoch=query.reset_epoch,
            )
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
        self._buckets: dict[tuple[str, str, str], dict[int, int]] = defaultdict(dict)
        self._claims: set[str] = set()

    async def claim_once(self, key: str, *, ttl_seconds: int) -> bool:
        if key in self._claims:
            return False
        self._claims.add(key)
        return True

    def _slot(
        self, scope: ScopeRef, metric: LimitMetric, window_id: str, reset: int
    ) -> dict[int, int]:
        return self._buckets[(scope.key_part, metric.value, f"{window_id}:r{reset}")]

    async def totals(self, queries: Sequence[WindowQuery], *, now: float) -> list[WindowTotal]:
        return self._totals(queries, now=now)

    def _totals(self, queries: Sequence[WindowQuery], *, now: float) -> list[WindowTotal]:
        results: list[WindowTotal] = []
        for query in queries:
            slot = self._slot(query.scope, query.metric, query.window_id, query.reset_epoch)
            if query.period is not None:
                results.append(WindowTotal(units=max(0, slot.get(0, 0))))
                continue
            assert query.spec is not None
            indices = query.spec.indices_for(now)
            buckets = {index: slot[index] for index in indices if index in slot}
            results.append(WindowTotal(units=max(0, sum(buckets.values())), buckets=buckets))
        return results

    async def apply(self, deltas: Sequence[CounterDelta], *, now: float) -> None:
        self._apply(deltas, now=now)

    def _apply(self, deltas: Sequence[CounterDelta], *, now: float) -> None:
        for delta in deltas:
            if delta.units == 0:
                continue
            slot = self._slot(delta.scope, delta.metric, delta.window_id, delta.reset_epoch)
            # A calendar period is one counter; slot 0 stands in for "the period".
            bucket = 0 if delta.period is not None else delta.spec.bucket_index(now)  # type: ignore[union-attr]
            slot[bucket] = slot.get(bucket, 0) + delta.units

    async def apply_and_total(
        self,
        deltas: Sequence[CounterDelta],
        queries: Sequence[WindowQuery],
        *,
        now: float,
    ) -> list[WindowTotal]:
        """Atomic by construction: neither half awaits anything.

        Written against the synchronous helpers rather than by awaiting
        ``apply`` and ``totals`` in turn. Both happen to be await-free today, so
        the loop would not interleave between them — but that is a property of
        their current bodies, not of the interface, and this method's whole
        purpose is to not depend on it.
        """
        self._apply(deltas, now=now)
        return self._totals(queries, now=now)

    def expire_before(self, cutoff: float) -> None:
        """Drop rolling buckets that ended before *cutoff*, emulating TTL expiry."""
        for (_, _, window), slot in self._buckets.items():
            if not window.startswith("g"):
                continue
            granularity = int(window.split(":")[0][1:])
            for index in list(slot):
                if (index + 1) * granularity < cutoff:
                    del slot[index]
