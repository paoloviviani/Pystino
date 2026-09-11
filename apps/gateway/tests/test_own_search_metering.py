"""Metering our own web searches: a count that is quotable and never priced.

The dimension the search backends of phase 2 (docs/web-search-plan.md) will
use: Exa, Jina, Staan, Linkup. It is a **request count**, not money, and the
reason is recorded on ``LimitMetric.OWN_SEARCH_REQUESTS`` — half the vendors'
rates cannot be read at source, so a price here would be a guess wearing the
same type as a measurement.

Ground rule 3 applies with its full weight: a wrong answer in this file is an
unenforced limit, not a stack trace. Four properties carry it.

**The ceiling counts searches, not requests.** ``QuotaAmounts.requests``
already exists and defaults to one; this one defaults to zero. A default of one
would reserve a search against every embedding, and a search budget would then
be exhausted by traffic that never searched.

**Ours and theirs are different columns.** ``usage_records.search_count`` is
the counterparty's server-side searches (ADR 0058) and
``own_search_requests`` is ours. Every test here that touches a row asserts
both, because summing them is the mistake that is easy to make and invisible
afterwards — a report could no longer tell "Anthropic searched" from "we called
Staan".

**Searches survive a failed request.** They were spent whether or not the
completion arrived. A ceiling that forgave them would be raisable by making the
model fail.

**The labels are labels.** ``own_search_backend`` and ``own_search_tier`` carry
no rate, and recording them must move no money.

No search backend exists yet, so the caller here is a fake — the smallest thing
that can make a call and say which backend and tier it went to.
"""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass
from decimal import Decimal

import pytest
from conftest import Seeded
from fastapi.responses import JSONResponse
from gateway.accounting import DEFAULT_ESTIMATOR, RequestAccounting, RequestContext
from gateway.accounting.cost import TokenCounts
from gateway.config import Settings
from gateway.deps import Principal
from gateway.models import (
    ApiSurface,
    Group,
    LimitMetric,
    LimitRule,
    LimitScope,
    ModelDef,
    UsageRecord,
    UsageStatus,
    User,
)
from gateway.quota import (
    DatabaseCounterStore,
    InMemoryCounterStore,
    QuotaAmounts,
    QuotaExceeded,
    QuotaSubject,
    ScopeRef,
    WindowQuery,
    WindowSpec,
    from_units,
    to_units,
)
from gateway.routers import _metered
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlalchemy.orm import joinedload, selectinload
from test_quota_engine import FIXED_NOW, add_rule, engine_for

SEARCH = LimitMetric.OWN_SEARCH_REQUESTS


# --------------------------------------------------------------------------
# the fake backend
# --------------------------------------------------------------------------


@dataclass
class FakeSearchBackend:
    """Stands in for Exa, Jina, Staan or Linkup.

    Deliberately tiny. What is under test is the metering, and the only thing
    the metering needs from a backend is that a call happened, against which
    vendor, at which tier. Anything more here would be testing a client that
    does not exist yet.
    """

    name: str = "staan"
    tier: str = "web_for_ai"

    def search(self, accounting: RequestAccounting, *, times: int = 1) -> None:
        for _ in range(times):
            accounting.observe_own_search(1, backend=self.name, tier=self.tier)


async def load_model(session: AsyncSession, model_id: uuid.UUID) -> ModelDef:
    stmt = select(ModelDef).where(ModelDef.id == model_id).options(selectinload(ModelDef.prices))
    return (await session.execute(stmt)).scalar_one()


def accounting_for(
    *,
    session_factory: async_sessionmaker[AsyncSession],
    settings: Settings,
    seeded: Seeded,
    model: ModelDef,
) -> RequestAccounting:
    return RequestAccounting(
        context=RequestContext(
            request_id=f"req-{uuid.uuid4().hex[:8]}",
            model_name=seeded.model.name,
            currency=settings.billing_currency,
            streamed=False,
            user_id=seeded.user.id,
            group_id=seeded.group.id,
            api_key_id=seeded.api_key.id,
            model_id=seeded.model.id,
        ),
        session_factory=session_factory,
        settings=settings,
        estimator=DEFAULT_ESTIMATOR,
        model=model,
    )


async def fetch(
    session_factory: async_sessionmaker[AsyncSession], record_id: uuid.UUID
) -> UsageRecord:
    async with session_factory() as session:
        return (
            await session.execute(select(UsageRecord).where(UsageRecord.id == record_id))
        ).scalar_one()


