# ruff: noqa: S608  (fixed SQL built from test constants)
"""Quota rules limited to one provider or one model.

A rule may name a provider and/or a model. It then counts, and refuses, only the
requests that provider (or model) serves: "OpenAI at most EUR 500 a month" for the
deployment, "each user at most EUR 20 a month on Anthropic", a group's or a key's
share of one provider. Pinned here:

- matching: an unfiltered rule applies to everything, a filtered one only to a
  request whose target matches, and to none when no target is given;
- counting: a filtered rule has its own counter; the ledger side sums only rows
  whose **served** model belongs to the provider (so a request that arrived as an
  alias counts for the target's provider), never an own-key (BYOK) row;
- refusal: a 429 that names the provider or model and when it resets, and no
  failover to another provider even through an alias with several targets;
- the admin API validates the filter and shows it, and a person's own limits list
  shows the provider-scoped rules that apply to them;
- deleting a model or provider that a rule names is refused with a 409 (RESTRICT),
  so a cap never vanishes or widens, search backends included.
"""

from __future__ import annotations

import sqlite3
import uuid
from contextlib import closing
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

import httpx
import pytest
from conftest import UPSTREAM_BASE, FakeUpstream, Seeded
from gateway.models import (
    GroupModelAccess,
    LimitAppliesTo,
    LimitMetric,
    LimitRule,
    LimitScope,
    ModelDef,
    ModelPrice,
    ModelRouteTarget,
    Provider,
    UsageRecord,
    UsageStatus,
)
from gateway.periods import PeriodKind, period_containing
from gateway.quota import (
    DatabaseCounterStore,
    InMemoryCounterStore,
    QuotaAmounts,
    QuotaExceeded,
    QuotaSubject,
    QuotaTarget,
    ScopeRef,
    WindowQuery,
)
from gateway.quota.engine import QuotaEngine, rule_matches_target
from gateway.secrets import SecretBox
from helpers import completion_body
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from test_chat_completions import basic_request
from test_migration_0067 import _alembic
from test_quota_engine import engine_for
from test_quota_per_user import make_user


async def make_provider(session: AsyncSession, name: str) -> Provider:
    box = SecretBox(["test-encryption-key-not-for-production"])
    provider = Provider(
        name=name,
        base_url=UPSTREAM_BASE,
        api_key_encrypted=box.encrypt("k"),
        api_key_hint="k",
    )
    session.add(provider)
    await session.flush()
    return provider


async def make_model(
    session: AsyncSession, provider: Provider, name: str, *, priced: bool = True
) -> ModelDef:
    model = ModelDef(
        name=name, upstream_model=f"up/{name}", provider_id=provider.id, context_window=8192
    )
    session.add(model)
    await session.flush()
    if priced:
        session.add(
            ModelPrice(
                model_id=model.id,
                input_per_mtok=Decimal("1"),
                output_per_mtok=Decimal("2"),
                currency="EUR",
            )
        )
    return model


async def add_filtered_rule(
    session: AsyncSession,
    *,
    provider: Provider | None = None,
    model: ModelDef | None = None,
    scope: LimitScope = LimitScope.GLOBAL,
    scope_id: uuid.UUID | None = None,
    applies_to: LimitAppliesTo = LimitAppliesTo.AGGREGATE,
    metric: LimitMetric = LimitMetric.REQUESTS,
    limit: str = "2",
    period: PeriodKind | None = None,
    name: str = "provider cap",
    overrides: bool = False,
) -> LimitRule:
    rule = LimitRule(
        name=name,
        scope=scope,
        scope_id=scope_id,
        metric=metric,
        window_seconds=None if period else 3600,
        period=period,
        limit_value=Decimal(limit),
        applies_to=applies_to,
        provider_id=provider.id if provider else None,
        model_id=model.id if model else None,
        overrides_each_user=overrides,
    )
    session.add(rule)
    await session.commit()
    await session.refresh(rule, attribute_names=["resets"])
    return rule


def target_of(model: ModelDef) -> QuotaTarget:
    return QuotaTarget(provider_id=model.provider_id, model_id=model.id)


async def one(
    quota: QuotaEngine,
    session: AsyncSession,
    subject: QuotaSubject,
    model: ModelDef,
    **amounts: Decimal,
) -> None:
    reservation = await quota.check_and_reserve(
        session, subject, QuotaAmounts(**amounts), target=target_of(model)
    )
    await quota.settle(reservation, QuotaAmounts(**amounts))


async def ledger_row(
    session: AsyncSession,
    *,
    model: ModelDef | None,
    user_id: uuid.UUID,
    cost: str = "1",
    byok: bool = False,
    requested_model: str | None = None,
    provider_id: uuid.UUID | None = None,
) -> None:
    session.add(
        UsageRecord(
            request_id=f"r-{uuid.uuid4().hex[:8]}",
            status=UsageStatus.COMPLETED,
            user_id=user_id,
            model_id=model.id if model else None,
            # What the recorder writes: the provider that served the row.
            provider_id=provider_id or (model.provider_id if model else None),
            model_name=model.name if model else "gone",
            requested_model=requested_model,
            currency="EUR",
            total_tokens=10,
            cost=Decimal(cost),
            byok=byok,
            created_at=datetime.now(UTC),
        )
    )
    await session.commit()


@pytest.fixture
async def two_providers(session: AsyncSession) -> tuple[ModelDef, ModelDef]:
    """A model each on two providers, named for the story."""
    anthropic = await make_provider(session, "Anthropic")
    openai = await make_provider(session, "OpenAI")
    claude = await make_model(session, anthropic, "claude")
    gpt = await make_model(session, openai, "gpt")
    await session.commit()
    return claude, gpt


