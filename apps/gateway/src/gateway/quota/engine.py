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

import contextlib
import logging
import uuid
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from gateway.config import QuotaSettings
from gateway.models import (
    LimitAppliesTo,
    LimitMetric,
    LimitRule,
    LimitScope,
    Membership,
    UsageRecord,
    UsageStatus,
    User,
)
from gateway.periods import Period, period_containing
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

# Held for a minute by the one worker that rebuilds, so N workers starting together
# run one ledger query between them. It is an election and nothing more: the
# rebuild is a replace and so is idempotent, and whether the cache is cold is read
# from the counters themselves, never from this key. (It used to be a permanent
# "seeded" marker, and the marker was the bug: it was claimed only once a rule
# existed, so counters written by live traffic after the first rule was created
# looked cold at the next start and the ledger was added on top of them.)
_REBUILD_LOCK_KEY = "q:rebuild-lock"
_REBUILD_LOCK_SECONDS = 60

# In-progress rows older than this are reported by the health view: a request does
# not take half an hour, so one that has is a settle that never landed.
_STALE_IN_PROGRESS_SECONDS = 30 * 60


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
    #: Calls to one of our own web-search backends, counted rather than priced
    #: (``LimitMetric.OWN_SEARCH_REQUESTS`` carries the reasoning).
    #:
    #: Defaults to zero where ``requests`` defaults to one, and the asymmetry is
    #: not an oversight: every metered call *is* one request by definition, and
    #: almost none of them searches. A default of one here would reserve a
    #: search against every embedding and every OCR page, and a search ceiling
    #: would then be exhausted by traffic that never searched.
    own_search_requests: Decimal = Decimal(0)

    def get(self, metric: LimitMetric) -> Decimal:
        match metric:
            case LimitMetric.REQUESTS:
                return self.requests
            case LimitMetric.TOKENS:
                return self.tokens
            case LimitMetric.COST:
                return self.cost
            case LimitMetric.OWN_SEARCH_REQUESTS:
                return self.own_search_requests