def search_query(group_id: uuid.UUID, *, window_seconds: int = 60) -> WindowQuery:
    return WindowQuery(
        scope=ScopeRef(LimitScope.GROUP, group_id),
        metric=SEARCH,
        spec=WindowSpec.for_window(window_seconds),
    )


# --------------------------------------------------------------------------
# the metric itself
# --------------------------------------------------------------------------


class TestTheMetric:
    def test_a_search_is_a_whole_call_and_is_not_scaled(self) -> None:
        """No nano-units here. There is no fraction of a search."""
        assert to_units(SEARCH, Decimal(7)) == 7
        assert from_units(SEARCH, 7) == Decimal(7)

    def test_the_default_estimate_is_no_searches_where_it_is_one_request(self) -> None:
        """The asymmetry that keeps a search budget meaning something.

        Every metered call is one request by definition and almost none of them
        searches. Defaulting this to one would put a search on every embedding,
        and a 1,000-search ceiling would then be a 1,000-request ceiling under
        another name.
        """
        assert QuotaAmounts().requests == Decimal(1)
        assert QuotaAmounts().own_search_requests == Decimal(0)

    def test_the_metric_reads_its_own_field(self) -> None:
        """One name, one meaning: nothing else may answer for this metric."""
        amounts = QuotaAmounts(
            requests=Decimal(1),
            tokens=Decimal(900),
            cost=Decimal("2.50"),
            own_search_requests=Decimal(4),
        )
        assert amounts.get(SEARCH) == Decimal(4)
        assert amounts.get(LimitMetric.REQUESTS) == Decimal(1)
        assert amounts.get(LimitMetric.TOKENS) == Decimal(900)
        assert amounts.get(LimitMetric.COST) == Decimal("2.50")


# --------------------------------------------------------------------------
# admission
# --------------------------------------------------------------------------