class TestMatching:
    async def test_the_filter_decides_which_requests_a_rule_applies_to(
        self, session: AsyncSession, two_providers: tuple[ModelDef, ModelDef]
    ) -> None:
        claude, gpt = two_providers
        everything = LimitRule()
        by_provider = LimitRule(provider_id=claude.provider_id)
        by_model = LimitRule(provider_id=claude.provider_id, model_id=claude.id)
        other_model = LimitRule(model_id=uuid.uuid4())

        for target in (target_of(claude), target_of(gpt), None):
            assert rule_matches_target(everything, target)
        assert rule_matches_target(by_provider, target_of(claude))
        assert not rule_matches_target(by_provider, target_of(gpt))
        assert not rule_matches_target(by_provider, None)
        assert rule_matches_target(by_model, target_of(claude))
        assert not rule_matches_target(other_model, target_of(claude))

    async def test_a_provider_rule_never_sees_another_providers_traffic(
        self, session: AsyncSession, two_providers: tuple[ModelDef, ModelDef]
    ) -> None:
        claude, gpt = two_providers
        user = await make_user(session)
        provider = (
            await session.execute(select(Provider).where(Provider.id == claude.provider_id))
        ).scalar_one()
        await add_filtered_rule(session, provider=provider, limit="2")
        quota = engine_for()
        subject = QuotaSubject(user.id, None)

        # Plenty of traffic on the other provider is invisible to the rule.
        for _ in range(5):
            await one(quota, session, subject, gpt)
        await one(quota, session, subject, claude)
        await one(quota, session, subject, claude)
        with pytest.raises(QuotaExceeded):
            await quota.check_and_reserve(
                session, subject, QuotaAmounts(), target=target_of(claude)
            )
        # The other provider is still open: the rule is not a global ceiling.
        reservation = await quota.check_and_reserve(
            session, subject, QuotaAmounts(), target=target_of(gpt)
        )
        assert reservation.is_empty or reservation.deltas == []

    async def test_a_model_rule_is_narrower_than_its_provider(self, session: AsyncSession) -> None:
        provider = await make_provider(session, "Anthropic")
        opus = await make_model(session, provider, "opus")
        haiku = await make_model(session, provider, "haiku")
        await session.commit()
        user = await make_user(session)
        await add_filtered_rule(session, model=opus, limit="1")
        quota = engine_for()
        subject = QuotaSubject(user.id, None)

        await one(quota, session, subject, opus)
        with pytest.raises(QuotaExceeded):
            await quota.check_and_reserve(session, subject, QuotaAmounts(), target=target_of(opus))
        await quota.check_and_reserve(session, subject, QuotaAmounts(), target=target_of(haiku))

    async def test_without_a_target_a_filtered_rule_is_not_evaluated(
        self, session: AsyncSession, two_providers: tuple[ModelDef, ModelDef]
    ) -> None:
        """Zero refuses everything the rule applies to, so passing proves it was skipped."""
        claude, _ = two_providers
        provider = (
            await session.execute(select(Provider).where(Provider.id == claude.provider_id))
        ).scalar_one()
        await add_filtered_rule(session, provider=provider, limit="0")
        user = await make_user(session)
        reservation = await engine_for().check_and_reserve(
            session, QuotaSubject(user.id, None), QuotaAmounts()
        )
        assert reservation.is_empty

    async def test_a_provider_rule_and_a_rule_on_everything_keep_separate_counters(
        self, session: AsyncSession, two_providers: tuple[ModelDef, ModelDef]
    ) -> None:
        """Same scope, metric and window, so only the filter tells the counters
        apart: traffic on the other provider must not use up the provider's budget."""
        claude, gpt = two_providers
        provider = (
            await session.execute(select(Provider).where(Provider.id == claude.provider_id))
        ).scalar_one()
        await add_filtered_rule(session, limit="100", name="everything")
        await add_filtered_rule(session, provider=provider, limit="2")
        user = await make_user(session)
        quota = engine_for()
        subject = QuotaSubject(user.id, None)

        for _ in range(3):
            await one(quota, session, subject, gpt)
        reservation = await quota.check_and_reserve(
            session, subject, QuotaAmounts(), target=target_of(claude)
        )
        assert not reservation.is_empty

    async def test_the_filter_is_part_of_the_counter_key(self) -> None:
        user = uuid.uuid4()
        plain = ScopeRef(LimitScope.USER, user)
        filtered = ScopeRef(LimitScope.USER, user, provider_id=uuid.uuid4())
        by_model = ScopeRef(LimitScope.USER, user, model_id=uuid.uuid4())
        assert len({plain.key_part, filtered.key_part, by_model.key_part}) == 3


class TestEachUserPerProvider:
    async def test_each_user_gets_a_budget_on_the_provider(
        self, session: AsyncSession, two_providers: tuple[ModelDef, ModelDef]
    ) -> None:
        claude, gpt = two_providers
        provider = (
            await session.execute(select(Provider).where(Provider.id == claude.provider_id))
        ).scalar_one()
        await add_filtered_rule(
            session, provider=provider, applies_to=LimitAppliesTo.EACH_USER, limit="2"
        )
        first, second = await make_user(session), await make_user(session)
        quota = engine_for()

        for _ in range(2):
            await one(quota, session, QuotaSubject(first.id, None), claude)
        with pytest.raises(QuotaExceeded):
            await quota.check_and_reserve(
                session, QuotaSubject(first.id, None), QuotaAmounts(), target=target_of(claude)
            )
        # Their spend elsewhere is free, and so is everyone else's here.
        await quota.check_and_reserve(
            session, QuotaSubject(first.id, None), QuotaAmounts(), target=target_of(gpt)
        )
        await quota.check_and_reserve(
            session, QuotaSubject(second.id, None), QuotaAmounts(), target=target_of(claude)
        )

    async def test_an_unfiltered_override_does_not_replace_a_provider_ceiling(
        self, session: AsyncSession, two_providers: tuple[ModelDef, ModelDef]
    ) -> None:
        claude, _ = two_providers
        provider = (
            await session.execute(select(Provider).where(Provider.id == claude.provider_id))
        ).scalar_one()
        user = await make_user(session)
        each_user = await add_filtered_rule(
            session, provider=provider, applies_to=LimitAppliesTo.EACH_USER, name="per provider"
        )
        override = await add_filtered_rule(
            session,
            scope=LimitScope.USER,
            scope_id=user.id,
            overrides=True,
            limit="50",
            name="override",
        )
        kept = QuotaEngine.drop_overridden([each_user, override])
        assert each_user in kept
        # The override of the *same* filter does replace it.
        same = await add_filtered_rule(
            session,
            provider=provider,
            scope=LimitScope.USER,
            scope_id=user.id,
            overrides=True,
            limit="50",
            name="same filter",
        )
        assert each_user not in QuotaEngine.drop_overridden([each_user, same])