@dataclass(frozen=True, slots=True)
class Violation:
    rule: LimitRule
    current: Decimal
    limit: Decimal
    retry_after_seconds: int

    def describe(self) -> str:
        # window_label renders either kind: "3600s" or "month". Reading
        # window_seconds directly here printed "per Nones" for a calendar rule.
        # A per-member refusal names the member's share — their own current,
        # which they are entitled to know — and never the group's total, so
        # one member's 429 cannot report the group's spend to another member.
        # A per-user refusal reads the same way and for the same reason: the
        # figure is the person's own total, never anyone else's.
        kind = {
            LimitAppliesTo.EACH_MEMBER: " per member",
            LimitAppliesTo.EACH_USER: " per user",
        }.get(self.rule.applies_to, "")
        return (
            f"{self.rule.scope.value}{kind} limit of {self.limit} {self.rule.metric.value}"
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


def _negate(delta: CounterDelta) -> CounterDelta:
    """The same counter, moved the other way.

    Every window field travels with it. Rebuilding the delta from the query
    instead would be one place for `period` or `reset_epoch` to go missing, and
    an undo that lands on a different key than the reserve is worse than no
    undo at all — it leaves the reservation in place *and* subtracts from
    something else.
    """
    return CounterDelta(
        scope=delta.scope,
        metric=delta.metric,
        units=-delta.units,
        spec=delta.spec,
        period=delta.period,
        reset_epoch=delta.reset_epoch,
    )


@dataclass(frozen=True, slots=True)
class WindowHealth:
    """One rule's current window as the counter and the ledger each see it.

    For a per-member rule there is one of these per member: each member's
    budget is enforced independently, so drift in one member's counter is a
    fact about that member, and ``member_id`` says which. A per-user (global,
    ``each_user``) rule reports the same way, one window per user, and
    ``member_id`` is then the user.
    """

    rule: LimitRule
    window_id: str
    counter: Decimal | None  # None: the counter store could not be read
    ledger: Decimal
    counter_ttl_seconds: int | None
    stale_in_progress: int
    member_id: uuid.UUID | None = None

    @property
    def difference(self) -> Decimal | None:
        return None if self.counter is None else self.counter - self.ledger


@dataclass(frozen=True, slots=True)
class QuotaHealth:
    windows: list[WindowHealth]
    lock_present: bool
    lock_ttl_seconds: int | None


@dataclass(frozen=True, slots=True)
class Reconciled:
    rule: LimitRule
    window_id: str
    before: Decimal
    after: Decimal
    member_id: uuid.UUID | None = None


class QuotaEngine:
    def __init__(
        self,
        store: CounterStore,
        *,
        settings: QuotaSettings,
        fallback: CounterStore | None = None,
        billing_timezone: str = "Europe/Rome",
        notifier: Any | None = None,
    ) -> None:
        self._store = store
        self._fallback = fallback
        self._settings = settings
        # The quota notifier (ADR 0052), if the deployment has one wired: an
        # optional observer called with every measured (rule, percentage) so
        # admission stays ignorant of mail, thresholds and everything else.
        self._notifier = notifier
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

    @staticmethod
    def scope_for_rule(rule: LimitRule, subject: QuotaSubject) -> ScopeRef | None:
        """The counter scope a rule counts under for this subject.

        An ``each_member`` group rule counts the member's own usage billed to
        the group — ``(group, member)`` — so the scope carries both ids. An
        ``each_user`` global rule counts the person's whole usage under the
        ordinary user scope, ``(user, person)``: the very counter a
        ``scope=user`` rule for them reads, because it is the same quantity
        (see :meth:`query_for`). A subject with no user cannot be attributed
        to anyone, and those rules are skipped: every path that reserves
        today carries an authenticated user, and the guard is what keeps a
        future user-less path from enforcing a budget it cannot count.
        """
        if rule.scope is LimitScope.GROUP and rule.applies_to is LimitAppliesTo.EACH_MEMBER:
            if subject.user_id is None:
                return None
            return ScopeRef(LimitScope.GROUP, rule.scope_id, member_id=subject.user_id)
        if rule.scope is LimitScope.GLOBAL and rule.applies_to is LimitAppliesTo.EACH_USER:
            if subject.user_id is None:
                return None
            return ScopeRef(LimitScope.USER, subject.user_id)
        return ScopeRef(rule.scope, rule.scope_id)

    async def current_values(
        self,
        rules: Sequence[LimitRule],
        *,
        now: float | None = None,
        subject: QuotaSubject | None = None,
    ) -> dict[uuid.UUID, Decimal]:
        """Consumption so far for each rule, in the rule's own metric.

        For display. Reads the same counters enforcement reads — including the
        reset watermark — so what an administrator sees is what the next request
        will be judged against, rather than a separate calculation that can drift
        from it.

        A per-member rule answers for one member only, so it needs the subject
        to attribute to: without one it is skipped, and the caller reads the
        absence as "no single figure" rather than zero. Callers that show one
        person's budget pass them; callers that list every rule do not.

        Best-effort: if the counter store cannot answer this returns nothing at
        all, because a page rendering "EUR 0.00 of EUR 10" during an outage is
        worse than one rendering nothing.
        """
        if not rules:
            return {}
        moment = now if now is not None else utcnow().timestamp()
        as_datetime = datetime.fromtimestamp(moment, tz=UTC)

        queries = []
        answerable = []
        for rule in rules:
            spec, period = self.window_for(rule, as_datetime)
            scope = self.scope_for_rule(rule, subject or QuotaSubject(None, None))
            if scope is None:
                continue
            answerable.append(rule)
            queries.append(
                WindowQuery(
                    scope=scope,
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
            for rule, total in zip(answerable, totals, strict=True)
        }

    def query_for(
        self, rule: LimitRule, moment: float, member_id: uuid.UUID | None = None
    ) -> WindowQuery:
        """The rule's current window as a counter query, reset watermark included.

        A per-member or per-user rule needs the person: without one there is
        no key to ask about. An aggregate rule takes none.

        A per-user rule reads the plain ``user:<id>`` counter rather than a
        key of its own. ``scope=user`` rules and per-user rules both mean "this
        person's total spend", so for the same metric, window and reset they
        are the same number; one key means one reserve, one settle and one
        refund per request however many such rules apply, and seeding or
        reconciling either repairs both. A reset is the exception that keeps
        them independent where it should: the watermark is in the key, so
        resetting one rule renames only its own counter.
        """
        if rule.scope is LimitScope.GROUP and rule.applies_to is LimitAppliesTo.EACH_MEMBER:
            assert member_id is not None, "a per-member query needs its member"
            scope = ScopeRef(LimitScope.GROUP, rule.scope_id, member_id=member_id)
        elif rule.scope is LimitScope.GLOBAL and rule.applies_to is LimitAppliesTo.EACH_USER:
            assert member_id is not None, "a per-user query needs its user"
            scope = ScopeRef(LimitScope.USER, member_id)
        else:
            assert member_id is None, "only a per-member rule takes a member"
            scope = ScopeRef(rule.scope, rule.scope_id)
        spec, period = self.window_for(rule, datetime.fromtimestamp(moment, tz=UTC))
        return WindowQuery(
            scope=scope,
            metric=rule.metric,
            spec=spec,
            period=period,
            reset_epoch=self.reset_epoch_for(rule),
        )

    async def _active_rules(self, session: AsyncSession) -> list[LimitRule]:
        return list(
            (
                await session.execute(
                    select(LimitRule)
                    .where(LimitRule.is_active.is_(True))
                    # Resets are read by query_for, and touching an unloaded
                    # relationship from async code raises MissingGreenlet.
                    .options(selectinload(LimitRule.resets))
                    .execution_options(populate_existing=True)
                )
            )
            .scalars()
            .all()
        )

    @staticmethod
    async def _member_ids(session: AsyncSession, group_id: uuid.UUID) -> list[uuid.UUID]:
        """Everyone holding a membership in the group: the counter namespace.

        Membership is the only per-user fact, so a member here is a holder of
        a membership row — including a disabled user's, whose settled counters
        still exist and still need repairing even though they can spend no
        more.
        """
        return list(
            (
                await session.execute(
                    select(Membership.user_id).where(Membership.group_id == group_id)
                )
            )
            .scalars()
            .all()
        )

    @staticmethod
    async def _member_ids_with_usage(
        session: AsyncSession, group_id: uuid.UUID, since: datetime
    ) -> list[uuid.UUID]:
        """Members with settled usage billed to the group since *since*.

        The cheap enumeration for seeding and cold rebuilds: a member with no
        usage rebuilds to nothing, so asking the ledger per member would spend
        a query to write no keys. Reconcile and health do not use this — their
        job is drift, and a zero-usage member with a non-zero counter is
        exactly the drift they must reach.
        """
        # The is_not(None) filter is not part of the SQL type; the cast states
        # what the filter guarantees.
        rows: Sequence[uuid.UUID | None] = (
            (
                await session.execute(
                    select(UsageRecord.user_id)
                    .where(
                        UsageRecord.group_id == group_id,
                        UsageRecord.created_at >= since,
                        UsageRecord.status != UsageStatus.IN_PROGRESS,
                        UsageRecord.user_id.is_not(None),
                    )
                    .distinct()
                )
            )
            .scalars()
            .all()
        )
        return [user_id for user_id in rows if user_id is not None]

    @staticmethod
    async def _user_ids(session: AsyncSession) -> list[uuid.UUID]:
        """Every user row: the counter namespace of a per-user rule.

        A user is a counter holder from their first request, so the set that
        can hold a counter is the users table — disabled people included, for
        the reason ``_member_ids`` gives.
        """
        return list((await session.execute(select(User.id))).scalars().all())

    @staticmethod
    async def _user_ids_with_usage(session: AsyncSession, since: datetime) -> list[uuid.UUID]:
        """Users with settled usage anywhere since *since*: one ledger query."""
        rows: Sequence[uuid.UUID | None] = (
            (
                await session.execute(
                    select(UsageRecord.user_id)
                    .where(
                        UsageRecord.created_at >= since,
                        UsageRecord.status != UsageStatus.IN_PROGRESS,
                        UsageRecord.user_id.is_not(None),
                    )
                    .distinct()
                )
            )
            .scalars()
            .all()
        )
        return [user_id for user_id in rows if user_id is not None]

    def _window_since(self, rule: LimitRule, moment: float) -> datetime:
        """The earliest instant that can still count towards the rule's window."""
        spec, period = self.window_for(rule, datetime.fromtimestamp(moment, tz=UTC))
        if period is not None:
            return period.start
        assert spec is not None
        return datetime.fromtimestamp(moment - spec.window_seconds, tz=UTC)

    async def _queries_for_rule(
        self, session: AsyncSession, rule: LimitRule, moment: float, *, with_usage_only: bool
    ) -> list[tuple[uuid.UUID | None, WindowQuery]]:
        """The rule's current windows as counter queries: one per member for a
        per-member rule, each paired with its member, else the rule's own."""
        if (
            rule.scope is LimitScope.GROUP
            and rule.applies_to is LimitAppliesTo.EACH_MEMBER
            and rule.scope_id is not None
        ):
            if with_usage_only:
                members = await self._member_ids_with_usage(
                    session, rule.scope_id, self._window_since(rule, moment)
                )
            else:
                members = await self._member_ids(session, rule.scope_id)
            return [(member, self.query_for(rule, moment, member)) for member in members]
        if rule.scope is LimitScope.GLOBAL and rule.applies_to is LimitAppliesTo.EACH_USER:
            if with_usage_only:
                users = await self._user_ids_with_usage(session, self._window_since(rule, moment))
            else:
                users = await self._user_ids(session)
            return [(user, self.query_for(rule, moment, user)) for user in users]
        return [(None, self.query_for(rule, moment))]

    async def seed_rule(
        self, session: AsyncSession, rule: LimitRule, *, now: float | None = None
    ) -> bool:
        """Prime a newly created rule's counter from the ledger.

        Without this, a rule added mid-period starts from an empty counter — which
        answers zero, confidently. For "EUR 5 this month" created on the 20th that
        means the cap is wrong for eleven days: the group has already spent the
        money, and the gateway does not know. The cold-cache rebuild does not help,
        because the cache is not cold; only this rule is.

        A per-member rule primes each member that already spent in the window —
        a new per-member rule starts every member at zero otherwise, the same
        wrong-for-the-rest-of-the-month once per member.

        The same argument is weaker for a rolling window, which self-corrects within
        one window, but seeding is correct for both and the code is the same.

        Returns True if the counter was seeded. Best-effort: a failure here leaves
        the rule under-counted rather than blocking its creation, and is logged.
        """
        if not self._settings.enabled or self._fallback is None:
            return False

        moment = now if now is not None else utcnow().timestamp()
        queries = [
            query
            for _, query in await self._queries_for_rule(
                session, rule, moment, with_usage_only=True
            )
        ]
        try:
            # A replace, so seeding is safe at any time: a rule created, edited or
            # re-activated while its counter already carries traffic ends at the
            # ledger's figure, not at the sum of both. A scope with no history
            # costs one query and no keys.
            await self._fallback.rebuild_into(self._store, queries, now=moment)  # type: ignore[attr-defined]
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
        """Repopulate the counter cache from the ledger where it looks freshly empty.

        Why this exists, found by restarting Valkey under a running stack: the
        counter cache is disposable for *reads* — a failed read falls back to the
        database — but an **empty** cache is not a failed read. It answers
        confidently with zero. So after Valkey is wiped or restarted without its
        snapshot, every group silently receives a fresh budget, and the gateway
        cheerfully serves a group that is already over its limit. Observed: the
        ledger said €11.70 spent against a €10 ceiling while Valkey said €0.90.

        **Cold is read from the counters, per window: a counter that reads zero.**
        This used to be a "seeded" marker key whose presence stood for "the cache
        still holds our data", and it was wrong both ways. It was claimed only
        when a rule already existed, so a deployment that created its first rule
        and served traffic had counters and no marker; the next start took the
        cache for cold and *added* the ledger onto the live counters, counting
        everything since the rule was created twice (observed: €0.38 against a
        ledger of €0.28). A marker is a second fact that has to be kept true in
        step with the first, in every path that writes a counter; the counter is
        the fact itself. Reading it cannot go stale, covers a partially lost
        cache (one scope's keys expired or evicted, the rest intact) which the
        marker could not, and needs no TTL sized to the longest window.

        Two things make a spurious rebuild harmless rather than merely rare: the
        rebuild *replaces* (``CounterStore.replace``), so rebuilding a window
        that was in fact warm gives the ledger's figure rather than a double;
        and a window is rebuilt only where it reads zero, so a warm one is not
        touched at all. A window with a zero counter and a zero ledger is
        written as nothing, so an idle deployment costs one query per rule.

        What this cannot see is a counter that is present but *wrong* — a stale
        snapshot, a lost write. That is what :meth:`reconcile` is for.

        ``claim_once`` elects one worker so N starting together run one ledger
        query between them. It is an optimisation, not a safeguard.

        Returns True if this process performed a rebuild, whether or not the
        ledger had anything to write into the windows it found empty.
        """
        if not self._settings.enabled or self._fallback is None:
            return False

        moment = now if now is not None else utcnow().timestamp()
        rules = await self._active_rules(session)
        if not rules:
            return False

        queries = [
            query
            for rule in rules
            for _, query in await self._queries_for_rule(
                session, rule, moment, with_usage_only=True
            )
        ]
        try:
            current = await self._store.totals(queries, now=moment)
            cold = [query for query, total in zip(queries, current, strict=True) if not total.units]
            if not cold:
                logger.debug("counter cache already warm; no rebuild needed")
                return False
            if not await self._store.claim_once(
                _REBUILD_LOCK_KEY, ttl_seconds=_REBUILD_LOCK_SECONDS
            ):
                logger.debug("another worker is rebuilding the counter cache")
                return False
        except Exception:
            logger.warning(
                "could not check whether the counter cache is cold; skipping rebuild",
                exc_info=True,
            )
            return False

        try:
            written = await self._fallback.rebuild_into(  # type: ignore[attr-defined]
                self._store, cold, now=moment
            )
        except Exception:
            logger.error("counter cache rebuild failed", exc_info=True)
            return False

        restored = sum(1 for total in written if total.units)
        if restored:
            logger.warning(
                "counter cache was cold and has been rebuilt from the ledger "
                "(%d window(s)); quotas would otherwise have started from zero",
                restored,
            )
        return True

    async def reconcile(
        self, session: AsyncSession, *, now: float | None = None
    ) -> list[Reconciled]:
        """Recompute every active rule's current window from the ledger, by replacing.

        The repair for drift of any cause: a lost write, a stale snapshot, a
        settle that never landed, the double count this module used to produce.
        Same replace semantics as the cold-cache rebuild, so it is idempotent and
        safe to run on a live system.

        The trade-off, stated because it is the whole risk: the ledger is read,
        then the counter is overwritten, and the two are not one transaction.
        What happens in between is lost — an increment landing after the read
        and a reservation held at that instant (counted by the counter, absent
        from the ledger until it settles). The window is one ledger query long,
        the loss is bounded by the traffic in flight over it, and it errs
        *low*: the counter may briefly read under the truth, and the next
        reconcile or the window rolling over repairs it. It never errs high, so
        it cannot refuse a request that should have been admitted. Admission
        keeps working throughout; nothing is locked.

        Raises if the counter store cannot be written: a reconcile that silently
        did nothing would report a repair it did not make.
        """
        if self._fallback is None:
            raise QuotaUnavailable("no ledger to reconcile against")
        moment = now if now is not None else utcnow().timestamp()
        rules = await self._active_rules(session)
        expanded: list[tuple[LimitRule, uuid.UUID | None, WindowQuery]] = []
        for rule in rules:
            for member, query in await self._queries_for_rule(
                session, rule, moment, with_usage_only=False
            ):
                expanded.append((rule, member, query))
        queries = [query for _, _, query in expanded]
        before = await self._store.totals(queries, now=moment)
        written = await self._fallback.rebuild_into(  # type: ignore[attr-defined]
            self._store, queries, now=moment
        )
        return [
            Reconciled(
                rule=rule,
                window_id=query.window_id,
                before=from_units(rule.metric, was.units),
                after=from_units(rule.metric, now_.units),
                member_id=member,
            )
            for (rule, member, query), was, now_ in zip(expanded, before, written, strict=True)
        ]

    async def health(self, session: AsyncSession, *, now: float | None = None) -> QuotaHealth:
        """Read-only: each rule's current window, counter against ledger.

        Reads only; nothing here writes a counter. A counter that cannot be read
        is reported as unknown (``None``), never as zero, because "zero" is
        exactly the confident wrong answer this view exists to catch.

        A per-member rule reports one window per member: each member's budget
        is enforced independently, so drift in one member's counter is a fact
        about that member.
        """
        if self._fallback is None:
            raise QuotaUnavailable("no ledger to compare against")
        moment = now if now is not None else utcnow().timestamp()
        rules = list(
            (
                await session.execute(
                    select(LimitRule)
                    .options(selectinload(LimitRule.resets))
                    .order_by(LimitRule.scope, LimitRule.metric)
                    .execution_options(populate_existing=True)
                )
            )
            .scalars()
            .all()
        )
        expanded: list[tuple[LimitRule, uuid.UUID | None, WindowQuery]] = []
        for rule in rules:
            for member, query in await self._queries_for_rule(
                session, rule, moment, with_usage_only=False
            ):
                expanded.append((rule, member, query))
        queries = [query for _, _, query in expanded]
        ledger = await self._fallback.totals(queries, now=moment)
        stale = await self._fallback.stale_in_progress(  # type: ignore[attr-defined]
            queries, now=moment, older_than_seconds=_STALE_IN_PROGRESS_SECONDS
        )

        counters: list[int | None] = [None] * len(queries)
        ttls: list[int | None] = [None] * len(queries)
        lock_present, lock_ttl = False, None
        try:
            counters = [t.units for t in await self._store.totals(queries, now=moment)]
            ttls = await self._store.ttls(queries, now=moment)
            lock_ttl = await self._store.key_ttl(_REBUILD_LOCK_KEY)
            lock_present = lock_ttl is not None
        except Exception:
            logger.warning("counter store unreadable for the health view", exc_info=True)

        return QuotaHealth(
            windows=[
                WindowHealth(
                    rule=rule,
                    window_id=query.window_id,
                    counter=None if units is None else from_units(rule.metric, units),
                    ledger=from_units(rule.metric, total.units),
                    counter_ttl_seconds=ttl,
                    stale_in_progress=count,
                    member_id=member,
                )
                for (rule, member, query), units, total, ttl, count in zip(
                    expanded, counters, ledger, ttls, stale, strict=True
                )
            ],
            lock_present=lock_present,
            lock_ttl_seconds=lock_ttl,
        )

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

        # Read the clock again, now that the last await before reserving is
        # behind us. A moment taken before the rule query is stale by however
        # long the query took, and a stale moment is not harmless: the
        # reservation lands in the bucket of that earlier time and the total is
        # summed only up to it, so a request that crossed a bucket boundary
        # inside the query reserves behind requests that arrived later and
        # cannot see them. That admitted 4 of 50 against a limit of 3 (issue
        # #12). A caller-supplied ``now`` is a pinned clock and stays as given.
        if now is None:
            moment = utcnow().timestamp()
        as_datetime = datetime.fromtimestamp(moment, tz=UTC)
        scoped: list[tuple[LimitRule, WindowQuery]] = []
        for rule in rules:
            spec, period = self.window_for(rule, as_datetime)
            scope = self.scope_for_rule(rule, subject)
            if scope is None:
                # No user to attribute a per-member rule to: skipped, not
                # evaluated. See scope_for_rule.
                continue
            scoped.append(
                (
                    rule,
                    WindowQuery(
                        scope=scope,
                        metric=rule.metric,
                        spec=spec,
                        period=period,
                        reset_epoch=self.reset_epoch_for(rule),
                    ),
                )
            )

        queries = [query for _, query in scoped]

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
        contribution: dict[tuple[str, str, str, int], int] = {}
        for key, query in wanted.items():
            units = to_units(query.metric, estimate.get(query.metric), round_up=True)
            reserved[query.metric] = units
            contribution[key] = units
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

        # Reserve first, then judge what we found — see CounterStore.
        # apply_and_total for why this order is the one that is not a race.
        totals = await self._reserve(deltas, queries, now=moment)

        violations: list[Violation] = []
        measured: list[tuple[LimitRule, int]] = []
        for (rule, query), total in zip(scoped, totals, strict=True):
            # Our own contribution is subtracted back out, so the decision is
            # made on what the window held *before* this request — which is the
            # documented policy: refused at the limit, and an admitted request
            # may overshoot by its own usage. Reserving first changes when the
            # increment happens, never what the rule means.
            mine = contribution.get(
                (query.scope.key_part, query.metric.value, query.window_id, query.reset_epoch), 0
            )
            current = from_units(rule.metric, max(0, total.units - mine))
            if rule.limit_value > 0:
                measured.append(
                    (
                        rule,
                        min(100, int(current / rule.limit_value * 100)),
                    )
                )
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

        # Announce crossings before judging violations: a request that busts
        # the limit is exactly when a user asked to hear about it, and the
        # notifier is in-memory + detached, so it costs admission nothing.
        if self._notifier is not None and measured:
            with contextlib.suppress(Exception):
                self._notifier.observe(measured, requester=subject.user_id)

        if violations:
            # Undo what was reserved a moment ago. The refusal is the caller's
            # answer, so their estimate must not keep occupying the window —
            # holding it would refuse the *next* caller too, for a request that
            # never ran.
            await self._apply([_negate(delta) for delta in deltas], now=moment)
            raise QuotaExceeded(violations)

        return Reservation(deltas=deltas, reserved=reserved, created_at=moment)

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
            QuotaAmounts(
                requests=Decimal(0),
                tokens=Decimal(0),
                cost=Decimal(0),
                own_search_requests=Decimal(0),
            ),
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

    async def _reserve(
        self,
        deltas: Sequence[CounterDelta],
        queries: Sequence[WindowQuery],
        *,
        now: float,
    ) -> list[WindowTotal]:
        """Atomic reserve-then-read, with the same degradation ladder as a read.

        The fallback path cannot reserve — it counts settled ledger rows — so a
        deployment running on it keeps the wider overshoot bound documented on
        DatabaseCounterStore. Failing open returns zeroes, which admits the
        request; that is what failing open means.
        """
        try:
            return list(await self._store.apply_and_total(deltas, queries, now=now))
        except Exception:
            verbose = self._verbose("read", now)
            logger.warning(
                "counter store unavailable for reserve%s",
                "" if verbose else " (traceback suppressed; still failing)",
                exc_info=verbose,
            )

        if self._fallback is not None and self._settings.fallback_to_database:
            try:
                if self._verbose("fallback", now):
                    logger.warning("falling back to database counters")
                else:
                    logger.debug("still using database counters")
                return list(await self._fallback.apply_and_total(deltas, queries, now=now))
            except Exception:
                logger.error(
                    "database counter fallback failed",
                    exc_info=self._verbose("fallback_failed", now),
                )

        if self._settings.fail_open:
            logger.error("quota checks failing open; requests are not being limited")
            return [WindowTotal(units=0) for _ in queries]

        raise QuotaUnavailable("no counter store could answer")

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
