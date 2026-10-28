"""How many SQL round trips one request costs.

A performance property, pinned as a test because that is the only way it stays
true. Measured at 20.5ms p50 against a 1.4ms upstream, the gateway's own cost is
dominated by per-request database round trips, and the number of them is the
kind of thing an innocent-looking `selectinload` changes without anybody
noticing.

The numbers here are upper bounds, deliberately loose enough not to fail on an
unrelated change and tight enough to catch a regression that adds a query to the
hot path. If one of these has to be raised, that is a decision worth making on
purpose.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager

import httpx
from conftest import FakeUpstream, Seeded
from sqlalchemy import event
from sqlalchemy.engine import Engine
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker


@contextmanager
def counted(engine: Engine) -> Iterator[list[str]]:
    """Every statement the engine executes while the block runs."""
    seen: list[str] = []

    def before(conn: object, cursor: object, statement: str, *args: object) -> None:
        seen.append(statement)

    # The async engine wraps a sync one; the events fire on the sync dialect.
    target = engine.sync_engine if hasattr(engine, "sync_engine") else engine
    event.listen(target, "before_cursor_execute", before)
    try:
        yield seen
    finally:
        event.remove(target, "before_cursor_execute", before)


def summarise(statements: list[str]) -> str:
    """A readable digest, for when an assertion fails."""
    lines = []
    for statement in statements:
        first = " ".join(statement.split())[:90]
        lines.append(f"    {first}")
    return "\n".join(lines)


class TestTheMeteredPath:
    async def test_a_chat_completion_stays_within_its_query_budget(
        self,
        client: httpx.AsyncClient,
        seeded: Seeded,
        app: object,
        fake_upstream: FakeUpstream,
    ) -> None:
        """The hot path: auth, access, quota, and two ledger writes."""
        engine = app.state.engine  # type: ignore[attr-defined]
        fake_upstream.set_json(
            {
                "id": "c1",
                "object": "chat.completion",
                "choices": [{"index": 0, "message": {"role": "assistant", "content": "ok"}}],
                "usage": {"prompt_tokens": 10, "completion_tokens": 2, "total_tokens": 12},
            }
        )

        # Warm anything one-off — the first request of a process may load a
        # limit rule set or a dialect detail that later ones do not.
        await client.post(
            "/v1/chat/completions",
            json={"model": seeded.model.name, "messages": [{"role": "user", "content": "hi"}]},
            headers=seeded.auth,
        )

        with counted(engine) as statements:
            response = await client.post(
                "/v1/chat/completions",
                json={"model": seeded.model.name, "messages": [{"role": "user", "content": "hi"}]},
                headers=seeded.auth,
            )
        assert response.status_code == 200

        selects = [s for s in statements if s.lstrip().upper().startswith("SELECT")]
        writes = [
            s for s in statements if s.lstrip().upper().startswith(("INSERT", "UPDATE", "DELETE"))
        ]

        # Two writes by design: the in-progress row is created before the
        # upstream call so a crash mid-request is visible, then finalised.
        print(f"\n  metered path: {len(selects)} selects, {len(writes)} writes")
        print(summarise(selects))
        assert len(writes) <= 2, f"{len(writes)} writes:\n{summarise(writes)}"
        # Five: the key (joined to user and billing group), memberships, the
        # model (joined to its provider), its prices, and the limit rules.
        assert len(selects) <= 5, f"{len(selects)} selects:\n{summarise(selects)}"

    async def test_authentication_alone_is_cheap(
        self, client: httpx.AsyncClient, seeded: Seeded, app: object
    ) -> None:
        """`/v1/models` is auth plus the access-filtered catalogue and nothing else.

        Isolated from the metered path because authentication runs on *every*
        request, so a query added here is multiplied by everything.
        """
        engine = app.state.engine  # type: ignore[attr-defined]
        await client.get("/v1/models", headers=seeded.auth)

        with counted(engine) as statements:
            assert (await client.get("/v1/models", headers=seeded.auth)).status_code == 200

        selects = [s for s in statements if s.lstrip().upper().startswith("SELECT")]
        print(f"\n  /v1/models: {len(selects)} selects")
        print(summarise(selects))
        # Four: the key joined to its user and group, memberships, the
        # catalogue, and the agents the caller may reach. Was five before the
        # loaders were changed from `selectinload` to `joinedload` for the
        # many-to-one relations, and three before agents were listed here.
        #
        # The fourth is agents (ADR 0062), and it is the price of an agent
        # being addressable as a model: a client discovers one from this
        # listing, so it has to be in it. It stayed at four rather than five
        # because the reachability predicate reads
        # `principal.user.group_ids()` — memberships are already loaded to
        # authenticate the request — where a second `effective_group_ids` call
        # would have added a round trip for something in hand. That mistake was
        # made and this test is what caught it.
        assert len(selects) <= 4, f"{len(selects)} selects:\n{summarise(selects)}"

    async def test_bearer_authentication_costs_no_more_than_a_key(
        self,
        bearer_app: object,
        client: httpx.AsyncClient,
        seeded: Seeded,
        signing_key: object,
    ) -> None:
        """An access token must not be the expensive way in.

        The budget is the API key path's three selects plus one: routing the
        token to the identity provider whose keys must verify it (ADR 0051) is
        an indexed lookup on a table with a row per configured IdP. It holds
        only because memberships are reconciled *when the token disagrees with
        them* rather than on every request. Writing them every time would put
        two writes on the hot path of every chat message; the second request
        below is the one measured precisely because the first has already
        settled any divergence.
        """
        from test_bearer_auth import auth, make_token

        engine = bearer_app.state.engine  # type: ignore[attr-defined]
        token = make_token(signing_key)  # type: ignore[arg-type]
        await client.get("/v1/models", headers=auth(token))

        with counted(engine) as statements:
            assert (await client.get("/v1/models", headers=auth(token))).status_code == 200

        selects = [s for s in statements if s.lstrip().upper().startswith("SELECT")]
        writes = [
            s for s in statements if s.lstrip().upper().startswith(("INSERT", "UPDATE", "DELETE"))
        ]
        print(f"\n  /v1/models (bearer): {len(selects)} selects, {len(writes)} writes")
        print(summarise(selects))
        assert not writes, f"a steady-state token request wrote:\n{summarise(writes)}"
        # Four before agents, five now: the user (joined to its default billing
        # group), memberships (joined to their groups), the catalogue, and the
        # agents this caller may reach. One more than the key path because a
        # bearer resolves the user rather than being handed it by the key row —
        # which is the asymmetry this test exists to bound, and it has not
        # grown.
        assert len(selects) <= 5, f"{len(selects)} selects:\n{summarise(selects)}"

    async def test_bearer_group_gate_adds_no_queries(
        self,
        bearer_app: object,
        client: httpx.AsyncClient,
        seeded: Seeded,
        signing_key: object,
        session_factory: object,
    ) -> None:
        """ADR 0095: the group gate on the steady-state path reads the
        memberships authentication already loaded — no query, no write, same
        budget as without the gate."""
        from gateway.models import OIDCPolicyConfig
        from test_bearer_auth import auth, make_token

        async with session_factory() as session:  # type: ignore[attr-defined]
            # The self-mapping is the operator's vouch for the IdP name (ADR
            # 0095 review): without it the claim-sourced membership would not
            # count toward the gate.
            session.add(
                OIDCPolicyConfig(
                    join_mode="group",
                    required_groups=["research"],
                    group_mappings=[["research", "research"]],
                )
            )
            await session.commit()
        await bearer_app.state.oidc_policy.refresh_once()  # type: ignore[attr-defined]

        engine = bearer_app.state.engine  # type: ignore[attr-defined]
        token = make_token(signing_key)  # type: ignore[arg-type]
        assert (await client.get("/v1/models", headers=auth(token))).status_code == 200

        with counted(engine) as statements:
            assert (await client.get("/v1/models", headers=auth(token))).status_code == 200

        selects = [s for s in statements if s.lstrip().upper().startswith("SELECT")]
        writes = [
            s for s in statements if s.lstrip().upper().startswith(("INSERT", "UPDATE", "DELETE"))
        ]
        print(f"\n  /v1/models (bearer, group mode): {len(selects)} selects, {len(writes)} writes")
        print(summarise(selects))
        assert not writes, f"a steady-state token request wrote:\n{summarise(writes)}"
        assert len(selects) <= 5, f"{len(selects)} selects:\n{summarise(selects)}"

    async def test_a_per_member_rule_adds_no_queries(
        self,
        client: httpx.AsyncClient,
        seeded: Seeded,
        app: object,
        fake_upstream: FakeUpstream,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        """A per-member rule rides the same single rule query and Valkey keys.

        The member attribution is a second counter key, not a second query:
        rule loading is still one SELECT (plus the resets selectinload any
        rule incurs), and the reserve is Valkey INCRBYs. Measured against the
        same setup with only the aggregate rule, so the assertion is that the
        per-member kind costs nothing further — not a new absolute number.
        """
        from decimal import Decimal

        from gateway.models import LimitAppliesTo, LimitMetric, LimitRule, LimitScope
        from test_chat_completions import basic_request

        engine = app.state.engine  # type: ignore[attr-defined]
        completion = {
            "id": "c1",
            "object": "chat.completion",
            "choices": [{"index": 0, "message": {"role": "assistant", "content": "ok"}}],
            "usage": {"prompt_tokens": 10, "completion_tokens": 2, "total_tokens": 12},
        }

        async def make_rules(applies: list[str]) -> None:
            async with session_factory() as db:
                for mode in applies:
                    db.add(
                        LimitRule(
                            name=f"rule {mode}",
                            scope=LimitScope.GROUP,
                            scope_id=seeded.group.id,
                            metric=LimitMetric.COST,
                            window_seconds=3600,
                            limit_value=Decimal("1000"),
                            applies_to=LimitAppliesTo(mode),
                        )
                    )
                await db.commit()

        async def metered() -> tuple[int, int]:
            fake_upstream.set_json(completion)
            with counted(engine) as statements:
                response = await client.post(
                    "/v1/chat/completions",
                    json=basic_request(),
                    headers=seeded.auth,
                )
            assert response.status_code == 200
            selects = [s for s in statements if s.lstrip().upper().startswith("SELECT")]
            writes = [
                s
                for s in statements
                if s.lstrip().upper().startswith(("INSERT", "UPDATE", "DELETE"))
            ]
            return len(selects), len(writes)

        await make_rules(["aggregate"])
        # Warm anything one-off, as the budget test above does.
        await metered()
        aggregate = await metered()

        await make_rules(["each_member"])
        member = await metered()

        print(f"\n  aggregate-only: {aggregate[0]} selects, {aggregate[1]} writes")
        print(f"  aggregate+per-member: {member[0]} selects, {member[1]} writes")
        assert member == aggregate, f"per-member rules cost extra queries: {aggregate} -> {member}"
        assert member[1] <= 2

    async def test_a_per_user_rule_adds_no_queries(
        self,
        client: httpx.AsyncClient,
        seeded: Seeded,
        app: object,
        fake_upstream: FakeUpstream,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        """A global each-user rule rides the one rule query and the user's own
        counter: the metered path costs what it did with a plain global rule."""
        from decimal import Decimal

        from gateway.models import LimitAppliesTo, LimitMetric, LimitRule, LimitScope
        from test_chat_completions import basic_request

        engine = app.state.engine  # type: ignore[attr-defined]
        completion = {
            "id": "c1",
            "object": "chat.completion",
            "choices": [{"index": 0, "message": {"role": "assistant", "content": "ok"}}],
            "usage": {"prompt_tokens": 10, "completion_tokens": 2, "total_tokens": 12},
        }

        async def make_rule(mode: str) -> None:
            async with session_factory() as db:
                db.add(
                    LimitRule(
                        name=f"rule {mode}",
                        scope=LimitScope.GLOBAL,
                        metric=LimitMetric.COST,
                        window_seconds=3600,
                        limit_value=Decimal("1000"),
                        applies_to=LimitAppliesTo(mode),
                    )
                )
                await db.commit()

        async def metered() -> tuple[int, int]:
            fake_upstream.set_json(completion)
            with counted(engine) as statements:
                response = await client.post(
                    "/v1/chat/completions", json=basic_request(), headers=seeded.auth
                )
            assert response.status_code == 200
            selects = [s for s in statements if s.lstrip().upper().startswith("SELECT")]
            writes = [
                s
                for s in statements
                if s.lstrip().upper().startswith(("INSERT", "UPDATE", "DELETE"))
            ]
            return len(selects), len(writes)

        await make_rule("aggregate")
        await metered()
        aggregate = await metered()

        await make_rule("each_user")
        per_user = await metered()

        print(f"\n  global aggregate: {aggregate[0]} selects, {aggregate[1]} writes")
        print(f"  global aggregate+each-user: {per_user[0]} selects, {per_user[1]} writes")
        assert per_user == aggregate, (
            f"per-user rules cost extra queries: {aggregate} -> {per_user}"
        )
        assert per_user[1] <= 2

    async def test_an_override_rule_adds_no_queries(
        self,
        client: httpx.AsyncClient,
        seeded: Seeded,
        app: object,
        fake_upstream: FakeUpstream,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        """An override replaces the each-user rule in the rules already loaded:
        no lookup of its own, so the metered path costs what it did with the
        each-user rule alone."""
        from decimal import Decimal

        from gateway.models import LimitAppliesTo, LimitMetric, LimitRule, LimitScope
        from test_chat_completions import basic_request

        engine = app.state.engine  # type: ignore[attr-defined]
        completion = {
            "id": "c1",
            "object": "chat.completion",
            "choices": [{"index": 0, "message": {"role": "assistant", "content": "ok"}}],
            "usage": {"prompt_tokens": 10, "completion_tokens": 2, "total_tokens": 12},
        }

        async def metered() -> tuple[int, int]:
            fake_upstream.set_json(completion)
            with counted(engine) as statements:
                response = await client.post(
                    "/v1/chat/completions", json=basic_request(), headers=seeded.auth
                )
            assert response.status_code == 200
            selects = [s for s in statements if s.lstrip().upper().startswith("SELECT")]
            writes = [
                s
                for s in statements
                if s.lstrip().upper().startswith(("INSERT", "UPDATE", "DELETE"))
            ]
            return len(selects), len(writes)

        async with session_factory() as db:
            db.add(
                LimitRule(
                    name="each user",
                    scope=LimitScope.GLOBAL,
                    metric=LimitMetric.COST,
                    window_seconds=3600,
                    limit_value=Decimal("1000"),
                    applies_to=LimitAppliesTo.EACH_USER,
                )
            )
            await db.commit()
        await metered()
        per_user = await metered()

        async with session_factory() as db:
            db.add(
                LimitRule(
                    name="override",
                    scope=LimitScope.USER,
                    scope_id=seeded.user.id,
                    metric=LimitMetric.COST,
                    window_seconds=3600,
                    limit_value=Decimal("2000"),
                    overrides_each_user=True,
                )
            )
            await db.commit()
        overridden = await metered()

        print(f"\n  each-user: {per_user[0]} selects, {per_user[1]} writes")
        print(f"  each-user + override: {overridden[0]} selects, {overridden[1]} writes")
        assert overridden == per_user, (
            f"an override costs extra queries: {per_user} -> {overridden}"
        )


class TestTheAliasPath:
    """An alias costs one round trip more than the model it points at (ADR 0101).

    The existing pins in this file are unchanged by that: a direct request still
    costs five selects, because `accessible_model_by_name` deliberately does
    **not** eager-load an alias's route targets. `selectinload` on that collection
    would issue its round trip whether or not the row needed one, so a
    deployment with no aliases at all would pay for the feature on every request
    forever. Dereferencing only when the row turned out to be an alias is what
    keeps the ordinary path where it was.
    """

    async def _alias(
        self, session_factory: async_sessionmaker[AsyncSession], seeded: Seeded
    ) -> str:
        """An alias on the seeded model, granted to the seeded group."""
        from gateway.models import GroupModelAccess, ModelDef, ModelRouteTarget

        async with session_factory() as session:
            alias = ModelDef(
                name="fast",
                provider_id=None,
                upstream_model=None,
                is_alias=True,
                kind=seeded.model.kind,
                display_name="Fast",
                is_active=True,
                is_public=True,
            )
            session.add(alias)
            await session.flush()
            session.add(
                ModelRouteTarget(alias_id=alias.id, target_model_id=seeded.model.id, position=0)
            )
            session.add(GroupModelAccess(group_id=seeded.group.id, model_id=alias.id))
            await session.commit()
            return alias.name

    async def test_a_direct_model_costs_no_more_than_it_did(
        self,
        client: httpx.AsyncClient,
        seeded: Seeded,
        app: object,
        fake_upstream: FakeUpstream,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        """The pin that matters: an alias existing changes nothing for a caller
        who is not using one.

        Without this, a regression that made every request load route targets
        would still satisfy the alias pin below and only ever show up as latency.
        """
        await self._alias(session_factory, seeded)
        engine = app.state.engine  # type: ignore[attr-defined]
        fake_upstream.set_json(
            {
                "id": "c1",
                "object": "chat.completion",
                "choices": [{"index": 0, "message": {"role": "assistant", "content": "ok"}}],
                "usage": {"prompt_tokens": 10, "completion_tokens": 2, "total_tokens": 12},
            }
        )
        await client.post(
            "/v1/chat/completions",
            json={"model": seeded.model.name, "messages": [{"role": "user", "content": "hi"}]},
            headers=seeded.auth,
        )

        with counted(engine) as statements:
            response = await client.post(
                "/v1/chat/completions",
                json={
                    "model": seeded.model.name,
                    "messages": [{"role": "user", "content": "hi"}],
                },
                headers=seeded.auth,
            )
        assert response.status_code == 200
        selects = [s for s in statements if s.lstrip().upper().startswith("SELECT")]
        print(f"\n  direct model, with an alias present: {len(selects)} selects")
        print(summarise(selects))
        assert len(selects) <= 5, f"{len(selects)} selects:\n{summarise(selects)}"

    async def test_the_alias_path_costs_exactly_one_more_select(
        self,
        client: httpx.AsyncClient,
        seeded: Seeded,
        app: object,
        fake_upstream: FakeUpstream,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        """Six: the five above, plus the one that reads the target.

        The reason it is one and not three — `alias_targets` loads the targets
        joined to its provider, and `selectinload`s its prices — is that the
        request path needs all three immediately. There is no cache to make a
        second request cheaper, because a retarget has to take effect on the
        next request (ADR 0101).
        """
        alias = await self._alias(session_factory, seeded)
        engine = app.state.engine  # type: ignore[attr-defined]
        fake_upstream.set_json(
            {
                "id": "c1",
                "object": "chat.completion",
                "choices": [{"index": 0, "message": {"role": "assistant", "content": "ok"}}],
                "usage": {"prompt_tokens": 10, "completion_tokens": 2, "total_tokens": 12},
            }
        )
        await client.post(
            "/v1/chat/completions",
            json={"model": alias, "messages": [{"role": "user", "content": "hi"}]},
            headers=seeded.auth,
        )

        with counted(engine) as statements:
            response = await client.post(
                "/v1/chat/completions",
                json={"model": alias, "messages": [{"role": "user", "content": "hi"}]},
                headers=seeded.auth,
            )
        assert response.status_code == 200
        selects = [s for s in statements if s.lstrip().upper().startswith("SELECT")]
        writes = [
            s for s in statements if s.lstrip().upper().startswith(("INSERT", "UPDATE", "DELETE"))
        ]
        print(f"\n  alias path: {len(selects)} selects, {len(writes)} writes")
        print(summarise(selects))
        assert len(writes) <= 2, f"{len(writes)} writes:\n{summarise(writes)}"
        assert len(selects) <= 6, f"{len(selects)} selects:\n{summarise(selects)}"


class TestTheRoutedAliasPath:
    """An alias with several targets (issue P1) on the same budget.

    Reading the whole route is still one statement, so a routed alias without a
    session key costs what a single-target alias does. Stickiness is what adds
    work, and only for a caller who sent a session key: one select to read the
    pin, and a write when the pin is made or renewed.
    """

    async def _measure(
        self,
        client: httpx.AsyncClient,
        seeded: Seeded,
        app: object,
        fake_upstream: FakeUpstream,
        session_factory: async_sessionmaker[AsyncSession],
        *,
        headers: dict[str, str],
    ) -> tuple[list[str], list[str]]:
        from test_routing_policies import COMPLETION, make_route

        await make_route(session_factory, seeded, ("t-a", 1), ("t-b", 1))
        engine = app.state.engine  # type: ignore[attr-defined]
        fake_upstream.set_json(COMPLETION)
        body = {"model": "routed", "messages": [{"role": "user", "content": "hi"}]}
        await client.post("/v1/chat/completions", json=body, headers={**seeded.auth, **headers})
        with counted(engine) as statements:
            response = await client.post(
                "/v1/chat/completions", json=body, headers={**seeded.auth, **headers}
            )
        assert response.status_code == 200
        selects = [s for s in statements if s.lstrip().upper().startswith("SELECT")]
        writes = [
            s for s in statements if s.lstrip().upper().startswith(("INSERT", "UPDATE", "DELETE"))
        ]
        return selects, writes

    async def test_without_a_session_key_it_costs_what_a_single_target_alias_does(
        self,
        client: httpx.AsyncClient,
        seeded: Seeded,
        app: object,
        fake_upstream: FakeUpstream,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        selects, writes = await self._measure(
            client, seeded, app, fake_upstream, session_factory, headers={}
        )
        print(f"\n  routed alias, no key: {len(selects)} selects, {len(writes)} writes")
        print(summarise(selects))
        assert len(selects) <= 6, f"{len(selects)} selects:\n{summarise(selects)}"
        assert len(writes) <= 2, f"{len(writes)} writes:\n{summarise(writes)}"

    async def test_a_pinned_session_costs_one_select_more_and_no_write(
        self,
        client: httpx.AsyncClient,
        seeded: Seeded,
        app: object,
        fake_upstream: FakeUpstream,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        selects, writes = await self._measure(
            client, seeded, app, fake_upstream, session_factory, headers={"x-session-id": "s"}
        )
        print(f"\n  routed alias, pinned: {len(selects)} selects, {len(writes)} writes")
        print(summarise(selects))
        # The second request in `_measure` finds a fresh pin, so it reads it and
        # writes nothing: renewal waits for the last half of the window.
        assert len(selects) <= 7, f"{len(selects)} selects:\n{summarise(selects)}"
        assert len(writes) <= 2, f"{len(writes)} writes:\n{summarise(writes)}"


class TestTheQuotaUsagePage:
    async def test_the_user_list_costs_the_same_for_one_user_as_for_twenty(
        self,
        app: object,
        client: httpx.AsyncClient,
        seeded: Seeded,
        session: AsyncSession,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        """Counters and rules are read once per page, not once per listed user."""

        from gateway.models import QuotaReset
        from gateway.quota import QuotaSubject
        from test_admin import as_user, make_admin
        from test_quota_per_member import add_member_rule, make_member
        from test_quota_per_user import add_user_rule, one_request

        as_user(app, await make_admin(session_factory, seeded))  # type: ignore[arg-type]
        rule = await add_member_rule(session, scope_id=seeded.group.id, limit="50")
        await add_user_rule(session, limit="50")
        quota = app.state.quota_engine  # type: ignore[attr-defined]
        engine = app.state.engine  # type: ignore[attr-defined]

        reads: list[int] = []
        original = quota._totals

        async def spy(queries: object, *, now: float) -> object:
            reads.append(1)
            return await original(queries, now=now)

        quota._totals = spy

        async def page(limit: int) -> int:
            reads.clear()
            with counted(engine) as statements:
                response = await client.get("/api/admin/quota-usage", params={"limit": limit})
            assert response.status_code == 200, response.text
            assert len(reads) == 1, f"{len(reads)} counter reads for a page of {limit}"
            return len([s for s in statements if s.lstrip().upper().startswith("SELECT")])

        await page(1)  # warm anything one-off
        one = await page(1)
        for _ in range(19):
            member = await make_member(session, seeded.group)
            await one_request(quota, session, QuotaSubject(member.id, seeded.group.id))
            session.add(QuotaReset(rule_id=rule.id, user_id=member.id))
        await session.commit()
        twenty = await page(20)
        print(f"\n  /quota-usage: {one} selects for 1 user, {twenty} for 20")
        assert twenty == one, f"{one} selects for 1 user, {twenty} for 20"