class TestLedgerSums:
    async def _total(
        self, session_factory: async_sessionmaker[AsyncSession], scope: ScopeRef
    ) -> int:
        month = period_containing(PeriodKind.MONTH, datetime.now(UTC), timezone="Europe/Rome")
        (total,) = await DatabaseCounterStore(session_factory).totals(
            [WindowQuery(scope=scope, metric=LimitMetric.REQUESTS, period=month)],
            now=datetime.now(UTC).timestamp(),
        )
        return total.units

    async def test_only_rows_served_by_the_provider_count(
        self,
        session: AsyncSession,
        session_factory: async_sessionmaker[AsyncSession],
        two_providers: tuple[ModelDef, ModelDef],
    ) -> None:
        claude, gpt = two_providers
        user = await make_user(session)
        await ledger_row(session, model=claude, user_id=user.id)
        await ledger_row(session, model=claude, user_id=user.id)
        await ledger_row(session, model=gpt, user_id=user.id)
        # A request that arrived as an alias is stored under the model that served it.
        await ledger_row(session, model=claude, user_id=user.id, requested_model="fast")
        # A row whose model was deleted still counts for the provider that served
        # it (the row carries it), and for no model.
        await ledger_row(session, model=None, user_id=user.id, provider_id=claude.provider_id)
        # Own-key usage is in no quota.
        await ledger_row(session, model=claude, user_id=user.id, byok=True)

        everything = await self._total(session_factory, ScopeRef(LimitScope.GLOBAL))
        on_claude = await self._total(
            session_factory, ScopeRef(LimitScope.GLOBAL, provider_id=claude.provider_id)
        )
        on_gpt_model = await self._total(
            session_factory, ScopeRef(LimitScope.GLOBAL, model_id=gpt.id)
        )
        on_claude_model = await self._total(
            session_factory, ScopeRef(LimitScope.GLOBAL, model_id=claude.id)
        )
        assert (everything, on_claude, on_gpt_model, on_claude_model) == (5, 4, 1, 3)

    async def test_the_grouped_per_user_query_applies_the_filter_too(
        self,
        session: AsyncSession,
        session_factory: async_sessionmaker[AsyncSession],
        two_providers: tuple[ModelDef, ModelDef],
    ) -> None:
        claude, gpt = two_providers
        first, second = await make_user(session), await make_user(session)
        await ledger_row(session, model=claude, user_id=first.id)
        await ledger_row(session, model=gpt, user_id=first.id)
        await ledger_row(session, model=gpt, user_id=second.id)
        month = period_containing(PeriodKind.MONTH, datetime.now(UTC), timezone="Europe/Rome")
        queries = [
            WindowQuery(
                scope=ScopeRef(LimitScope.USER, user.id, provider_id=claude.provider_id),
                metric=LimitMetric.REQUESTS,
                period=month,
            )
            for user in (first, second)
        ]
        totals = await DatabaseCounterStore(session_factory).totals(
            queries, now=datetime.now(UTC).timestamp()
        )
        assert [t.units for t in totals] == [1, 0]

    async def test_seeding_a_new_provider_rule_starts_from_the_ledger_on_that_provider(
        self,
        session: AsyncSession,
        session_factory: async_sessionmaker[AsyncSession],
        two_providers: tuple[ModelDef, ModelDef],
    ) -> None:
        claude, gpt = two_providers
        provider = (
            await session.execute(select(Provider).where(Provider.id == claude.provider_id))
        ).scalar_one()
        user = await make_user(session)
        for model in (claude, claude, gpt, gpt, gpt):
            await ledger_row(session, model=model, user_id=user.id)
        rule = await add_filtered_rule(
            session, provider=provider, period=PeriodKind.MONTH, limit="2"
        )
        quota = engine_for(InMemoryCounterStore(), fallback=DatabaseCounterStore(session_factory))
        assert await quota.seed_rule(session, rule)
        # The two on claude fill it; the three on gpt are not its business.
        with pytest.raises(QuotaExceeded):
            await quota.check_and_reserve(
                session, QuotaSubject(user.id, None), QuotaAmounts(), target=target_of(claude)
            )
        await quota.check_and_reserve(
            session, QuotaSubject(user.id, None), QuotaAmounts(), target=target_of(gpt)
        )