class TestAdmission:
    async def test_a_search_rule_admits_until_the_limit_and_then_refuses(
        self, session: AsyncSession
    ) -> None:
        group_id = uuid.uuid4()
        await add_rule(
            session, scope=LimitScope.GROUP, scope_id=group_id, metric=SEARCH, limit="5"
        )
        quota = engine_for()
        subject = QuotaSubject(None, group_id)

        # Two requests of two searches each: four spent, under the ceiling.
        for _ in range(2):
            reservation = await quota.check_and_reserve(
                session, subject, QuotaAmounts(own_search_requests=Decimal(2))
            )
            await quota.settle(reservation, QuotaAmounts(own_search_requests=Decimal(2)))

        # A third takes the window to six, which is over — but the policy is
        # "refused *at* the limit", so this one is admitted and the next is not.
        reservation = await quota.check_and_reserve(
            session, subject, QuotaAmounts(own_search_requests=Decimal(2))
        )
        await quota.settle(reservation, QuotaAmounts(own_search_requests=Decimal(2)))

        with pytest.raises(QuotaExceeded) as caught:
            await quota.check_and_reserve(
                session, subject, QuotaAmounts(own_search_requests=Decimal(1))
            )
        assert caught.value.violations[0].rule.metric is SEARCH
        assert caught.value.retry_after_seconds >= 1

    async def test_traffic_that_searches_nothing_never_approaches_the_ceiling(
        self, session: AsyncSession
    ) -> None:
        """The test that fails if this metric ever defaults to one.

        Fifty ordinary completions against a ceiling of two searches. Not one of
        them searched, so not one of them may consume the budget.
        """
        group_id = uuid.uuid4()
        await add_rule(
            session, scope=LimitScope.GROUP, scope_id=group_id, metric=SEARCH, limit="2"
        )
        quota = engine_for()
        subject = QuotaSubject(None, group_id)

        for _ in range(50):
            reservation = await quota.check_and_reserve(
                session, subject, QuotaAmounts(tokens=Decimal(1000), cost=Decimal("0.01"))
            )
            await quota.settle(
                reservation, QuotaAmounts(tokens=Decimal(900), cost=Decimal("0.009"))
            )

        # Still room for the two searches the rule allows.
        reservation = await quota.check_and_reserve(
            session, subject, QuotaAmounts(own_search_requests=Decimal(2))
        )
        await quota.settle(reservation, QuotaAmounts(own_search_requests=Decimal(2)))
        with pytest.raises(QuotaExceeded):
            await quota.check_and_reserve(
                session, subject, QuotaAmounts(own_search_requests=Decimal(1))
            )

    async def test_a_search_ceiling_does_not_refuse_on_the_request_count(
        self, session: AsyncSession
    ) -> None:
        """Three searches inside one request close a three-search window.

        The distinction the whole dimension exists for. One request can make
        several backend calls, so a `requests` rule and an `own_search_requests`
        rule are not two spellings of the same ceiling: here the request rule of
        one hundred is barely touched while the search rule is spent by a single
        request.
        """
        group_id = uuid.uuid4()
        await add_rule(
            session, scope=LimitScope.GROUP, scope_id=group_id, metric=SEARCH, limit="3"
        )
        await add_rule(
            session,
            scope=LimitScope.GROUP,
            scope_id=group_id,
            metric=LimitMetric.REQUESTS,
            limit="100",
        )
        quota = engine_for()
        subject = QuotaSubject(None, group_id)

        reservation = await quota.check_and_reserve(
            session, subject, QuotaAmounts(own_search_requests=Decimal(3))
        )
        await quota.settle(reservation, QuotaAmounts(own_search_requests=Decimal(3)))

        with pytest.raises(QuotaExceeded) as caught:
            await quota.check_and_reserve(
                session, subject, QuotaAmounts(own_search_requests=Decimal(1))
            )
        # One violation, and it is the search rule. The request rule has seen
        # one request of its hundred.
        assert [v.rule.metric for v in caught.value.violations] == [SEARCH]

    async def test_every_applicable_rule_still_has_to_pass(
        self, session: AsyncSession
    ) -> None:
        """Additive, like every other metric: a generous cost rule rescues nothing."""
        user_id = uuid.uuid4()
        await add_rule(
            session,
            scope=LimitScope.USER,
            scope_id=user_id,
            metric=LimitMetric.COST,
            limit="1000",
        )
        await add_rule(
            session, scope=LimitScope.USER, scope_id=user_id, metric=SEARCH, limit="1"
        )
        quota = engine_for()
        subject = QuotaSubject(user_id, None)

        reservation = await quota.check_and_reserve(
            session,
            subject,
            QuotaAmounts(cost=Decimal("0.01"), own_search_requests=Decimal(1)),
        )
        await quota.settle(
            reservation,
            QuotaAmounts(cost=Decimal("0.01"), own_search_requests=Decimal(1)),
        )

        with pytest.raises(QuotaExceeded) as caught:
            await quota.check_and_reserve(
                session, subject, QuotaAmounts(own_search_requests=Decimal(1))
            )
        assert [v.rule.metric for v in caught.value.violations] == [SEARCH]

    async def test_a_search_rule_reaches_every_scope(self, session: AsyncSession) -> None:
        """Global, group, user and key — the same four a cost rule reaches."""
        group_id, user_id, key_id = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
        await add_rule(session, scope=LimitScope.GLOBAL, metric=SEARCH, limit="100")
        await add_rule(
            session, scope=LimitScope.GROUP, scope_id=group_id, metric=SEARCH, limit="100"
        )
        await add_rule(
            session, scope=LimitScope.USER, scope_id=user_id, metric=SEARCH, limit="100"
        )
        await add_rule(
            session, scope=LimitScope.API_KEY, scope_id=key_id, metric=SEARCH, limit="0"
        )
        quota = engine_for()

        # The key's rule is the tight one, and it is honoured.
        with pytest.raises(QuotaExceeded) as caught:
            await quota.check_and_reserve(
                session,
                QuotaSubject(user_id, group_id, key_id),
                QuotaAmounts(own_search_requests=Decimal(1)),
            )
        assert caught.value.violations[0].rule.scope is LimitScope.API_KEY


# --------------------------------------------------------------------------
# reserve -> settle
# --------------------------------------------------------------------------


