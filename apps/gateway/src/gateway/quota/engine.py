"""Quota rule evaluation, reserve-then-settle.

Ordering, which the brief is explicit about and which matters: **quota checks run
before the upstream call, accounting after it.** In between sits a reservation,
and the reservation is the part that is easy to leave out.

Without it, ten requests arriving in the same millisecond each read the same
under-limit total and each decide they are allowed, so a group with 100 tokens
left spends 1000. The reservation makes each request's *worst case* immediately
visible to its siblings, and the settle step replaces the estimate with the truth
once the response is complete.

Overrun policy (ADR 0009): a request is refused when the window total has already
reached the limit. A request that is admitted always runs to completion — we never
abort a stream mid-generation, because the tokens are already spent upstream and
truncating destroys the answer as well as the money. Overshoot is therefore
bounded by one request's actual usage, and the *next* request is refused.
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import Sequence
from dataclasses import dataclass, field
from decimal import Decimal

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from gateway.config import QuotaSettings
from gateway.models import LimitMetric, LimitRule, LimitScope
from gateway.quota.counters import (
    CounterDelta,
    CounterStore,
    ScopeRef,
    WindowQuery,
    WindowTotal,
    from_units,
    to_units,
)
from gateway.quota.windows import WindowSpec
from gateway.types import utcnow

logger = logging.getLogger(__name__)

# How often a *recurring* counter-store failure may log a full traceback. See
# QuotaEngine._verbose.
_TRACEBACK_INTERVAL_SECONDS = 60.0

# Presence of this key in the counter cache means "the cache still holds our
# counters". It is lost exactly when the counters are. See
# QuotaEngine.rebuild_if_cache_is_cold.
_REBUILD_MARKER_KEY = "q:seeded"


@dataclass(frozen=True, slots=True)
class QuotaSubject:
    """Who is being charged, and therefore which rules apply."""

    user_id: uuid.UUID | None
    group_id: uuid.UUID | None

    def scope_refs(self) -> list[ScopeRef]:
        refs = [ScopeRef(LimitScope.GLOBAL)]
        if self.group_id is not None:
            refs.append(ScopeRef(LimitScope.GROUP, self.group_id))
        if self.user_id is not None:
            refs.append(ScopeRef(LimitScope.USER, self.user_id))
        return refs


@dataclass(frozen=True, slots=True)
class QuotaAmounts:
    """A bundle of metric values: an estimate before, the truth after."""

    requests: Decimal = Decimal(1)
    tokens: Decimal = Decimal(0)
    cost: Decimal = Decimal(0)

    def get(self, metric: LimitMetric) -> Decimal:
        match metric:
            case LimitMetric.REQUESTS:
                return self.requests
            case LimitMetric.TOKENS:
                return self.tokens
            case LimitMetric.COST:
                return self.cost


@dataclass(frozen=True, slots=True)
class Violation:
    rule: LimitRule
    current: Decimal
    limit: Decimal
    retry_after_seconds: int

    def describe(self) -> str:
        scope = self.rule.scope.value
        return (
            f"{scope} limit of {self.limit} {self.rule.metric.value}"
            f" per {self.rule.window_seconds}s exceeded (current {self.current})"
        )


class QuotaExceeded(Exception):
    """Raised instead of calling the upstream. Surfaces as HTTP 429."""

    def __init__(self, violations: Sequence[Violation]) -> None:
        self.violations = list(violations)
        super().__init__("; ".join(v.describe() for v in self.violations))

    @property
    def retry_after_seconds(self) -> int:
        return max((v.retry_after_seconds for v in self.violations), default=1)


class QuotaUnavailable(Exception):
    """Neither counter store could answer. Surfaces as HTTP 503."""


@dataclass
class Reservation:
    """Provisional counter writes, to be replaced by actuals.

    Always settle or release a reservation, including on the error paths: an
    abandoned reservation keeps consuming a user's budget until its buckets age
    out of the window.
    """

    deltas: list[CounterDelta] = field(default_factory=list)
    reserved: dict[LimitMetric, int] = field(default_factory=dict)
    created_at: float = 0.0
    settled: bool = False

    @property
    def is_empty(self) -> bool:
        return not self.deltas


class QuotaEngine:
    def __init__(
        self,
        store: CounterStore,
        *,
        settings: QuotaSettings,
        fallback: CounterStore | None = None,
    ) -> None:
        self._store = store
        self._fallback = fallback
        self._settings = settings
        # When each distinct failure last logged a traceback.
        self._logged_at: dict[str, float] = {}

    def _spec(self, window_seconds: int) -> WindowSpec:
        return WindowSpec.for_window(
            window_seconds,
            min_bucket_seconds=self._settings.min_bucket_seconds,
            max_buckets_per_window=self._settings.max_buckets_per_window,
        )

    async def load_rules(self, session: AsyncSession, subject: QuotaSubject) -> list[LimitRule]:
        """Active rules applying to *subject*.

        One indexed query per request. If this ever shows up in a profile it is
        safely cacheable with a short TTL — rules change on human timescales —
        but correctness first.
        """
        conditions = [LimitRule.scope == LimitScope.GLOBAL]
        if subject.group_id is not None:
            conditions.append(
                (LimitRule.scope == LimitScope.GROUP) & (LimitRule.scope_id == subject.group_id)
            )
        if subject.user_id is not None:
            conditions.append(
                (LimitRule.scope == LimitScope.USER) & (LimitRule.scope_id == subject.user_id)
            )

        stmt = select(LimitRule).where(LimitRule.is_active.is_(True), or_(*conditions))
        return list((await session.execute(stmt)).scalars().all())

    async def rebuild_if_cache_is_cold(
        self, session: AsyncSession, *, now: float | None = None
    ) -> bool:
        """Repopulate the counter cache from the ledger if it looks freshly empty.

        Why this exists, found by restarting Valkey under a running stack: the
        counter cache is disposable for *reads* — a failed read falls back to the
        database — but an **empty** cache is not a failed read. It answers
        confidently with zero. So after Valkey is wiped or restarted without its
        snapshot, every group silently receives a fresh budget, and the gateway
        cheerfully serves a group that is already over its limit. Observed: the
        ledger said €11.70 spent against a €10 ceiling while Valkey said €0.90.

        The marker key is the trick. Its presence *in the cache* is a proxy for "the
        cache still holds our data", because the two are lost together. So:

        * fresh or wiped cache → marker absent → exactly one worker rebuilds;
        * gateway restarted, cache intact → marker present → no rebuild, which is
          correct because the counters are already right;
        * buckets aged out naturally while the marker lived → no rebuild, also
          correct, because that traffic is genuinely outside every window now.

        ``claim_once`` is atomic, so N workers starting together produce one rebuild
        rather than N additive ones.

        Returns True if this process performed the rebuild.
        """
        if not self._settings.enabled or self._fallback is None:
            return False

        moment = now if now is not None else utcnow().timestamp()

        rules = list(
            (await session.execute(select(LimitRule).where(LimitRule.is_active.is_(True))))
            .scalars()
            .all()
        )
        if not rules:
            return False

        # Generously longer than any window, so a rebuild happens only when the
        # cache genuinely lost its contents rather than on every window rollover.
        marker_ttl = max(rule.window_seconds for rule in rules) * 4

        try:
            claimed = await self._store.claim_once(_REBUILD_MARKER_KEY, ttl_seconds=marker_ttl)
        except Exception:
            logger.warning(
                "could not check whether the counter cache is cold; skipping rebuild",
                exc_info=True,
            )
            return False

        if not claimed:
            logger.debug("counter cache already warm; no rebuild needed")
            return False

        queries = [
            WindowQuery(
                scope=ScopeRef(rule.scope, rule.scope_id),
                metric=rule.metric,
                spec=self._spec(rule.window_seconds),
            )
            for rule in rules
        ]

        try:
            await self._fallback.rebuild_into(self._store, queries, now=moment)  # type: ignore[attr-defined]
        except Exception:
            logger.error("counter cache rebuild failed", exc_info=True)
            return False

        logger.warning(
            "counter cache was cold and has been rebuilt from the ledger "
            "(%d rule scope(s)); quotas would otherwise have started from zero",
            len(queries),
        )
        return True

    async def check_and_reserve(
        self,
        session: AsyncSession,
        subject: QuotaSubject,
        estimate: QuotaAmounts,
        *,
        now: float | None = None,
    ) -> Reservation:
        """Evaluate every applicable rule and reserve the estimate.

        Raises:
            QuotaExceeded: at least one rule is already at or over its limit.
            QuotaUnavailable: no counter store could answer and fail_open is off.
        """
        moment = now if now is not None else utcnow().timestamp()

        if not self._settings.enabled:
            return Reservation(created_at=moment)

        rules = await self.load_rules(session, subject)
        if not rules:
            return Reservation(created_at=moment)

        scoped: list[tuple[LimitRule, ScopeRef, WindowSpec]] = []
        for rule in rules:
            ref = ScopeRef(rule.scope, rule.scope_id)
            scoped.append((rule, ref, self._spec(rule.window_seconds)))

        queries = [
            WindowQuery(scope=ref, metric=rule.metric, spec=spec) for rule, ref, spec in scoped
        ]
        totals = await self._totals(queries, now=moment)

        violations: list[Violation] = []
        for (rule, _, spec), total in zip(scoped, totals, strict=True):
            current = from_units(rule.metric, total.units)
            # ">=" not ">": at the limit means spent, so the next request is
            # refused. See the overrun policy in the module docstring.
            if current >= rule.limit_value:
                violations.append(
                    Violation(
                        rule=rule,
                        current=current,
                        limit=rule.limit_value,
                        retry_after_seconds=spec.retry_after_seconds(moment, total.buckets),
                    )
                )

        if violations:
            raise QuotaExceeded(violations)

        # Only metrics that some rule actually watches get counters. A rule added
        # later starts from an empty cache; the database store still knows the
        # real history, which is why it is also the rebuild source.
        wanted: dict[tuple[ScopeRef, LimitMetric, int], WindowSpec] = {}
        for rule, ref, spec in scoped:
            wanted[(ref, rule.metric, spec.granularity_seconds)] = spec

        deltas: list[CounterDelta] = []
        reserved: dict[LimitMetric, int] = {}
        for (ref, metric, _), spec in wanted.items():
            units = to_units(metric, estimate.get(metric), round_up=True)
            reserved[metric] = units
            # Recorded even when the estimate is zero. settle() corrects only the
            # deltas a reservation holds, so dropping zero-unit entries here would
            # mean the *actual* usage for that metric is never counted — which is
            # exactly what happens for an unpriced model (estimated cost 0) or a
            # metric the caller could not estimate. The store itself skips
            # zero-valued writes, so this costs nothing on the wire.
            deltas.append(CounterDelta(scope=ref, metric=metric, spec=spec, units=units))

        reservation = Reservation(deltas=deltas, reserved=reserved, created_at=moment)
        await self._apply(deltas, now=moment)
        return reservation

    async def settle(
        self,
        reservation: Reservation,
        actual: QuotaAmounts,
        *,
        now: float | None = None,
    ) -> None:
        """Replace the reserved estimate with what actually happened.

        Applies the difference to the same bucket grid the reservation used, so
        the net effect on the window is exactly the actual usage.
        """
        if reservation.settled or reservation.is_empty:
            reservation.settled = True
            return

        moment = now if now is not None else utcnow().timestamp()
        corrections: list[CounterDelta] = []
        for delta in reservation.deltas:
            actual_units = to_units(delta.metric, actual.get(delta.metric))
            difference = actual_units - delta.units
            if difference:
                corrections.append(
                    CounterDelta(
                        scope=delta.scope,
                        metric=delta.metric,
                        spec=delta.spec,
                        units=difference,
                    )
                )
        reservation.settled = True
        # Corrections land in the *current* bucket, not the reservation's. For a
        # stream that outlives a bucket this shifts a little traffic forward in
        # time; the window total is unaffected, which is what rules read.
        await self._apply(corrections, now=moment)

    async def release(self, reservation: Reservation, *, now: float | None = None) -> None:
        """Undo a reservation entirely, for requests that never happened."""
        await self.settle(
            reservation,
            QuotaAmounts(requests=Decimal(0), tokens=Decimal(0), cost=Decimal(0)),
            now=now,
        )

    # -- store access with fallback ----------------------------------------

    def _verbose(self, key: str, now: float) -> bool:
        """Whether this occurrence should carry a full traceback.

        A counter store outage is not a one-off event: it recurs on **every**
        request, and each request touches the store three times (read, reserve,
        settle). Logging a traceback each time turns a degraded dependency into a
        second incident — the log volume itself — and buries the one line an
        operator needs under hundreds of identical stacks.

        So the first occurrence of each distinct failure is logged in full, and
        subsequent ones are one-liners until the interval elapses.
        """
        last = self._logged_at.get(key)
        if last is None or now - last >= _TRACEBACK_INTERVAL_SECONDS:
            self._logged_at[key] = now
            return True
        return False

    async def _totals(self, queries: Sequence[WindowQuery], *, now: float) -> list[WindowTotal]:
        try:
            return list(await self._store.totals(queries, now=now))
        except Exception:
            verbose = self._verbose("read", now)
            logger.warning(
                "counter store unavailable for reads%s",
                "" if verbose else " (traceback suppressed; still failing)",
                exc_info=verbose,
            )

        if self._fallback is not None and self._settings.fallback_to_database:
            try:
                if self._verbose("fallback", now):
                    # Deliberately at warning: running on the fallback is a
                    # degraded state an operator must be able to notice.
                    logger.warning("falling back to database counters")
                else:
                    logger.debug("still using database counters")
                return list(await self._fallback.totals(queries, now=now))
            except Exception:
                logger.error(
                    "database counter fallback failed",
                    exc_info=self._verbose("fallback_failed", now),
                )

        if self._settings.fail_open:
            logger.error("quota checks failing open; requests are not being limited")
            return [WindowTotal(units=0) for _ in queries]

        raise QuotaUnavailable("no counter store could answer")

    async def _apply(self, deltas: Sequence[CounterDelta], *, now: float) -> None:
        if not deltas:
            return
        try:
            await self._store.apply(deltas, now=now)
        except Exception:
            # Counters are a cache. Losing a write degrades quota precision until
            # the next rebuild but never loses money, because the ledger write is
            # a separate, transactional path.
            verbose = self._verbose("write", now)
            logger.warning(
                "counter store unavailable for writes%s",
                "" if verbose else " (traceback suppressed; still failing)",
                exc_info=verbose,
            )