class TestRefusalMessage:
    async def test_it_names_the_provider_and_when_it_resets(
        self, session: AsyncSession, two_providers: tuple[ModelDef, ModelDef]
    ) -> None:
        claude, _ = two_providers
        provider = (
            await session.execute(select(Provider).where(Provider.id == claude.provider_id))
        ).scalar_one()
        await add_filtered_rule(
            session,
            provider=provider,
            applies_to=LimitAppliesTo.EACH_USER,
            metric=LimitMetric.COST,
            limit="0",
            period=PeriodKind.MONTH,
        )
        user = await make_user(session)
        with pytest.raises(QuotaExceeded) as caught:
            await engine_for().check_and_reserve(
                session, QuotaSubject(user.id, None), QuotaAmounts(), target=target_of(claude)
            )
        message = str(caught.value)
        assert message.endswith(
            "per user limit of 0 cost per month on Anthropic exceeded (current 0)"
        )
        assert caught.value.names_provider_or_model

    async def test_a_model_rule_says_model(self, session: AsyncSession) -> None:
        provider = await make_provider(session, "Anthropic")
        opus = await make_model(session, provider, "opus")
        await session.commit()
        await add_filtered_rule(session, model=opus, limit="0")
        user = await make_user(session)
        with pytest.raises(QuotaExceeded) as caught:
            await engine_for().check_and_reserve(
                session, QuotaSubject(user.id, None), QuotaAmounts(), target=target_of(opus)
            )
        assert "per 1 hour on opus exceeded" in str(caught.value)