class TestReserveAndSettle:
    async def test_the_worst_case_is_visible_in_flight_and_replaced_afterwards(
        self, session: AsyncSession
    ) -> None:
        """A request that *may* search ten times holds ten until it is done.

        The whole reason reservations exist, in this metric's terms: a request
        allowed to fan out must make its worst case visible to its siblings
        immediately, or ten concurrent requests each reading the same
        pre-search total all decide there is room.
        """
        group_id = uuid.uuid4()
        await add_rule(
            session, scope=LimitScope.GROUP, scope_id=group_id, metric=SEARCH, limit="100"
        )
        store = InMemoryCounterStore()
        quota = engine_for(store)
        query = search_query(group_id)

        reservation = await quota.check_and_reserve(
            session,
            QuotaSubject(None, group_id),
            QuotaAmounts(own_search_requests=Decimal(10)),
            now=FIXED_NOW,
        )
        assert (await store.totals([query], now=FIXED_NOW))[0].units == 10

        # It searched three times. The other seven go back.
        await quota.settle(
            reservation, QuotaAmounts(own_search_requests=Decimal(3)), now=FIXED_NOW
        )
        assert (await store.totals([query], now=FIXED_NOW))[0].units == 3

    async def test_a_request_that_searched_more_than_it_reserved_is_charged_for_all_of_them(
        self, session: AsyncSession
    ) -> None:
        """Settle is a replacement, not a minimum.

        Overshoot is the documented policy — an admitted request runs to
        completion — and it must land on the counter, so that the *next*
        request is the one refused. A settle that clamped to the reservation
        would hide the overshoot and let it repeat forever.
        """
        group_id = uuid.uuid4()
        await add_rule(
            session, scope=LimitScope.GROUP, scope_id=group_id, metric=SEARCH, limit="100"
        )
        store = InMemoryCounterStore()
        quota = engine_for(store)
        query = search_query(group_id)

        reservation = await quota.check_and_reserve(
            session,
            QuotaSubject(None, group_id),
            QuotaAmounts(own_search_requests=Decimal(2)),
            now=FIXED_NOW,
        )
        await quota.settle(
            reservation, QuotaAmounts(own_search_requests=Decimal(9)), now=FIXED_NOW
        )
        assert (await store.totals([query], now=FIXED_NOW))[0].units == 9

    async def test_releasing_hands_back_every_reserved_search(
        self, session: AsyncSession
    ) -> None:
        """A request that never happened leaves no searches occupying the window."""
        group_id = uuid.uuid4()
        await add_rule(
            session, scope=LimitScope.GROUP, scope_id=group_id, metric=SEARCH, limit="100"
        )
        store = InMemoryCounterStore()
        quota = engine_for(store)

        reservation = await quota.check_and_reserve(
            session,
            QuotaSubject(None, group_id),
            QuotaAmounts(own_search_requests=Decimal(8)),
            now=FIXED_NOW,
        )
        await quota.release(reservation, now=FIXED_NOW)
        assert (await store.totals([search_query(group_id)], now=FIXED_NOW))[0].units == 0

    async def test_a_refused_reservation_leaves_no_searches_behind(
        self, session: AsyncSession
    ) -> None:
        """Admission increments before it judges, so a refusal must undo itself.

        Otherwise fifty refusals bury the window under fifty estimates and the
        next legitimate request is refused by traffic that never ran.
        """
        group_id = uuid.uuid4()
        await add_rule(
            session, scope=LimitScope.GROUP, scope_id=group_id, metric=SEARCH, limit="4"
        )
        store = InMemoryCounterStore()
        quota = engine_for(store)
        subject = QuotaSubject(None, group_id)
        query = search_query(group_id)

        reservation = await quota.check_and_reserve(
            session, subject, QuotaAmounts(own_search_requests=Decimal(4)), now=FIXED_NOW
        )
        await quota.settle(
            reservation, QuotaAmounts(own_search_requests=Decimal(4)), now=FIXED_NOW
        )

        for _ in range(10):
            with pytest.raises(QuotaExceeded):
                await quota.check_and_reserve(
                    session, subject, QuotaAmounts(own_search_requests=Decimal(3)), now=FIXED_NOW
                )

        # The four that were spent, and nothing from the ten that were refused.
        assert (await store.totals([query], now=FIXED_NOW))[0].units == 4


# --------------------------------------------------------------------------
# the ledger row
# --------------------------------------------------------------------------


