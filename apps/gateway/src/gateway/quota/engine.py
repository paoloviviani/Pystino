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
from datetime import UTC, datetime
from decimal import Decimal

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from gateway.config import QuotaSettings
from gateway.models import LimitMetric, LimitRule, LimitScope
from gateway.periods import Period, PeriodKind, period_containing
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

# Approximate span of each calendar period, used only to size the rebuild marker's
# TTL. Upper bounds on purpose: a marker that lives slightly too long is harmless,
# one that expires early causes a needless rebuild.
_CALENDAR_SPAN_SECONDS = {
    PeriodKind.DAY: 86_400,
    PeriodKind.WEEK: 7 * 86_400,
    PeriodKind.MONTH: 31 * 86_400,
    PeriodKind.QUARTER: 92 * 86_400,
    PeriodKind.YEAR: 366 * 86_400,
}


@dataclass(frozen=True, slots=True)
class QuotaSubject:
    """Who is being charged, and therefore which rules apply."""

    user_id: uuid.UUID | None
    group_id: uuid.UUID | None
    api_key_id: uuid.UUID | None = None

    def scope_refs(self) -> list[ScopeRef]:
        refs = [ScopeRef(LimitScope.GLOBAL)]
        if self.group_id is not None:
            refs.append(ScopeRef(LimitScope.GROUP, self.group_id))
        if self.user_id is not None:
            refs.append(ScopeRef(LimitScope.USER, self.user_id))
        if self.api_key_id is not None:
            refs.append(ScopeRef(LimitScope.API_KEY, self.api_key_id))
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
        # window_label renders either kind: "3600s" or "month". Reading
        # window_seconds directly here printed "per Nones" for a calendar rule.
        return (
            f"{self.rule.scope.value} limit of {self.limit} {self.rule.metric.value}"
            f" per {self.rule.window_label} exceeded (current {self.current})"
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
        billing_timezone: str = "Europe/Rome",
    ) -> None:
        self._store = store
        self._fallback = fallback
        self._settings = settings
        # The same timezone reporting uses, so a calendar budget and a monthly report
        # share one definition of when the period starts (ADR 0024, ADR 0025).
        self._billing_timezone = billing_timezone
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
        if subject.api_key_id is not None:
            conditions.append(
                (LimitRule.scope == LimitScope.API_KEY) & (LimitRule.scope_id == subject.api_key_id)
            )

        stmt = (
            select(LimitRule)
            .where(LimitRule.is_active.is_(True), or_(*conditions))
            # Resets come along: the watermark is part of evaluating the rule, not a
            # separate lookup, and loading it lazily here would be a query per rule.
            .options(selectinload(LimitRule.resets))
            # Sessions are created with expire_on_commit=False (db.py), so an
            # already-loaded `resets` collection is *not* refreshed by a later
            # selectinload in the same session — SQLAlchemy leaves populated
            # collections alone. A reset created and committed earlier in the same
            # session would then be invisible to enforcement. Costs no extra query:
            # the rows are being fetched either way.
            .execution_options(populate_existing=True)
        )
        return list((await session.execute(stmt)).scalars().all())

    def window_for(
        self, rule: LimitRule, moment: datetime
    ) -> tuple[WindowSpec | None, Period | None]:
        """The rule's window: rolling spec or calendar period, never both."""
        if rule.period is not None:
            return None, period_containing(rule.period, moment, timezone=self._billing_timezone)
        assert rule.window_seconds is not None  # guaranteed by ck_limit_rules_one_window_kind
        return self._spec(rule.window_seconds), None

    @staticmethod
    def reset_epoch_for(rule: LimitRule) -> int:
        """Seconds-since-epoch of the rule's latest reset, or 0 if never reset.

        Goes into the counter key rather than being subtracted afterwards, so a reset
        renames the keys and the old values orphan (ADR 0025).
        """
        if not rule.resets:
            return 0
        return int(max(reset.effective_at for reset in rule.resets).timestamp())

    async def current_values(
        self, rules: Sequence[LimitRule], *, now: float | None = None
    ) -> dict[uuid.UUID, Decimal]:
        """Consumption so far for each rule, in the rule's own metric.

        For display. Reads the same counters enforcement reads — including the
        reset watermark — so what an administrator sees is what the next request
        will be judged against, rather than a separate calculation that can drift
        from it.

        Best-effort: if the counter store cannot answer this returns nothing at
        all, because a page rendering "EUR 0.00 of EUR 10" during an outage is
        worse than one rendering nothing.
        """
        if not rules:
            return {}
        moment = now if now is not None else utcnow().timestamp()
        as_datetime = datetime.fromtimestamp(moment, tz=UTC)

        queries = []
        for rule in rules:
            spec, period = self.window_for(rule, as_datetime)
            queries.append(
                WindowQuery(
                    scope=ScopeRef(rule.scope, rule.scope_id),
                    metric=rule.metric,
                    spec=spec,
                    period=period,
                    reset_epoch=self.reset_epoch_for(rule),
                )
            )

        try:
            totals = await self._totals(queries, now=moment)
        except QuotaUnavailable:
            return {}
        return {
            rule.id: from_units(rule.metric, total.units)
            for rule, total in zip(rules, totals, strict=True)
        }

    async def seed_rule(self, rule: LimitRule, *, now: float | None = None) -> bool:
        """Prime a newly created rule's counter from the ledger.

        Without this, a rule added mid-period starts from an empty counter — which
        answers zero, confidently. For "EUR 5 this month" created on the 20th that
        means the cap is wrong for eleven days: the group has already spent the
        money, and the gateway does not know. The cold-cache rebuild does not help,
        because the cache is not cold; only this rule is.

        The same argument is weaker for a rolling window, which self-corrects within
        one window, but seeding is correct for both and the code is the same.

        Returns True if the counter was seeded. Best-effort: a failure here leaves
        the rule under-counted rather than blocking its creation, and is logged.
        """
        if not self._settings.enabled or self._fallback is None:
            return False

        moment = now if now is not None else utcnow().timestamp()
        spec, period = self.window_for(rule, datetime.fromtimestamp(moment, tz=UTC))
        query = WindowQuery(
            scope=ScopeRef(rule.scope, rule.scope_id),
            metric=rule.metric,
            spec=spec,
            period=period,
            reset_epoch=self.reset_epoch_for(rule),
        )
        try:
            # Writes only if the ledger has something to write, so creating a rule
            # for a scope with no history costs one query and no keys.
            await self._fallback.rebuild_into(self._store, [query], now=moment)  # type: ignore[attr-defined]
        except Exception:
            logger.error(
                "could not seed counters for new limit rule %s; it will under-count "
                "usage already recorded in this window",
                rule.id,
                exc_info=True,
            )
            return False
        return True

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
            (
                await session.execute(
                    select(LimitRule)
                    .where(LimitRule.is_active.is_(True))
                    # Resets are read below, and touching an unloaded relationship
                    # from async code raises MissingGreenlet — the same trap that
                    # bit user provisioning once already.
                    .options(selectinload(LimitRule.resets))
                )
            )
            .scalars()
            .all()
        )
        if not rules:
            return False

        # Generously longer than any window, so a rebuild happens only when the
        # cache genuinely lost its contents rather than on every window rollover.
        # A calendar rule has no window_seconds, so its span stands in — approximate
        # is fine, this only sizes a TTL.
        spans: list[int] = []
        for rule in rules:
            if rule.window_seconds is not None:
                spans.append(rule.window_seconds)
            elif rule.period is not None:
                spans.append(_CALENDAR_SPAN_SECONDS[rule.period])
            else:
                # ck_limit_rules_one_window_kind forbids this; belt and braces.
                spans.append(366 * 86_400)
        marker_ttl = max(spans) * 4

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
                spec=spec,
                period=period,
                reset_epoch=self.reset_epoch_for(rule),
            )
            for rule in rules
            for spec, period in [self.window_for(rule, utcnow())]
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

        as_datetime = datetime.fromtimestamp(moment, tz=UTC)
        scoped: list[tuple[LimitRule, WindowQuery]] = []
        for rule in rules:
            spec, period = self.window_for(rule, as_datetime)
            scoped.append(
                (
                    rule,
                    WindowQuery(
                        scope=ScopeRef(rule.scope, rule.scope_id),
                        metric=rule.metric,
                        spec=spec,
                        period=period,
                        reset_epoch=self.reset_epoch_for(rule),
                    ),
                )
            )

        queries = [query for _, query in scoped]
        totals = await self._totals(queries, now=moment)

        violations: list[Violation] = []
        for (rule, query), total in zip(scoped, totals, strict=True):
            current = from_units(rule.metric, total.units)
            # ">=" not ">": at the limit means spent, so the next request is
            # refused. See the overrun policy in the module docstring.
            if current >= rule.limit_value:
                if query.period is not None:
                    # A calendar period knows exactly when it ends; no estimate needed.
                    retry_after = max(1, query.period.seconds_remaining(as_datetime))
                else:
                    assert query.spec is not None
                    retry_after = query.spec.retry_after_seconds(moment, total.buckets)
                violations.append(
                    Violation(
                        rule=rule,
                        current=current,
                        limit=rule.limit_value,
                        retry_after_seconds=retry_after,
                    )
                )

        if violations:
            raise QuotaExceeded(violations)

        # Only metrics that some rule actually watches get counters. A rule added
        # later starts from an empty cache; the database store still knows the
        # real history, which is why it is also the rebuild source.
        # One delta per distinct counter. Two rules sharing a scope, metric and
        # window share a counter and must not be double-counted.
        wanted: dict[tuple[str, str, str, int], WindowQuery] = {}
        for _, query in scoped:
            wanted[
                (query.scope.key_part, query.metric.value, query.window_id, query.reset_epoch)
            ] = query

        deltas: list[CounterDelta] = []
        reserved: dict[LimitMetric, int] = {}
        for query in wanted.values():
            units = to_units(query.metric, estimate.get(query.metric), round_up=True)
            reserved[query.metric] = units
            # Recorded even when the estimate is zero. settle() corrects only the
            # deltas a reservation holds, so dropping zero-unit entries here would
            # mean the *actual* usage for that metric is never counted — which is
            # exactly what happens for an unpriced model (estimated cost 0) or a
            # metric the caller could not estimate. The store itself skips
            # zero-valued writes, so this costs nothing on the wire.
            deltas.append(
                CounterDelta(
                    scope=query.scope,
                    metric=query.metric,
                    units=units,
                    spec=query.spec,
                    period=query.period,
                    reset_epoch=query.reset_epoch,
                )
            )

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
                # Reuses the reservation's own window, so a stream that began on 31
                # August settles into August even though it finished in September.
                corrections.append(
                    CounterDelta(
                        scope=delta.scope,
                        metric=delta.metric,
                        units=difference,
                        spec=delta.spec,
                        period=delta.period,
                        reset_epoch=delta.reset_epoch,
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