class TestOverHttp:
    async def test_a_request_to_the_limited_provider_is_refused_naming_it(
        self,
        client: httpx.AsyncClient,
        seeded: Seeded,
        fake_upstream: FakeUpstream,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        async with session_factory() as db:
            provider = (
                await db.execute(select(Provider).where(Provider.id == seeded.provider.id))
            ).scalar_one()
            await add_filtered_rule(db, provider=provider, limit="0", name="cap")
        fake_upstream.set_json(completion_body())
        response = await client.post(
            "/v1/chat/completions", json=basic_request(), headers=seeded.auth
        )
        assert response.status_code == 429
        error = response.json()["error"]
        assert error["code"] == "quota_exceeded"
        assert "per 1 hour on fake exceeded (current 0)" in error["message"]
        assert int(response.headers["retry-after"]) > 0

    async def test_a_rule_on_another_provider_leaves_the_request_alone(
        self,
        client: httpx.AsyncClient,
        seeded: Seeded,
        fake_upstream: FakeUpstream,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        async with session_factory() as db:
            other = await make_provider(db, "elsewhere")
            await db.commit()
            await add_filtered_rule(db, provider=other, limit="0")
        fake_upstream.set_json(completion_body())
        response = await client.post(
            "/v1/chat/completions", json=basic_request(), headers=seeded.auth
        )
        assert response.status_code == 200, response.text

    async def test_an_alias_is_checked_against_the_target_and_never_falls_back(
        self,
        client: httpx.AsyncClient,
        seeded: Seeded,
        fake_upstream: FakeUpstream,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        """Alias `fast` -> [a model on `fake`, a model on `backup`]. The first target's
        provider is at its limit: the caller is refused, not moved to `backup`."""
        calls: list[str] = []

        def respond(request: httpx.Request) -> httpx.Response:
            calls.append(str(request.url))
            return httpx.Response(200, json=completion_body())

        fake_upstream._responder = respond
        async with session_factory() as db:
            first = await make_model(db, seeded.provider, "t-first")
            backup = await make_provider(db, "backup")
            second = await make_model(db, backup, "t-second")
            alias = ModelDef(
                name="fast",
                provider_id=None,
                upstream_model=None,
                is_alias=True,
                is_active=True,
                route_rule="first_available",
            )
            db.add(alias)
            await db.flush()
            for position, target in enumerate((first, second)):
                db.add(
                    ModelRouteTarget(
                        alias_id=alias.id,
                        target_model_id=target.id,
                        position=position,
                        weight=1,
                    )
                )
            db.add(GroupModelAccess(group_id=seeded.group.id, model_id=alias.id))
            await db.commit()
            provider = (
                await db.execute(select(Provider).where(Provider.id == seeded.provider.id))
            ).scalar_one()
            await add_filtered_rule(db, provider=provider, limit="0")

        response = await client.post(
            "/v1/chat/completions", json=basic_request(model="fast"), headers=seeded.auth
        )
        assert response.status_code == 429, response.text
        assert "on fake exceeded" in response.json()["error"]["message"]
        assert calls == []
        async with session_factory() as db:
            assert (await db.execute(select(UsageRecord))).first() is None

    async def test_an_unfiltered_refusal_on_an_alias_still_fails_over(
        self,
        client: httpx.AsyncClient,
        seeded: Seeded,
        fake_upstream: FakeUpstream,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        """The no-fallback rule is for provider and model limits only: a limit with
        no filter keeps today's alias behaviour (covered by the routing tests), and a
        limit on the *second* target's provider does not touch the first."""
        fake_upstream.set_json(completion_body())
        async with session_factory() as db:
            first = await make_model(db, seeded.provider, "t-first")
            backup = await make_provider(db, "backup")
            await make_model(db, backup, "t-second")
            alias = ModelDef(
                name="fast2",
                provider_id=None,
                upstream_model=None,
                is_alias=True,
                is_active=True,
                route_rule="first_available",
            )
            db.add(alias)
            await db.flush()
            db.add(
                ModelRouteTarget(alias_id=alias.id, target_model_id=first.id, position=0, weight=1)
            )
            db.add(GroupModelAccess(group_id=seeded.group.id, model_id=alias.id))
            await db.commit()
            await add_filtered_rule(db, provider=backup, limit="0")
        response = await client.post(
            "/v1/chat/completions", json=basic_request(model="fast2"), headers=seeded.auth
        )
        assert response.status_code == 200, response.text

    async def test_a_cap_on_a_later_target_does_not_end_the_alias(
        self,
        client: httpx.AsyncClient,
        seeded: Seeded,
        fake_upstream: FakeUpstream,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        """Alias -> [t-first (upstream 500), t-second on `backup` (at its limit),
        t-third on `fake`]. The no-fallback rule is for the first attempt: once the
        first target failed upstream the request has already moved on, so the capped
        second target is skipped and the third serves it."""
        fake_upstream.set_json(completion_body())
        calls: list[str] = []

        def respond(request: httpx.Request) -> httpx.Response:
            calls.append(str(request.url))
            if len(calls) == 1:
                return httpx.Response(500, json={"error": {"message": "boom"}})
            return httpx.Response(200, json=completion_body())

        fake_upstream._responder = respond
        async with session_factory() as db:
            first = await make_model(db, seeded.provider, "t-first")
            backup = await make_provider(db, "backup")
            second = await make_model(db, backup, "t-second")
            third = await make_model(db, seeded.provider, "t-third")
            alias = ModelDef(
                name="fast3",
                provider_id=None,
                upstream_model=None,
                is_alias=True,
                is_active=True,
                route_rule="first_available",
            )
            db.add(alias)
            await db.flush()
            for position, target in enumerate((first, second, third)):
                db.add(
                    ModelRouteTarget(
                        alias_id=alias.id,
                        target_model_id=target.id,
                        position=position,
                        weight=1,
                    )
                )
            db.add(GroupModelAccess(group_id=seeded.group.id, model_id=alias.id))
            await db.commit()
            await add_filtered_rule(db, provider=backup, limit="0")
        response = await client.post(
            "/v1/chat/completions", json=basic_request(model="fast3"), headers=seeded.auth
        )
        assert response.status_code == 200, response.text
        assert len(calls) == 2


class TestAdminApi:
    async def _admin(
        self, app: Any, session_factory: async_sessionmaker[AsyncSession], seeded: Seeded
    ) -> None:
        from test_admin import as_user, make_admin

        as_user(app, await make_admin(session_factory, seeded))

    def _body(self, **extra: Any) -> dict[str, Any]:
        return {
            "name": "cap",
            "scope": "global",
            "metric": "cost",
            "period": "month",
            "limit_value": "20",
            "applies_to": "each_user",
            **extra,
        }

    async def test_create_list_and_show_the_filter(
        self,
        app: Any,
        client: httpx.AsyncClient,
        seeded: Seeded,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        await self._admin(app, session_factory, seeded)
        created = await client.post(
            "/api/admin/limits",
            json=self._body(provider_id=str(seeded.provider.id), model_id=str(seeded.model.id)),
        )
        assert created.status_code == 201, created.text
        body = created.json()
        # The model alone is stored; the provider shown is its model's.
        assert body["provider_id"] == str(seeded.provider.id)
        assert body["provider_name"] == "fake"
        assert body["model_name"] == "test-model"
        async with session_factory() as db:
            stored = (await db.execute(select(LimitRule))).scalars().one()
            assert stored.provider_id is None
            assert stored.model_id == seeded.model.id

        listed = (await client.get("/api/admin/limits")).json()["items"]
        (row,) = [r for r in listed if r["name"] == "cap"]
        assert row["provider_name"] == "fake"
        assert row["model_id"] == str(seeded.model.id)

    async def test_the_same_rule_per_provider_is_allowed_and_twice_is_a_conflict(
        self,
        app: Any,
        client: httpx.AsyncClient,
        seeded: Seeded,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        await self._admin(app, session_factory, seeded)
        async with session_factory() as db:
            other = await make_provider(db, "elsewhere")
            await db.commit()
            other_id = str(other.id)
        mine = self._body(provider_id=str(seeded.provider.id))
        assert (await client.post("/api/admin/limits", json=mine)).status_code == 201
        assert (await client.post("/api/admin/limits", json=self._body())).status_code == 201
        assert (
            await client.post("/api/admin/limits", json=self._body(provider_id=other_id))
        ).status_code == 201
        assert (await client.post("/api/admin/limits", json=mine)).status_code == 409

    async def test_validation(
        self,
        app: Any,
        client: httpx.AsyncClient,
        seeded: Seeded,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        await self._admin(app, session_factory, seeded)
        async with session_factory() as db:
            other = await make_provider(db, "elsewhere")
            alias = ModelDef(
                name="some-alias", provider_id=None, upstream_model=None, is_alias=True
            )
            db.add(alias)
            await db.commit()
            other_id, alias_id = str(other.id), str(alias.id)

        missing = str(uuid.uuid4())
        post = client.post
        # A model must belong to the provider named with it.
        r = await post(
            "/api/admin/limits",
            json=self._body(provider_id=other_id, model_id=str(seeded.model.id)),
        )
        assert r.status_code == 400
        assert "does not belong" in r.json()["error"]["message"]
        # An alias serves nothing itself.
        r = await post("/api/admin/limits", json=self._body(model_id=alias_id))
        assert r.status_code == 400
        assert "alias" in r.json()["error"]["message"]
        for field in ("provider_id", "model_id"):
            r = await post("/api/admin/limits", json=self._body(**{field: missing}))
            assert r.status_code == 404

    async def test_a_members_own_provider_cannot_be_named(
        self,
        app: Any,
        client: httpx.AsyncClient,
        seeded: Seeded,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        """Own-key usage is in no quota, so a rule on it would never count."""
        await self._admin(app, session_factory, seeded)
        async with session_factory() as db:
            mine = await make_provider(db, "mine")
            mine.owner_kind = "user"
            mine.owner_id = seeded.user.id
            await db.commit()
            mine_id = str(mine.id)
        r = await client.post("/api/admin/limits", json=self._body(provider_id=mine_id))
        assert r.status_code == 400
        assert "own provider" in r.json()["error"]["message"]

    async def test_a_persons_limits_list_shows_provider_scoped_rules(
        self,
        app: Any,
        client: httpx.AsyncClient,
        seeded: Seeded,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        from test_my_limits import session_cookie

        async with session_factory() as db:
            provider = (
                await db.execute(select(Provider).where(Provider.id == seeded.provider.id))
            ).scalar_one()
            await add_filtered_rule(
                db,
                provider=provider,
                applies_to=LimitAppliesTo.EACH_USER,
                name="each user on fake",
                limit="100",
            )
        response = await client.get("/api/me/limits", headers=session_cookie(seeded.user.id, app))
        assert response.status_code == 200, response.text
        (entry,) = [r for r in response.json()["items"] if r["name"] == "each user on fake"]
        assert entry["provider_name"] == "fake"
        assert entry["model_name"] is None
        assert Decimal(entry["current_value"]) == 0


class TestDeletion:
    async def test_the_database_refuses_to_delete_what_a_rule_names(
        self, session: AsyncSession
    ) -> None:
        """RESTRICT: a cost cap never goes with the model, and is never widened."""
        from sqlalchemy.exc import IntegrityError

        provider = await make_provider(session, "Anthropic")
        opus = await make_model(session, provider, "opus")
        await session.commit()
        await add_filtered_rule(session, model=opus, name="opus cap")
        with pytest.raises(IntegrityError):
            await session.delete(opus)
            await session.commit()
        await session.rollback()
        assert list((await session.execute(select(LimitRule.name))).scalars().all()) == ["opus cap"]

    async def test_the_admin_api_names_the_rules_in_a_409(
        self,
        app: Any,
        client: httpx.AsyncClient,
        seeded: Seeded,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        from test_admin import as_user, make_admin

        as_user(app, await make_admin(session_factory, seeded))
        async with session_factory() as db:
            provider = (
                await db.execute(select(Provider).where(Provider.id == seeded.provider.id))
            ).scalar_one()
            model_rule = await add_filtered_rule(db, model=seeded.model, name="model cap")
            provider_rule = await add_filtered_rule(
                db, provider=provider, metric=LimitMetric.TOKENS, name="provider cap"
            )

        refused = await client.delete(f"/api/admin/models/{seeded.model.id}")
        assert refused.status_code == 409
        assert "'model cap'" in refused.json()["error"]["message"]
        refused = await client.delete(f"/api/admin/providers/{seeded.provider.id}")
        assert refused.status_code == 409
        assert "'provider cap'" in refused.json()["error"]["message"]

        # With the rules gone the model can go, and the rows it served keep their
        # provider.
        async with session_factory() as db:
            await db.delete(await db.get(LimitRule, model_rule.id))
            await db.delete(await db.get(LimitRule, provider_rule.id))
            await db.commit()
        assert (await client.delete(f"/api/admin/models/{seeded.model.id}")).status_code == 204

    async def test_a_search_backend_with_a_rule_is_refused_not_a_500(
        self,
        app: Any,
        client: httpx.AsyncClient,
        seeded: Seeded,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        """A provider re-kinded to search, or a model repointed onto a backend, keeps
        its rule; the total delete must answer 409, not trip the RESTRICT FK."""
        from gateway.models import ModelKind, ProviderKind
        from test_admin import as_user, make_admin

        as_user(app, await make_admin(session_factory, seeded))
        async with session_factory() as db:
            by_provider = await make_provider(db, "exa-a")
            by_provider.kind = ProviderKind.SEARCH
            await make_model(db, by_provider, "a-tier", priced=False)
            by_model = await make_provider(db, "exa-b")
            by_model.kind = ProviderKind.SEARCH
            tier = await make_model(db, by_model, "b-tier", priced=False)
            tier.kind = ModelKind.SEARCH
            await db.commit()
            await add_filtered_rule(db, provider=by_provider, name="rekinded cap")
            await add_filtered_rule(db, model=tier, name="tier cap")
            first, second = by_provider.id, by_model.id
        refused = await client.delete(f"/api/admin/providers/{first}")
        assert refused.status_code == 409, refused.text
        assert "'rekinded cap'" in refused.json()["error"]["message"]
        refused = await client.delete(f"/api/admin/providers/{second}")
        assert refused.status_code == 409, refused.text
        assert "'tier cap'" in refused.json()["error"]["message"]


class TestHistoryIsTheRowsOwn:
    """A provider's total is summed from the provider each row recorded, so the
    catalogue changing afterwards cannot move or lose spend (money)."""

    async def _provider_total(
        self, session_factory: async_sessionmaker[AsyncSession], provider_id: uuid.UUID
    ) -> int:
        month = period_containing(PeriodKind.MONTH, datetime.now(UTC), timezone="Europe/Rome")
        (total,) = await DatabaseCounterStore(session_factory).totals(
            [
                WindowQuery(
                    scope=ScopeRef(LimitScope.GLOBAL, provider_id=provider_id),
                    metric=LimitMetric.REQUESTS,
                    period=month,
                )
            ],
            now=datetime.now(UTC).timestamp(),
        )
        return total.units

    async def test_repointing_a_model_does_not_move_its_history(
        self,
        session: AsyncSession,
        session_factory: async_sessionmaker[AsyncSession],
        two_providers: tuple[ModelDef, ModelDef],
    ) -> None:
        claude, gpt = two_providers
        user = await make_user(session)
        original = claude.provider_id
        for _ in range(2):
            await ledger_row(session, model=claude, user_id=user.id)
        claude.provider_id = gpt.provider_id
        await session.commit()
        assert await self._provider_total(session_factory, original) == 2
        assert await self._provider_total(session_factory, gpt.provider_id) == 0

    async def test_deleting_a_model_does_not_lose_its_history(
        self,
        session: AsyncSession,
        session_factory: async_sessionmaker[AsyncSession],
        two_providers: tuple[ModelDef, ModelDef],
    ) -> None:
        claude, _ = two_providers
        user = await make_user(session)
        provider_id = claude.provider_id
        for _ in range(3):
            await ledger_row(session, model=claude, user_id=user.id)
        await session.delete(claude)
        await session.commit()
        assert await self._provider_total(session_factory, provider_id) == 3

    async def test_a_provider_rule_keeps_refusing_after_its_model_is_deleted(
        self,
        session: AsyncSession,
        session_factory: async_sessionmaker[AsyncSession],
        two_providers: tuple[ModelDef, ModelDef],
    ) -> None:
        """The cap that matters: seed from the ledger after the delete, still full."""
        claude, _ = two_providers
        provider = (
            await session.execute(select(Provider).where(Provider.id == claude.provider_id))
        ).scalar_one()
        user = await make_user(session)
        for _ in range(2):
            await ledger_row(session, model=claude, user_id=user.id)
        await session.delete(claude)
        await session.commit()
        sibling = await make_model(session, provider, "sibling")
        await session.commit()
        rule = await add_filtered_rule(
            session, provider=provider, period=PeriodKind.MONTH, limit="2"
        )
        quota = engine_for(InMemoryCounterStore(), fallback=DatabaseCounterStore(session_factory))
        assert await quota.seed_rule(session, rule)
        with pytest.raises(QuotaExceeded):
            await quota.check_and_reserve(
                session, QuotaSubject(user.id, None), QuotaAmounts(), target=target_of(sibling)
            )

    async def test_a_model_rule_survives_the_model_being_repointed(
        self, session: AsyncSession, two_providers: tuple[ModelDef, ModelDef]
    ) -> None:
        claude, gpt = two_providers
        user = await make_user(session)
        await add_filtered_rule(session, model=claude, limit="1")
        quota = engine_for()
        subject = QuotaSubject(user.id, None)
        await one(quota, session, subject, claude)
        claude.provider_id = gpt.provider_id
        await session.commit()
        with pytest.raises(QuotaExceeded):
            await quota.check_and_reserve(
                session, subject, QuotaAmounts(), target=target_of(claude)
            )

    async def test_requests_record_the_provider_that_served_them(
        self,
        client: httpx.AsyncClient,
        seeded: Seeded,
        fake_upstream: FakeUpstream,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        fake_upstream.set_json(completion_body())
        response = await client.post(
            "/v1/chat/completions", json=basic_request(), headers=seeded.auth
        )
        assert response.status_code == 200, response.text
        async with session_factory() as db:
            row = (await db.execute(select(UsageRecord))).scalars().one()
        assert row.provider_id == seeded.provider.id
        assert row.model_id == seeded.model.id


class TestCountersMoveTogether:
    async def test_settle_and_release_move_every_counter_a_request_touches(
        self, session: AsyncSession, two_providers: tuple[ModelDef, ModelDef]
    ) -> None:
        claude, _ = two_providers
        provider = (
            await session.execute(select(Provider).where(Provider.id == claude.provider_id))
        ).scalar_one()
        user = await make_user(session)
        everything = await add_filtered_rule(session, limit="100", name="all")
        by_provider = await add_filtered_rule(session, provider=provider, limit="100", name="p")
        by_model = await add_filtered_rule(session, model=claude, limit="100", name="m")
        rules = [everything, by_provider, by_model]
        quota = engine_for()
        subject = QuotaSubject(user.id, None)

        async def counts() -> list[Decimal]:
            current = await quota.current_values(rules, subject=subject)
            return [current[rule.id] for rule in rules]

        reservation = await quota.check_and_reserve(
            session, subject, QuotaAmounts(), target=target_of(claude)
        )
        assert await counts() == [1, 1, 1]
        # Settling with a different actual corrects all three by the same amount.
        await quota.settle(reservation, QuotaAmounts(requests=Decimal(0)))
        assert await counts() == [0, 0, 0]

        reservation = await quota.check_and_reserve(
            session, subject, QuotaAmounts(), target=target_of(claude)
        )
        await quota.settle(reservation, QuotaAmounts())
        assert await counts() == [1, 1, 1]
        released = await quota.check_and_reserve(
            session, subject, QuotaAmounts(), target=target_of(claude)
        )
        await quota.release(released)
        assert await counts() == [1, 1, 1]


class TestSearchIsRefused:
    async def _admin(
        self, app: Any, session_factory: async_sessionmaker[AsyncSession], seeded: Seeded
    ) -> None:
        from test_admin import as_user, make_admin

        as_user(app, await make_admin(session_factory, seeded))

    def _body(self, **extra: Any) -> dict[str, Any]:
        return {
            "name": "cap",
            "scope": "global",
            "metric": "cost",
            "period": "month",
            "limit_value": "20",
            **extra,
        }

    async def test_a_search_count_limit_cannot_name_a_provider_or_model(
        self,
        app: Any,
        client: httpx.AsyncClient,
        seeded: Seeded,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        await self._admin(app, session_factory, seeded)
        for extra in ({"provider_id": str(seeded.provider.id)}, {"model_id": str(seeded.model.id)}):
            r = await client.post(
                "/api/admin/limits", json=self._body(metric="own_search_requests", **extra)
            )
            assert r.status_code == 422, r.text
            assert "search-count" in r.json()["error"]["message"]
        # With no filter it is as before.
        ok = await client.post("/api/admin/limits", json=self._body(metric="own_search_requests"))
        assert ok.status_code == 201, ok.text

    async def test_a_search_backend_or_tier_cannot_be_named(
        self,
        app: Any,
        client: httpx.AsyncClient,
        seeded: Seeded,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        from gateway.models import ModelKind, ProviderKind

        await self._admin(app, session_factory, seeded)
        async with session_factory() as db:
            backend = await make_provider(db, "exa")
            backend.kind = ProviderKind.SEARCH
            tier = await make_model(db, backend, "exa-fast", priced=False)
            tier.kind = ModelKind.SEARCH
            await db.commit()
            backend_id, tier_id = str(backend.id), str(tier.id)
        for extra in ({"provider_id": backend_id}, {"model_id": tier_id}):
            r = await client.post("/api/admin/limits", json=self._body(**extra))
            assert r.status_code == 422, r.text
            assert "search" in r.json()["error"]["message"]


def _seed_served_row(path: Path) -> tuple[str, str]:
    """A provider, a model and a ledger row for it, in a database at 0088."""
    at = "2026-11-21 12:00:00.000000"
    provider, model = uuid.uuid4().hex, uuid.uuid4().hex
    fixed = {
        "providers": {
            "id": provider,
            "name": "old",
            "kind": "PROVIDER",
            "billing_mode": "OWN_PRICES",
        },
        "models": {
            "id": model,
            "name": "m",
            "provider_id": provider,
            "upstream_model": "u",
            "is_alias": 0,
        },
        "usage_records": {
            "id": uuid.uuid4().hex,
            "model_id": model,
            "status": "COMPLETED",
            "usage_source": "UNAVAILABLE",
            "api_surface": "CHAT_COMPLETIONS",
        },
    }
    with closing(sqlite3.connect(path)) as conn:
        for table, given in fixed.items():
            values = dict(given)
            for _, name, kind, notnull, default, _pk in conn.execute(f"PRAGMA table_info({table})"):
                if name in values or not notnull or default is not None:
                    continue
                kind = kind.upper()
                values[name] = (
                    at
                    if "DATETIME" in kind
                    else 0
                    if any(t in kind for t in ("INT", "BOOL", "NUMERIC", "FLOAT"))
                    else "{}"
                    if "JSON" in kind
                    else uuid.uuid4().hex
                    if "CHAR(32)" in kind
                    else "x"
                )
            columns = ", ".join(values)
            marks = ", ".join("?" for _ in values)
            conn.execute(f"INSERT INTO {table} ({columns}) VALUES ({marks})", list(values.values()))
        conn.commit()
    return model, provider


class TestMigration:
    def test_up_and_down_on_sqlite(self, tmp_path: Path) -> None:
        path = tmp_path / "m.db"
        url = f"sqlite+aiosqlite:///{path}"
        assert _alembic(url, "upgrade", "0088").returncode == 0
        at = "2026-11-21 12:00:00.000000"

        def insert(name: str = "old") -> str:
            return (
                "INSERT INTO limit_rules (id, name, scope, scope_id, metric, window_seconds, "
                "period, limit_value, is_active, created_at, updated_at, applies_to) "
                f"VALUES ('{uuid.uuid4().hex}', '{name}', 'global', NULL, 'cost', NULL, "
                f"'month', 5, 1, '{at}', '{at}', 'aggregate')"
            )

        with closing(sqlite3.connect(path)) as conn:
            conn.execute(insert())
            conn.commit()

        # A served row at 0088, to see the backfill.
        model_hex, provider_hex = _seed_served_row(path)

        up = _alembic(url, "upgrade", "0090")
        assert up.returncode == 0, up.stderr[-2000:]
        with closing(sqlite3.connect(path)) as conn:
            assert conn.execute(
                "SELECT provider_id FROM usage_records WHERE model_id = ?", (model_hex,)
            ).fetchall() == [(provider_hex,)]
            # A model rule stores the model alone.
            with pytest.raises(sqlite3.IntegrityError):
                conn.execute(
                    insert("both")
                    .replace("applies_to)", "applies_to, provider_id, model_id)")
                    .replace("'aggregate')", f"'aggregate', '{provider_hex}', '{model_hex}')")
                )
            conn.rollback()
        provider_id = uuid.uuid4().hex
        with closing(sqlite3.connect(path)) as conn:
            # The existing rule is unfiltered and unchanged.
            assert conn.execute("SELECT provider_id, model_id FROM limit_rules").fetchall() == [
                (None, None)
            ]
            # An identical unfiltered rule still collides...
            with pytest.raises(sqlite3.IntegrityError):
                conn.execute(insert("dup"))
            conn.rollback()
            conn.execute("PRAGMA foreign_keys=OFF")
            # ...while the same rule on a provider is another rule, and so is a second
            # provider's, but not the same provider twice.
            filtered = (
                "INSERT INTO limit_rules (id, name, scope, scope_id, metric, window_seconds, "
                "period, limit_value, is_active, created_at, updated_at, applies_to, provider_id) "
                f"VALUES ('{{id}}', 'p', 'global', NULL, 'cost', NULL, 'month', 5, 1, "
                f"'{at}', '{at}', 'aggregate', '{{provider}}')"
            )
            conn.execute(filtered.format(id=uuid.uuid4().hex, provider=provider_id))
            conn.execute(filtered.format(id=uuid.uuid4().hex, provider=uuid.uuid4().hex))
            with pytest.raises(sqlite3.IntegrityError):
                conn.execute(filtered.format(id=uuid.uuid4().hex, provider=provider_id))
            conn.rollback()
            conn.execute(f"DELETE FROM limit_rules WHERE provider_id = '{provider_id}'")
            conn.commit()

        # The downgrade refuses while a filtered rule exists, and says why.
        with closing(sqlite3.connect(path)) as conn:
            conn.execute("PRAGMA foreign_keys=OFF")
            conn.execute(filtered.format(id=uuid.uuid4().hex, provider=provider_id))
            conn.commit()
        refused = _alembic(url, "downgrade", "0088")
        assert refused.returncode != 0
        assert "limited to a provider or a model" in refused.stderr
        with closing(sqlite3.connect(path)) as conn:
            conn.execute("DELETE FROM limit_rules WHERE provider_id IS NOT NULL")
            conn.commit()
        down = _alembic(url, "downgrade", "0088")
        assert down.returncode == 0, down.stderr[-2000:]
        with closing(sqlite3.connect(path)) as conn:
            columns = {row[1] for row in conn.execute("PRAGMA table_info(limit_rules)")}
            assert "provider_id" not in columns and "model_id" not in columns
            assert conn.execute("SELECT count(*) FROM limit_rules").fetchone() == (1,)