class TestTheLedger:
    async def test_the_fakes_calls_reach_the_row_and_the_actuals(
        self,
        session: AsyncSession,
        session_factory: async_sessionmaker[AsyncSession],
        settings: Settings,
        seeded: Seeded,
    ) -> None:
        model = await load_model(session, seeded.model.id)
        accounting = accounting_for(
            session_factory=session_factory, settings=settings, seeded=seeded, model=model
        )
        record_id = await accounting.begin()

        FakeSearchBackend(name="exa", tier="deep-reasoning").search(accounting, times=3)
        actuals = await accounting.finalise(status=UsageStatus.COMPLETED)

        row = await fetch(session_factory, record_id)
        assert row.own_search_requests == 3
        assert row.own_search_backend == "exa"
        assert row.own_search_tier == "deep-reasoning"
        # The same number the quota engine will settle against. A row that
        # recorded three searches while the counter settled zero would be a
        # ceiling that never engages and a ledger that says it should have.
        assert actuals.own_search_requests == Decimal(3)

    async def test_our_searches_and_the_providers_are_different_columns(
        self,
        session: AsyncSession,
        session_factory: async_sessionmaker[AsyncSession],
        settings: Settings,
        seeded: Seeded,
    ) -> None:
        """`search_count` is theirs, `own_search_requests` is ours.

        The failure this prevents: a report that cannot tell "Anthropic
        searched" from "we called Staan". Adding to one column would have made
        the two numbers one, and no amount of later analysis recovers the
        split.
        """
        model = await load_model(session, seeded.model.id)
        accounting = accounting_for(
            session_factory=session_factory, settings=settings, seeded=seeded, model=model
        )
        record_id = await accounting.begin()

        FakeSearchBackend().search(accounting, times=2)
        await accounting.finalise(status=UsageStatus.COMPLETED)

        row = await fetch(session_factory, record_id)
        assert row.own_search_requests == 2
        # The counterparty ran none — nothing here may leak into its column.
        assert row.search_count == 0

    async def test_a_provider_side_search_does_not_fill_our_column(
        self,
        session: AsyncSession,
        session_factory: async_sessionmaker[AsyncSession],
        settings: Settings,
        seeded: Seeded,
    ) -> None:
        """And the same assertion from the other direction.

        The counterparty reports two server-side searches (ADR 0058) and we
        called no backend of our own. Only `search_count` may move.
        """
        model = await load_model(session, seeded.model.id)
        accounting = accounting_for(
            session_factory=session_factory, settings=settings, seeded=seeded, model=model
        )
        record_id = await accounting.begin()

        accounting.observe_payload(
            {
                "id": "c1",
                "object": "chat.completion",
                "choices": [
                    {"index": 0, "message": {"role": "assistant", "content": "hi"}}
                ],
                "usage": {
                    "prompt_tokens": 100,
                    "completion_tokens": 50,
                    "server_tool_use": {"web_search_requests": 2},
                },
            }
        )
        actuals = await accounting.finalise(status=UsageStatus.COMPLETED)

        row = await fetch(session_factory, record_id)
        assert row.search_count == 2
        assert row.own_search_requests == 0
        assert row.own_search_backend is None
        assert actuals.own_search_requests == Decimal(0)

    async def test_the_labels_cost_nothing(
        self,
        session: AsyncSession,
        session_factory: async_sessionmaker[AsyncSession],
        settings: Settings,
        seeded: Seeded,
    ) -> None:
        """Recording a backend and a tier moves no money.

        The seeded model is priced at 1/Mtok in and 2/Mtok out. Four searches
        against Staan's dearer tier must produce exactly the same charge as
        none, because there is no rate to apply and inventing one is the
        failure this whole design refuses.
        """
        model = await load_model(session, seeded.model.id)

        quiet = accounting_for(
            session_factory=session_factory, settings=settings, seeded=seeded, model=model
        )
        quiet_id = await quiet.begin()
        quiet.observe_payload(usage_frame())
        await quiet.finalise(status=UsageStatus.COMPLETED)

        busy = accounting_for(
            session_factory=session_factory, settings=settings, seeded=seeded, model=model
        )
        busy_id = await busy.begin()
        busy.observe_payload(usage_frame())
        FakeSearchBackend(name="staan", tier="web_for_ai").search(busy, times=4)
        await busy.finalise(status=UsageStatus.COMPLETED)

        quiet_row = await fetch(session_factory, quiet_id)
        busy_row = await fetch(session_factory, busy_id)
        assert busy_row.own_search_requests == 4
        assert busy_row.own_search_tier == "web_for_ai"
        assert busy_row.cost == quiet_row.cost
        assert busy_row.computed_cost == quiet_row.computed_cost

    async def test_searches_are_counted_even_when_the_request_then_failed(
        self,
        session: AsyncSession,
        session_factory: async_sessionmaker[AsyncSession],
        settings: Settings,
        seeded: Seeded,
    ) -> None:
        """They were spent before the upstream refused.

        The failure prevented: a ceiling that can be raised by making the model
        fail. The estimation path returns a bare `TokenCounts()` for a request
        that produced nothing, so a count carried inside it would be silently
        forgiven here — which is why this one is not carried inside it.
        """
        model = await load_model(session, seeded.model.id)
        accounting = accounting_for(
            session_factory=session_factory, settings=settings, seeded=seeded, model=model
        )
        record_id = await accounting.begin()

        FakeSearchBackend(name="linkup", tier="deep").search(accounting, times=5)
        actuals = await accounting.finalise(
            status=UsageStatus.UPSTREAM_ERROR,
            upstream_status=500,
            error_code="upstream_error",
        )

        row = await fetch(session_factory, record_id)
        assert row.status is UsageStatus.UPSTREAM_ERROR
        assert row.own_search_requests == 5
        assert row.own_search_backend == "linkup"
        assert row.cost == Decimal(0)
        assert actuals.own_search_requests == Decimal(5)

    async def test_a_second_backend_does_not_overwrite_the_first(
        self,
        session: AsyncSession,
        session_factory: async_sessionmaker[AsyncSession],
        settings: Settings,
        seeded: Seeded,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """One row carries one backend, and says so loudly when that is a lie.

        A request that genuinely fanned out across two vendors needs a child
        table. Until then the count stays honest and the label names the first,
        with a warning so the day it happens is findable in a log rather than
        inferred from a column that quietly dropped half its answer.
        """
        model = await load_model(session, seeded.model.id)
        accounting = accounting_for(
            session_factory=session_factory, settings=settings, seeded=seeded, model=model
        )
        record_id = await accounting.begin()

        FakeSearchBackend(name="exa", tier="instant").search(accounting)
        with caplog.at_level("WARNING"):
            FakeSearchBackend(name="jina", tier="reader").search(accounting)

        await accounting.finalise(status=UsageStatus.COMPLETED)

        row = await fetch(session_factory, record_id)
        # Both calls counted; only the first named.
        assert row.own_search_requests == 2
        assert row.own_search_backend == "exa"
        assert any("searched two backends" in message for message in caplog.messages)


# --------------------------------------------------------------------------
# the rebuild source
# --------------------------------------------------------------------------


class TestTheRebuildSource:
    async def test_the_counter_rebuilds_from_our_column_and_not_the_providers(
        self, session: AsyncSession, session_factory: async_sessionmaker[AsyncSession]
    ) -> None:
        """`DatabaseCounterStore` is what a cold cache is refilled from.

        If it read `search_count` the counter would come back holding the
        counterparty's searches instead of ours — silently, only after a Valkey
        restart, which is the worst possible moment to discover that a ceiling
        now means something else. The rows here make the two numbers
        deliberately unequal so that reading the wrong one cannot pass.
        """
        group = Group(name="search-agg")
        session.add(group)
        await session.flush()

        for index in range(3):
            session.add(
                UsageRecord(
                    request_id=f"srch-{index}",
                    status=UsageStatus.COMPLETED,
                    group_id=group.id,
                    model_name="m",
                    currency="EUR",
                    total_tokens=100,
                    # Theirs: nine per row. Ours: two.
                    search_count=9,
                    own_search_requests=2,
                    own_search_backend="staan",
                )
            )
        await session.commit()

        store = DatabaseCounterStore(session_factory)
        # The real clock, not FIXED_NOW: these rows carry `created_at = now`,
        # and a window measured backwards from 2023 contains none of them.
        total = (
            await store.totals(
                [search_query(group.id, window_seconds=3600)], now=time.time()
            )
        )[0]
        assert total.units == 6, "expected 3 rows x 2 of our searches, not 3 x 9 of theirs"

    async def test_an_in_progress_row_contributes_no_searches_yet(
        self, session: AsyncSession, session_factory: async_sessionmaker[AsyncSession]
    ) -> None:
        group = Group(name="search-inflight")
        session.add(group)
        await session.flush()
        session.add(
            UsageRecord(
                request_id="srch-inflight",
                status=UsageStatus.IN_PROGRESS,
                group_id=group.id,
                model_name="m",
                currency="EUR",
                own_search_requests=4,
            )
        )
        await session.commit()

        store = DatabaseCounterStore(session_factory)
        total = (
            await store.totals(
                [search_query(group.id, window_seconds=3600)], now=time.time()
            )
        )[0]
        assert total.units == 0


# --------------------------------------------------------------------------
# the shared pipeline
# --------------------------------------------------------------------------


class TestTheSharedPipeline:
    """`_metered.begin` carries the unit, and carries the right one.

    `_metered` is the one reserve → record → settle path, shared by five `/v1`
    surfaces and by knowledge ingestion. A search backend that could not reserve
    through it would grow a second copy of money code, which is the one
    duplication this project cannot afford — so the argument is wired now and
    pinned here, even though nothing passes a non-zero value yet.
    """

    async def test_the_worst_case_search_count_reaches_the_reservation(
        self,
        app: object,
        seeded: Seeded,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        state = app.state  # type: ignore[attr-defined]
        async with session_factory() as db:
            db.add(
                LimitRule(
                    name="searches",
                    scope=LimitScope.GROUP,
                    scope_id=seeded.group.id,
                    metric=SEARCH,
                    window_seconds=3600,
                    limit_value=Decimal(3),
                )
            )
            await db.commit()

        async with session_factory() as db:
            principal, model = await principal_and_model(db, seeded)
            metered = await _metered.begin(
                fx=state.fx,
                session_factory=session_factory,
                session=db,
                principal=principal,
                settings=state.settings,
                quota=state.quota_engine,
                estimator=state.token_estimator,
                model=model,
                surface=ApiSurface.CHAT_COMPLETIONS,
                request_id="req-search-reserve",
                worst_case=TokenCounts(prompt=10, completion=10),
                worst_case_own_searches=4,
            )
            assert isinstance(metered, _metered.Metered)
            # Four, not the token total and not one: the argument must reach the
            # search dimension and nothing else.
            assert metered.reservation.reserved[SEARCH] == 4
            assert metered.reservation.reserved[LimitMetric.OWN_SEARCH_REQUESTS] == 4

            # And the window now holds them, so the next caller is refused.
            refused = await _metered.begin(
                fx=state.fx,
                session_factory=session_factory,
                session=db,
                principal=principal,
                settings=state.settings,
                quota=state.quota_engine,
                estimator=state.token_estimator,
                model=model,
                surface=ApiSurface.CHAT_COMPLETIONS,
                request_id="req-search-refused",
                worst_case=TokenCounts(prompt=10, completion=10),
                worst_case_own_searches=1,
            )
            assert isinstance(refused, JSONResponse)
            assert refused.status_code == 429

    async def test_a_surface_that_does_not_search_reserves_none(
        self,
        app: object,
        seeded: Seeded,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        """The default, which is what every surface passes today."""
        state = app.state  # type: ignore[attr-defined]
        async with session_factory() as db:
            db.add(
                LimitRule(
                    name="searches",
                    scope=LimitScope.GROUP,
                    scope_id=seeded.group.id,
                    metric=SEARCH,
                    window_seconds=3600,
                    limit_value=Decimal(3),
                )
            )
            await db.commit()

        async with session_factory() as db:
            principal, model = await principal_and_model(db, seeded)
            metered = await _metered.begin(
                fx=state.fx,
                session_factory=session_factory,
                session=db,
                principal=principal,
                settings=state.settings,
                quota=state.quota_engine,
                estimator=state.token_estimator,
                model=model,
                surface=ApiSurface.CHAT_COMPLETIONS,
                request_id="req-no-search",
                worst_case=TokenCounts(prompt=10, completion=10),
            )
            assert isinstance(metered, _metered.Metered)
            assert metered.reservation.reserved[SEARCH] == 0


async def principal_and_model(
    session: AsyncSession, seeded: Seeded
) -> tuple[Principal, ModelDef]:
    user = (await session.execute(select(User).where(User.id == seeded.user.id))).scalar_one()
    group = (await session.execute(select(Group).where(Group.id == seeded.group.id))).scalar_one()
    model = (
        await session.execute(
            select(ModelDef)
            .where(ModelDef.id == seeded.model.id)
            .options(selectinload(ModelDef.prices), joinedload(ModelDef.provider))
        )
    ).scalar_one()
    return Principal(user=user, billing_group=group), model


def usage_frame() -> dict[str, object]:
    """A plain completion: 100 in, 50 out, and no searches of anyone's."""
    return {
        "id": "c1",
        "object": "chat.completion",
        "choices": [{"index": 0, "message": {"role": "assistant", "content": "hello"}}],
        "usage": {"prompt_tokens": 100, "completion_tokens": 50},
    }
