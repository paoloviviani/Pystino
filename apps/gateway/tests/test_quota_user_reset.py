"""Resetting one user's consumption of a per-user quota, and the page that shows it.

A group rule applied to each member and a global rule applied to each user keep
one counter per user. A reset can now name that user (``quota_resets.user_id``)
and renames only their counter key. The properties pinned here:

- the engine: one user's reset zeroes them and nobody else, for both per-user
  kinds; a whole-rule reset still zeroes everyone; reserve and settle around a
  reset use the same epoch; the shared ``user:<id>`` counter of an each-user
  rule and a ``scope=user`` rule splits on a per-user reset, by design;
- the API: per-user reset is refused on shared rules (422), 404 for an unknown
  or non-member user, audited, and listed with the user's email;
- the consumption endpoints: all four kinds of applicable rule, an override
  shown as replaced, values that honour resets, shared rules marked, and 403
  for non-admins;
- erasure and merge of a user who has per-user resets.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

import httpx
import pytest
from conftest import Seeded
from gateway.merge import merge_users
from gateway.models import (
    AdminAuditEvent,
    LimitAppliesTo,
    LimitMetric,
    LimitRule,
    LimitScope,
    QuotaReset,
    User,
)
from gateway.periods import PeriodKind
from gateway.quota import QuotaAmounts, QuotaEngine, QuotaExceeded, QuotaSubject
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from test_admin import as_user, make_admin
from test_quota_calendar import make_group
from test_quota_engine import engine_for
from test_quota_per_member import add_member_rule, make_member
from test_quota_per_user import add_user_rule, make_user, one_request


async def reset_for(
    session: AsyncSession, rule: LimitRule, user: User | None, *, by: User | None = None
) -> QuotaReset:
    reset = QuotaReset(
        rule_id=rule.id, user_id=user.id if user else None, created_by=by.id if by else None
    )
    session.add(reset)
    await session.commit()
    await session.refresh(rule, attribute_names=["resets"])
    return reset


async def value(
    quota: QuotaEngine, session: AsyncSession, rule: LimitRule, subject: QuotaSubject
) -> Decimal | None:
    await session.refresh(rule, attribute_names=["resets"])
    return (await quota.current_values([rule], subject=subject)).get(rule.id)


class TestEngineEpoch:
    def test_epoch_ignores_another_users_reset(self) -> None:
        mine, other = uuid.uuid4(), uuid.uuid4()
        rule = LimitRule(
            name="r",
            scope=LimitScope.GLOBAL,
            metric=LimitMetric.REQUESTS,
            window_seconds=60,
            limit_value=Decimal(1),
            applies_to=LimitAppliesTo.EACH_USER,
        )
        t0 = datetime(2026, 10, 1, tzinfo=UTC)
        rule.resets = [
            QuotaReset(user_id=other, effective_at=t0 + timedelta(days=5)),
            QuotaReset(user_id=None, effective_at=t0),
        ]
        assert QuotaEngine.reset_epoch_for(rule, mine) == int(t0.timestamp())
        assert QuotaEngine.reset_epoch_for(rule, other) == int((t0 + timedelta(days=5)).timestamp())
        assert QuotaEngine.reset_epoch_for(rule) == int(t0.timestamp())

    async def test_each_member_reset_zeroes_that_member_only(self, session: AsyncSession) -> None:
        group = await make_group(session)
        first = await make_member(session, group)
        second = await make_member(session, group)
        rule = await add_member_rule(session, scope_id=group.id, limit="2")
        quota = engine_for()
        for member in (first, second):
            for _ in range(2):
                await one_request(quota, session, QuotaSubject(member.id, group.id))
        with pytest.raises(QuotaExceeded):
            await quota.check_and_reserve(session, QuotaSubject(first.id, group.id), QuotaAmounts())

        await reset_for(session, rule, first)

        assert await value(quota, session, rule, QuotaSubject(first.id, group.id)) == 0
        assert await value(quota, session, rule, QuotaSubject(second.id, group.id)) == 2
        await quota.check_and_reserve(session, QuotaSubject(first.id, group.id), QuotaAmounts())
        with pytest.raises(QuotaExceeded):
            await quota.check_and_reserve(
                session, QuotaSubject(second.id, group.id), QuotaAmounts()
            )

    async def test_each_user_reset_zeroes_that_user_only(self, session: AsyncSession) -> None:
        first = await make_user(session)
        second = await make_user(session)
        rule = await add_user_rule(session, limit="2")
        quota = engine_for()
        for user in (first, second):
            for _ in range(2):
                await one_request(quota, session, QuotaSubject(user.id, None))

        await reset_for(session, rule, first)

        assert await value(quota, session, rule, QuotaSubject(first.id, None)) == 0
        assert await value(quota, session, rule, QuotaSubject(second.id, None)) == 2
        await quota.check_and_reserve(session, QuotaSubject(first.id, None), QuotaAmounts())
        with pytest.raises(QuotaExceeded):
            await quota.check_and_reserve(session, QuotaSubject(second.id, None), QuotaAmounts())

    async def test_a_whole_rule_reset_still_zeroes_everyone(self, session: AsyncSession) -> None:
        group = await make_group(session)
        first = await make_member(session, group)
        second = await make_member(session, group)
        rule = await add_member_rule(session, scope_id=group.id, limit="2")
        quota = engine_for()
        for member in (first, second):
            await one_request(quota, session, QuotaSubject(member.id, group.id))

        await reset_for(session, rule, None)

        for member in (first, second):
            assert await value(quota, session, rule, QuotaSubject(member.id, group.id)) == 0

    async def test_reserve_and_settle_share_an_epoch_across_a_reset(
        self, session: AsyncSession
    ) -> None:
        """A reset landing between reserve and settle must not move the settle
        onto the new key: the correction belongs to the counter reserved on."""
        user = await make_user(session)
        rule = await add_user_rule(session, limit="100")
        quota = engine_for()
        subject = QuotaSubject(user.id, None)

        reservation = await quota.check_and_reserve(session, subject, QuotaAmounts())
        await reset_for(session, rule, user)
        await quota.settle(reservation, QuotaAmounts(requests=Decimal(4)))

        assert await value(quota, session, rule, subject) == 0
        # And the next request counts on the new epoch from zero.
        await one_request(quota, session, subject)
        assert await value(quota, session, rule, subject) == 1

    async def test_the_shared_user_counter_splits_on_a_per_user_reset(
        self, session: AsyncSession
    ) -> None:
        """An each-user rule and the person's own scope=user rule read one
        counter only while their epochs match. Resetting the each-user rule
        for them moves that rule to a fresh key; the other keeps its count."""
        from test_quota_engine import add_rule

        user = await make_user(session)
        each_user = await add_user_rule(session, limit="10")
        own = await add_rule(
            session,
            scope=LimitScope.USER,
            scope_id=user.id,
            metric=LimitMetric.REQUESTS,
            limit="10",
            window_seconds=3600,
        )
        await session.refresh(own, attribute_names=["resets"])
        quota = engine_for()
        subject = QuotaSubject(user.id, None)
        for _ in range(3):
            await one_request(quota, session, subject)

        await reset_for(session, each_user, user)

        assert await value(quota, session, each_user, subject) == 0
        assert await value(quota, session, own, subject) == 3
        await one_request(quota, session, subject)
        assert await value(quota, session, each_user, subject) == 1
        assert await value(quota, session, own, subject) == 4


# ----------------------------------------------------------------------------
# API
# ----------------------------------------------------------------------------


@pytest.fixture
async def admin_client(
    app: Any, client: httpx.AsyncClient, seeded: Seeded, session_factory: Any
) -> httpx.AsyncClient:
    as_user(app, await make_admin(session_factory, seeded))
    return client


async def make_rule(session: AsyncSession, **kw: Any) -> LimitRule:
    rule = LimitRule(
        name=kw.pop("name", "rule"),
        metric=kw.pop("metric", LimitMetric.REQUESTS),
        window_seconds=kw.pop("window_seconds", 3600),
        limit_value=Decimal(kw.pop("limit", "10")),
        **kw,
    )
    session.add(rule)
    await session.commit()
    await session.refresh(rule, attribute_names=["resets"])
    return rule


class TestResetApi:
    async def test_per_user_reset_on_a_shared_rule_is_422(
        self, admin_client: httpx.AsyncClient, session: AsyncSession, seeded: Seeded
    ) -> None:
        group_total = await make_rule(session, scope=LimitScope.GROUP, scope_id=seeded.group.id)
        global_total = await make_rule(
            session, scope=LimitScope.GLOBAL, metric=LimitMetric.COST, name="total"
        )
        for rule in (group_total, global_total):
            response = await admin_client.post(
                f"/api/admin/limits/{rule.id}/reset", json={"user_id": str(seeded.user.id)}
            )
            assert response.status_code == 422, response.text
        assert (await session.execute(select(QuotaReset))).first() is None

    async def test_unknown_and_non_member_users_are_404(
        self, admin_client: httpx.AsyncClient, session: AsyncSession, seeded: Seeded
    ) -> None:
        rule = await make_rule(
            session,
            scope=LimitScope.GROUP,
            scope_id=seeded.group.id,
            applies_to=LimitAppliesTo.EACH_MEMBER,
        )
        outsider = await make_user(session)
        await session.commit()
        for user_id in (uuid.uuid4(), outsider.id):
            response = await admin_client.post(
                f"/api/admin/limits/{rule.id}/reset", json={"user_id": str(user_id)}
            )
            assert response.status_code == 404, response.text
        each_user = await make_rule(
            session,
            scope=LimitScope.GLOBAL,
            applies_to=LimitAppliesTo.EACH_USER,
            metric=LimitMetric.COST,
            name="eu",
        )
        assert (
            await admin_client.post(
                f"/api/admin/limits/{each_user.id}/reset", json={"user_id": str(uuid.uuid4())}
            )
        ).status_code == 404
        # An each-user rule has no membership to ask about: any user qualifies.
        assert (
            await admin_client.post(
                f"/api/admin/limits/{each_user.id}/reset", json={"user_id": str(outsider.id)}
            )
        ).status_code == 200

    async def test_a_per_user_reset_is_stored_audited_and_listed(
        self,
        admin_client: httpx.AsyncClient,
        session: AsyncSession,
        seeded: Seeded,
    ) -> None:
        rule = await make_rule(
            session,
            scope=LimitScope.GROUP,
            scope_id=seeded.group.id,
            applies_to=LimitAppliesTo.EACH_MEMBER,
        )
        response = await admin_client.post(
            f"/api/admin/limits/{rule.id}/reset",
            json={"user_id": str(seeded.user.id), "reason": "extension"},
        )
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["user_id"] == str(seeded.user.id)
        assert body["user_email"] == seeded.user.email

        stored = (await session.execute(select(QuotaReset))).scalar_one()
        assert stored.user_id == seeded.user.id
        audit = (
            await session.execute(
                select(AdminAuditEvent).where(AdminAuditEvent.action == "limit.reset")
            )
        ).scalar_one()
        assert audit.target_id == str(rule.id)
        assert audit.changes["user"] == {"after": seeded.user.email}

        await admin_client.post(f"/api/admin/limits/{rule.id}/reset", json={"reason": "all"})
        listed = (await admin_client.get(f"/api/admin/limits/{rule.id}/resets")).json()["items"]
        assert sorted((e["user_email"], e["reason"]) for e in listed if e["user_email"]) == [
            (seeded.user.email, "extension")
        ]
        assert [e["user_id"] for e in listed].count(None) == 1
        # last_reset_at on the rule is the whole-rule reset's, never a user's.
        rules = (await admin_client.get("/api/admin/limits")).json()["items"]
        whole = max(e["effective_at"] for e in listed if e["user_id"] is None)
        assert [r["last_reset_at"] for r in rules if r["id"] == str(rule.id)] == [whole]


# ----------------------------------------------------------------------------
# consumption endpoints
# ----------------------------------------------------------------------------


class Fleet:
    def __init__(self) -> None:
        self.rules: dict[str, LimitRule] = {}


async def build_fleet(session: AsyncSession, seeded: Seeded) -> Fleet:
    """The seeded user in group G1 with all four kinds of rule, plus decoys."""
    fleet = Fleet()
    other = await make_group(session, "elsewhere")
    stranger = await make_user(session)
    g1 = seeded.group.id
    r = fleet.rules
    r["own"] = await make_rule(
        session, scope=LimitScope.USER, scope_id=seeded.user.id, metric=LimitMetric.COST, name="own"
    )
    r["each_user"] = await make_rule(
        session,
        scope=LimitScope.GLOBAL,
        applies_to=LimitAppliesTo.EACH_USER,
        metric=LimitMetric.TOKENS,
        name="each user tokens",
        limit="100",
    )
    r["replaced"] = await make_rule(
        session,
        scope=LimitScope.GLOBAL,
        applies_to=LimitAppliesTo.EACH_USER,
        name="each user requests",
        limit="5",
    )
    r["override"] = await make_rule(
        session,
        scope=LimitScope.USER,
        scope_id=seeded.user.id,
        overrides_each_user=True,
        name="my requests",
        limit="20",
    )
    r["each_member"] = await make_rule(
        session,
        scope=LimitScope.GROUP,
        scope_id=g1,
        applies_to=LimitAppliesTo.EACH_MEMBER,
        period=PeriodKind.MONTH,
        window_seconds=None,
        name="per member",
        limit="4",
        metric=LimitMetric.REQUESTS,
    )
    r["group_total"] = await make_rule(
        session,
        scope=LimitScope.GROUP,
        scope_id=g1,
        name="group total",
        limit="50",
        window_seconds=7200,
    )
    r["global_total"] = await make_rule(
        session, scope=LimitScope.GLOBAL, metric=LimitMetric.COST, name="all", limit="500"
    )
    # Not applicable to the seeded user: another group's rule, another user's.
    r["decoy_group"] = await make_rule(
        session, scope=LimitScope.GROUP, scope_id=other.id, name="decoy group"
    )
    r["decoy_user"] = await make_rule(
        session, scope=LimitScope.USER, scope_id=stranger.id, name="decoy user"
    )
    await session.commit()
    return fleet


class TestQuotaUsageApi:
    async def test_non_admins_get_403(
        self,
        app: Any,
        client: httpx.AsyncClient,
        seeded: Seeded,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        as_user(app, await make_admin(session_factory, seeded, admin=False))
        assert (await client.get("/api/admin/quota-usage")).status_code == 403
        url = f"/api/admin/quota-usage/{seeded.user.id}"
        assert (await client.get(url)).status_code == 403

    async def test_unknown_user_is_404(self, admin_client: httpx.AsyncClient) -> None:
        assert (await admin_client.get(f"/api/admin/quota-usage/{uuid.uuid4()}")).status_code == 404

    async def test_every_kind_applies_and_overrides_are_replaced(
        self, admin_client: httpx.AsyncClient, session: AsyncSession, seeded: Seeded
    ) -> None:
        fleet = await build_fleet(session, seeded)
        body = (await admin_client.get(f"/api/admin/quota-usage/{seeded.user.id}")).json()
        by_id = {e["rule_id"]: e for e in body["entries"]}
        ids = {name: str(rule.id) for name, rule in fleet.rules.items()}
        assert set(by_id) == {ids[n] for n in ids if not n.startswith("decoy")}
        assert by_id[ids["own"]]["kind"] == "own"
        assert by_id[ids["each_user"]]["kind"] == "each_user"
        assert by_id[ids["each_member"]]["kind"] == "each_member"
        assert by_id[ids["each_member"]]["scope_label"] == "research"
        assert by_id[ids["group_total"]]["kind"] == "shared"
        assert by_id[ids["global_total"]]["kind"] == "shared"
        for name in ("each_user", "each_member"):
            assert by_id[ids[name]]["can_reset_for_user"] is True
        # A user's own rule is theirs alone: no per-user variant of it exists.
        for name in ("own", "group_total", "global_total"):
            assert by_id[ids[name]]["can_reset_for_user"] is False
        # The override's figure holds; the rule it replaces is not a second budget.
        replaced = by_id[ids["replaced"]]
        assert replaced["replaced_by_rule_id"] == ids["override"]
        assert replaced["replaced_by_name"] == "my requests"
        assert replaced["can_reset_for_user"] is False
        assert replaced["current_value"] is None
        assert by_id[ids["override"]]["replaced_by_rule_id"] is None
        assert body["entries"][-1]["rule_id"] == ids["replaced"]
        # A calendar rule says when it resets.
        assert by_id[ids["each_member"]]["resets_at"] is not None

    async def test_values_honour_resets_and_shared_rules_show_the_shared_total(
        self, app: Any, admin_client: httpx.AsyncClient, session: AsyncSession, seeded: Seeded
    ) -> None:
        fleet = await build_fleet(session, seeded)
        ids = {name: str(rule.id) for name, rule in fleet.rules.items()}
        quota = app.state.quota_engine
        mine = QuotaSubject(seeded.user.id, seeded.group.id)
        colleague = await make_member(session, seeded.group)
        await session.commit()
        for _ in range(2):
            await one_request(quota, session, mine)
        await one_request(quota, session, QuotaSubject(colleague.id, seeded.group.id))

        async def entries(user_id: uuid.UUID) -> dict[str, Any]:
            body = (await admin_client.get(f"/api/admin/quota-usage/{user_id}")).json()
            return {e["rule_id"]: e for e in body["entries"]}

        before = await entries(seeded.user.id)
        assert Decimal(before[ids["each_member"]]["current_value"]) == 2
        assert Decimal(before[ids["group_total"]]["current_value"]) == 3  # everyone's
        assert before[ids["each_member"]]["percent"] == 50
        assert before[ids["each_member"]]["last_reset_at"] is None

        reset = await admin_client.post(
            f"/api/admin/limits/{ids['each_member']}/reset",
            json={"user_id": str(seeded.user.id), "reason": "r"},
        )
        assert reset.status_code == 200
        after = await entries(seeded.user.id)
        assert Decimal(after[ids["each_member"]]["current_value"]) == 0
        assert after[ids["each_member"]]["last_reset_for_user"] is True
        assert after[ids["each_member"]]["last_reset_by_email"] == seeded.user.email
        assert Decimal(after[ids["group_total"]]["current_value"]) == 3
        # The colleague is untouched by it.
        assert Decimal((await entries(colleague.id))[ids["each_member"]]["current_value"]) == 1

    async def test_the_user_list_shows_the_most_constrained_quota(
        self, app: Any, admin_client: httpx.AsyncClient, session: AsyncSession, seeded: Seeded
    ) -> None:
        fleet = await build_fleet(session, seeded)
        quota = app.state.quota_engine
        for _ in range(3):
            await one_request(quota, session, QuotaSubject(seeded.user.id, seeded.group.id))

        page = (
            await admin_client.get("/api/admin/quota-usage", params={"q": seeded.user.email})
        ).json()
        assert page["total"] == 1
        row = page["items"][0]
        assert row["user_id"] == str(seeded.user.id)
        # 3 of the per-member 4 requests (75%) beats the override's 3 of 20.
        assert row["most_constrained"]["rule_id"] == str(fleet.rules["each_member"].id)
        assert row["most_constrained"]["percent"] == 75
        # Seven enforced rules; the replaced one is not counted as a budget.
        assert row["rules"] == 6
        paged = (await admin_client.get("/api/admin/quota-usage", params={"limit": 1})).json()
        assert len(paged["items"]) == 1 and paged["total"] >= 2
        none = (await admin_client.get("/api/admin/quota-usage", params={"q": "zzz-nobody"})).json()
        assert none["items"] == []


# ----------------------------------------------------------------------------
# erasure and merge
# ----------------------------------------------------------------------------


class TestUserLifecycle:
    async def test_erasing_a_user_takes_their_resets_and_spares_everyone_elses(
        self, session: AsyncSession
    ) -> None:
        group = await make_group(session)
        gone = await make_member(session, group)
        stays = await make_member(session, group)
        rule = await add_member_rule(session, scope_id=group.id)
        await reset_for(session, rule, gone)
        await reset_for(session, rule, stays)
        await reset_for(session, rule, None)

        await session.delete(gone)
        await session.commit()

        remaining = (await session.execute(select(QuotaReset.user_id))).scalars().all()
        # Not nulled into a whole-rule reset, which would zero everyone.
        assert sorted(map(str, remaining), key=str) == sorted([str(stays.id), "None"], key=str)

    async def test_a_merge_moves_per_user_resets_to_the_survivor(
        self, session: AsyncSession, seeded: Seeded
    ) -> None:
        group = await make_group(session)
        source = await make_member(session, group)
        target = await make_member(session, group)
        rule = await add_member_rule(session, scope_id=group.id)
        await reset_for(session, rule, source)
        await session.commit()

        await merge_users(
            session,
            source_id=source.id,
            target_id=target.id,
            actor_id=seeded.user.id,
            actor_label="admin",
        )
        await session.commit()

        assert (await session.execute(select(QuotaReset.user_id))).scalars().all() == [target.id]
