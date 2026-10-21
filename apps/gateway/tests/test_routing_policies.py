"""Routing policies end to end (issue P1): selection, fallback, stickiness.

An alias with several targets is picked from by a rule, falls back when a target
fails for its own reasons, and remembers where a session went. What these pin is
mostly *accounting*, because that is where a wrong answer is a wrong invoice: a
failed attempt leaves its own row against the target that failed, the attempt
that served is billed to the target that served, and the alias is on every row.

The fake upstream answers per **upstream model name**, so one test can have
three targets behave three ways.
"""

from __future__ import annotations

import asyncio
import random
from collections.abc import Callable
from datetime import timedelta
from decimal import Decimal
from typing import Any

import httpx
import orjson
import pytest
from conftest import FakeUpstream, Seeded
from gateway.models import (
    GroupModelAccess,
    LimitMetric,
    LimitRule,
    LimitScope,
    ModelDef,
    ModelKind,
    ModelPrice,
    ModelRouteTarget,
    Provider,
    RouteSession,
    UsageRecord,
    UsageStatus,
)
from gateway.types import utcnow
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

COMPLETION = {
    "id": "c1",
    "object": "chat.completion",
    "model": "upstream/whatever",
    "choices": [{"index": 0, "message": {"role": "assistant", "content": "ok"}}],
    "usage": {"prompt_tokens": 10, "completion_tokens": 2, "total_tokens": 12},
}
ERROR = {"error": {"message": "nope", "type": "api_error"}}

Outcome = int | str | Callable[[httpx.Request], httpx.Response]


def chat(model: str = "routed", **extra: Any) -> dict[str, Any]:
    return {"model": model, "messages": [{"role": "user", "content": "hello there"}], **extra}


class Upstreams:
    """Per-upstream-model behaviour, with a record of who was called and when.

    An outcome is an HTTP status, ``"connect"``/``"timeout"`` for a transport
    failure, or a responder. A list is consumed one call at a time and then the
    last entry repeats, so "fails once, then recovers" is ``[503, 200]``.
    """

    def __init__(self, fake: FakeUpstream) -> None:
        self.calls: list[str] = []
        self._plans: dict[str, list[Outcome]] = {}
        fake._responder = self._respond

    def set(self, upstream_model: str, *outcomes: Outcome) -> None:
        self._plans[upstream_model] = list(outcomes)

    def _respond(self, request: httpx.Request) -> httpx.Response:
        model = orjson.loads(request.content)["model"]
        self.calls.append(model)
        plan = self._plans.get(model, [200])
        outcome = plan.pop(0) if len(plan) > 1 else plan[0]
        if callable(outcome):
            return outcome(request)
        if outcome == "connect":
            raise httpx.ConnectError("refused", request=request)
        if outcome == "timeout":
            raise httpx.ReadTimeout("slow", request=request)
        assert isinstance(outcome, int)
        if outcome == 200:
            return httpx.Response(200, json=COMPLETION)
        return httpx.Response(outcome, json=ERROR)


@pytest.fixture
def upstreams(fake_upstream: FakeUpstream) -> Upstreams:
    return Upstreams(fake_upstream)


async def make_route(
    session_factory: async_sessionmaker[AsyncSession],
    seeded: Seeded,
    *targets: tuple[str, int] | tuple[str, int, str],
    name: str = "routed",
    rule: str = "first_available",
    ttl: int = 3600,
    kind: ModelKind = ModelKind.CHAT,
    prices: dict[str, tuple[str, str]] | None = None,
) -> ModelDef:
    """An alias onto fresh models ``(name, weight[, upstream])``, granted to the group.

    The targets are **not** granted to the group: access is on the alias, which
    is what makes a routed target reachable only through the name.
    """
    async with session_factory() as session:
        alias = ModelDef(
            name=name,
            provider_id=None,
            upstream_model=None,
            is_alias=True,
            kind=kind,
            is_active=True,
            route_rule=rule,
            route_sticky_ttl_seconds=ttl,
        )
        session.add(alias)
        await session.flush()
        for position, target in enumerate(targets):
            target_name, weight = target[0], target[1]
            upstream = target[2] if len(target) == 3 else f"up-{target_name}"  # type: ignore[misc]
            model = ModelDef(
                name=target_name,
                upstream_model=upstream,
                provider_id=seeded.provider.id,
                kind=kind,
            )
            session.add(model)
            await session.flush()
            inp, out = (prices or {}).get(target_name, ("1", "2"))
            session.add(
                ModelPrice(
                    model_id=model.id,
                    input_per_mtok=Decimal(inp),
                    output_per_mtok=Decimal(out),
                    currency="EUR",
                )
            )
            session.add(
                ModelRouteTarget(
                    alias_id=alias.id,
                    target_model_id=model.id,
                    position=position,
                    weight=weight,
                )
            )
        session.add(GroupModelAccess(group_id=seeded.group.id, model_id=alias.id))
        await session.commit()
        return alias


async def ledger(session_factory: async_sessionmaker[AsyncSession]) -> list[UsageRecord]:
    async with session_factory() as session:
        rows = (
            (
                await session.execute(
                    select(UsageRecord).order_by(UsageRecord.created_at, UsageRecord.route_attempt)
                )
            )
            .scalars()
            .all()
        )
        return list(rows)


async def pins(session_factory: async_sessionmaker[AsyncSession]) -> list[RouteSession]:
    async with session_factory() as session:
        return list((await session.execute(select(RouteSession))).scalars().all())


async def post(
    client: httpx.AsyncClient, seeded: Seeded, body: dict[str, Any], **headers: str
) -> httpx.Response:
    return await client.post("/v1/chat/completions", json=body, headers={**seeded.auth, **headers})


# --------------------------------------------------------------------------
# selection
# --------------------------------------------------------------------------


class TestSelection:
    async def test_first_available_serves_from_the_first_target(
        self, client: httpx.AsyncClient, seeded: Seeded, session_factory: Any, upstreams: Upstreams
    ) -> None:
        await make_route(session_factory, seeded, ("t-a", 1), ("t-b", 1))
        response = await post(client, seeded, chat())
        assert response.status_code == 200, response.text
        assert upstreams.calls == ["up-t-a"]
        # The caller is told the name they sent, never a target's.
        assert response.json()["model"] == "routed"

    async def test_an_unavailable_first_target_is_skipped_without_an_attempt(
        self, client: httpx.AsyncClient, seeded: Seeded, session_factory: Any, upstreams: Upstreams
    ) -> None:
        await make_route(session_factory, seeded, ("t-a", 1), ("t-b", 1))
        async with session_factory() as session:
            a = (await session.execute(select(ModelDef).where(ModelDef.name == "t-a"))).scalar_one()
            a.is_active = False
            await session.commit()
        response = await post(client, seeded, chat())
        assert response.status_code == 200
        assert upstreams.calls == ["up-t-b"]
        rows = await ledger(session_factory)
        assert [r.model_name for r in rows] == ["t-b"]

    async def test_every_target_unavailable_is_the_alias_503(
        self, client: httpx.AsyncClient, seeded: Seeded, session_factory: Any, upstreams: Upstreams
    ) -> None:
        await make_route(session_factory, seeded, ("t-a", 1), ("t-b", 1))
        async with session_factory() as session:
            for model in (
                await session.execute(select(ModelDef).where(ModelDef.name.like("t-%")))
            ).scalars():
                model.is_active = False
            await session.commit()
        response = await post(client, seeded, chat())
        assert response.status_code == 503
        assert response.json()["error"]["code"] == "alias_unavailable"
        assert upstreams.calls == []

    async def test_cheapest_picks_the_lowest_list_price(
        self, client: httpx.AsyncClient, seeded: Seeded, session_factory: Any, upstreams: Upstreams
    ) -> None:
        await make_route(
            session_factory,
            seeded,
            ("t-dear", 1),
            ("t-cheap", 1),
            rule="cheapest",
            prices={"t-dear": ("10", "30"), "t-cheap": ("1", "2")},
        )
        assert (await post(client, seeded, chat())).status_code == 200
        assert upstreams.calls == ["up-t-cheap"]

    async def test_weighted_follows_the_draw(
        self,
        client: httpx.AsyncClient,
        seeded: Seeded,
        session_factory: Any,
        upstreams: Upstreams,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """The draw is seeded, so this is a fixed outcome rather than a statistic.

        Both orders are exercised by running two seeds and checking each against
        the same draw made directly on the rule.
        """
        from gateway.routers import _metered
        from gateway.routing import Candidate, SelectionContext, Weighted

        await make_route(session_factory, seeded, ("t-a", 1), ("t-b", 1), rule="weighted")
        for seed in (1, 2, 3, 4):
            upstreams.calls.clear()

            class Seeded_(random.Random):
                def __init__(self, _seed: int = seed) -> None:
                    super().__init__(_seed)

            monkeypatch.setattr(_metered.random, "Random", Seeded_)
            assert (await post(client, seeded, chat())).status_code == 200

            async with session_factory() as session:
                rows = (
                    (
                        await session.execute(
                            select(ModelDef).where(ModelDef.name.in_(["t-a", "t-b"]))
                        )
                    )
                    .scalars()
                    .all()
                )
                by_name = {m.name: m for m in rows}
            expected = await Weighted().order(
                [Candidate(by_name["t-a"], 0), Candidate(by_name["t-b"], 1)],
                SelectionContext(
                    alias=by_name["t-a"],
                    surface=_metered.ApiSurface.CHAT_COMPLETIONS,
                    rng=random.Random(seed),
                ),
            )
            assert upstreams.calls == [f"up-{expected[0].target.name}"]

    async def test_a_routed_alias_is_listed_once(
        self, client: httpx.AsyncClient, seeded: Seeded, session_factory: Any, upstreams: Upstreams
    ) -> None:
        await make_route(session_factory, seeded, ("t-a", 1), ("t-b", 1), ("t-c", 1))
        listing = (await client.get("/v1/models", headers=seeded.auth)).json()["data"]
        assert [m["id"] for m in listing].count("routed") == 1
        assert (await client.get("/v1/models/routed", headers=seeded.auth)).status_code == 200

    async def test_a_single_target_alias_writes_no_attempt_number(
        self, client: httpx.AsyncClient, seeded: Seeded, session_factory: Any, upstreams: Upstreams
    ) -> None:
        await make_route(session_factory, seeded, ("t-a", 1))
        assert (await post(client, seeded, chat(), **{"x-session-id": "s"})).status_code == 200
        rows = await ledger(session_factory)
        assert [r.route_attempt for r in rows] == [None]
        assert await pins(session_factory) == []


# --------------------------------------------------------------------------
# fallback
# --------------------------------------------------------------------------


class TestFallbackMatrix:
    @pytest.mark.parametrize(
        "failure",
        [500, 502, 503, 504, 529, 429, 408, "connect", "timeout"],
        ids=lambda v: str(v),
    )
    async def test_provider_side_failures_fall_back_and_bill_the_target_that_served(
        self,
        failure: Outcome,
        client: httpx.AsyncClient,
        seeded: Seeded,
        session_factory: Any,
        upstreams: Upstreams,
    ) -> None:
        await make_route(session_factory, seeded, ("t-a", 1), ("t-b", 1))
        upstreams.set("up-t-a", failure)
        response = await post(client, seeded, chat())
        assert response.status_code == 200, response.text
        assert response.json()["model"] == "routed"
        assert upstreams.calls == ["up-t-a", "up-t-b"]

        failed, served = await ledger(session_factory)
        assert (failed.model_name, failed.status, failed.route_attempt) == (
            "t-a",
            UsageStatus.UPSTREAM_ERROR,
            1,
        )
        assert (served.model_name, served.status, served.route_attempt) == (
            "t-b",
            UsageStatus.COMPLETED,
            2,
        )
        # The alias is on both rows, and the failed attempt cost nothing.
        assert failed.requested_model == served.requested_model == "routed"
        assert failed.cost == 0
        assert served.cost > 0
        assert failed.request_id == served.request_id

    @pytest.mark.parametrize("status", [400, 401, 402, 403, 404, 409, 413, 422])
    async def test_failures_the_request_caused_are_the_answer_not_a_reason_to_retry(
        self,
        status: int,
        client: httpx.AsyncClient,
        seeded: Seeded,
        session_factory: Any,
        upstreams: Upstreams,
    ) -> None:
        await make_route(session_factory, seeded, ("t-a", 1), ("t-b", 1))
        upstreams.set("up-t-a", status)
        response = await post(client, seeded, chat())
        assert response.status_code == status
        assert upstreams.calls == ["up-t-a"]
        rows = await ledger(session_factory)
        assert [(r.model_name, r.status) for r in rows] == [("t-a", UsageStatus.UPSTREAM_ERROR)]

    async def test_when_every_target_fails_the_caller_gets_the_last_failure(
        self, client: httpx.AsyncClient, seeded: Seeded, session_factory: Any, upstreams: Upstreams
    ) -> None:
        await make_route(session_factory, seeded, ("t-a", 1), ("t-b", 1))
        upstreams.set("up-t-a", 502)
        upstreams.set("up-t-b", 503)
        response = await post(client, seeded, chat())
        assert response.status_code == 503
        rows = await ledger(session_factory)
        assert [(r.model_name, r.route_attempt, r.status) for r in rows] == [
            ("t-a", 1, UsageStatus.UPSTREAM_ERROR),
            ("t-b", 2, UsageStatus.UPSTREAM_ERROR),
        ]

    async def test_when_every_target_is_unreachable_it_is_the_upstream_503(
        self, client: httpx.AsyncClient, seeded: Seeded, session_factory: Any, upstreams: Upstreams
    ) -> None:
        await make_route(session_factory, seeded, ("t-a", 1), ("t-b", 1))
        upstreams.set("up-t-a", "connect")
        upstreams.set("up-t-b", "timeout")
        response = await post(client, seeded, chat())
        assert response.status_code == 502 or response.status_code == 503
        assert len(upstreams.calls) == 2

    async def test_attempts_are_capped(
        self, client: httpx.AsyncClient, seeded: Seeded, session_factory: Any, upstreams: Upstreams
    ) -> None:
        await make_route(
            session_factory, seeded, ("t-a", 1), ("t-b", 1), ("t-c", 1), ("t-d", 1), ("t-e", 1)
        )
        for name in ("a", "b", "c", "d", "e"):
            upstreams.set(f"up-t-{name}", 503)
        response = await post(client, seeded, chat())
        assert response.status_code == 503
        assert upstreams.calls == ["up-t-a", "up-t-b", "up-t-c"]

    async def test_a_direct_model_never_falls_back_and_has_no_attempt_number(
        self, client: httpx.AsyncClient, seeded: Seeded, session_factory: Any, upstreams: Upstreams
    ) -> None:
        upstreams.set("upstream/test", 503)
        async with session_factory() as session:
            model = (
                await session.execute(select(ModelDef).where(ModelDef.name == "test-model"))
            ).scalar_one()
            upstream_name = model.upstream_model
        assert upstream_name is not None
        upstreams.set(upstream_name, 503)
        response = await post(client, seeded, chat("test-model"))
        assert response.status_code == 503
        assert upstreams.calls == [upstream_name]
        assert [r.route_attempt for r in await ledger(session_factory)] == [None]


# --------------------------------------------------------------------------
# quota admission, per attempt
# --------------------------------------------------------------------------


def refuse_calls(app: Any, monkeypatch: pytest.MonkeyPatch, *which: int) -> list[int]:
    """Make the quota engine refuse the n-th admission (1-based) and admit the rest.

    The shipped rules cannot name a target (a rule's scope is global, group, user
    or key), so two attempts of one request face the same counters and the same
    answer. A refusal that differs per attempt is exactly what a rule that names
    a target will produce later, and what a counter moving between attempts can
    produce now; forcing it here is how the per-attempt branch is reached.
    Returns the list of admission numbers seen, so a test can say how many
    attempts reached admission.
    """
    from gateway.quota import QuotaExceeded
    from gateway.quota.engine import Violation

    engine = app.state.quota_engine
    real = engine.check_and_reserve
    seen: list[int] = []

    async def check_and_reserve(*args: Any, **kwargs: Any) -> Any:
        seen.append(len(seen) + 1)
        if seen[-1] in which:
            rule = LimitRule(
                name="named target",
                scope=LimitScope.GLOBAL,
                metric=LimitMetric.COST,
                window_seconds=3600,
                limit_value=Decimal(1),
            )
            raise QuotaExceeded([Violation(rule, Decimal(1), Decimal(1), 42)])
        return await real(*args, **kwargs)

    monkeypatch.setattr(engine, "check_and_reserve", check_and_reserve)
    return seen


class TestAdmissionPerAttempt:
    async def test_an_attempt_that_is_not_admitted_moves_on_and_writes_no_row(
        self,
        app: Any,
        monkeypatch: pytest.MonkeyPatch,
        client: httpx.AsyncClient,
        seeded: Seeded,
        session_factory: Any,
        upstreams: Upstreams,
    ) -> None:
        await make_route(session_factory, seeded, ("t-a", 1), ("t-b", 1))
        seen = refuse_calls(app, monkeypatch, 1)
        response = await post(client, seeded, chat())
        assert response.status_code == 200, response.text
        assert seen == [1, 2]
        assert upstreams.calls == ["up-t-b"]
        rows = await ledger(session_factory)
        # A quota refusal writes no row (AGENTS.md); the served attempt is
        # numbered 2 because the refused one was attempt 1.
        assert [(r.model_name, r.route_attempt) for r in rows] == [("t-b", 2)]

    async def test_when_no_attempt_is_admitted_the_caller_gets_the_429(
        self,
        app: Any,
        monkeypatch: pytest.MonkeyPatch,
        client: httpx.AsyncClient,
        seeded: Seeded,
        session_factory: Any,
        upstreams: Upstreams,
    ) -> None:
        await make_route(session_factory, seeded, ("t-a", 1), ("t-b", 1))
        refuse_calls(app, monkeypatch, 1, 2)
        response = await post(client, seeded, chat())
        assert response.status_code == 429
        assert response.json()["error"]["code"] == "quota_exceeded"
        assert response.headers["retry-after"] == "42"
        assert upstreams.calls == []
        assert await ledger(session_factory) == []

    async def test_a_refusal_then_an_upstream_failure_is_the_upstream_failure(
        self,
        app: Any,
        monkeypatch: pytest.MonkeyPatch,
        client: httpx.AsyncClient,
        seeded: Seeded,
        session_factory: Any,
        upstreams: Upstreams,
    ) -> None:
        await make_route(session_factory, seeded, ("t-a", 1), ("t-b", 1), ("t-c", 1))
        refuse_calls(app, monkeypatch, 1)
        upstreams.set("up-t-b", 503)
        upstreams.set("up-t-c", 502)
        response = await post(client, seeded, chat())
        assert response.status_code == 502
        assert upstreams.calls == ["up-t-b", "up-t-c"]

    async def test_a_request_counted_quota_counts_every_attempt(
        self, client: httpx.AsyncClient, seeded: Seeded, session_factory: Any, upstreams: Upstreams
    ) -> None:
        """Each attempt is an upstream call and a ledger row, so a requests ceiling
        counts it: the counters are rebuilt from those rows, and a rule the
        ledger cannot reproduce would drift on every restart. With room for two,
        a failover serves; with room for one, the second attempt is refused."""
        await make_route(session_factory, seeded, ("t-a", 1), ("t-b", 1))
        async with session_factory() as session:
            session.add(
                LimitRule(
                    name="two requests",
                    scope=LimitScope.GROUP,
                    scope_id=seeded.group.id,
                    metric=LimitMetric.REQUESTS,
                    window_seconds=3600,
                    limit_value=Decimal(2),
                )
            )
            await session.commit()
        upstreams.set("up-t-a", 503)
        assert (await post(client, seeded, chat())).status_code == 200
        # The window is now full: the next request is refused outright.
        assert (await post(client, seeded, chat())).status_code == 429

    async def test_a_failed_attempt_hands_its_reservation_back(
        self, client: httpx.AsyncClient, seeded: Seeded, session_factory: Any, upstreams: Upstreams
    ) -> None:
        """A failed attempt settles at what it used, which is no tokens and no cost."""
        await make_route(
            session_factory,
            seeded,
            ("t-a", 1),
            ("t-b", 1),
            prices={"t-a": ("1000", "1000"), "t-b": ("1000", "1000")},
        )
        upstreams.set("up-t-a", 503)
        assert (await post(client, seeded, chat())).status_code == 200
        failed, served = await ledger(session_factory)
        assert failed.total_tokens == 0
        assert failed.cost == 0
        assert served.total_tokens == 12


# --------------------------------------------------------------------------
# stickiness
# --------------------------------------------------------------------------


class TestStickiness:
    async def test_a_session_stays_on_the_target_it_fell_back_to(
        self, client: httpx.AsyncClient, seeded: Seeded, session_factory: Any, upstreams: Upstreams
    ) -> None:
        await make_route(session_factory, seeded, ("t-a", 1), ("t-b", 1))
        upstreams.set("up-t-a", 503, 200)  # down once, then healthy again
        first = await post(client, seeded, chat(), **{"x-session-id": "conv-1"})
        assert first.status_code == 200
        assert upstreams.calls == ["up-t-a", "up-t-b"]

        upstreams.calls.clear()
        again = await post(client, seeded, chat(), **{"x-session-id": "conv-1"})
        assert again.status_code == 200
        # A is healthy and first in the operator's order, and the session still
        # goes to B: that is what sticking means.
        assert upstreams.calls == ["up-t-b"]

        # A different session is not held by it.
        upstreams.calls.clear()
        other = await post(client, seeded, chat(), **{"x-session-id": "conv-2"})
        assert other.status_code == 200
        assert upstreams.calls == ["up-t-a"]

    async def test_a_request_without_a_key_is_never_pinned(
        self, client: httpx.AsyncClient, seeded: Seeded, session_factory: Any, upstreams: Upstreams
    ) -> None:
        await make_route(session_factory, seeded, ("t-a", 1), ("t-b", 1))
        upstreams.set("up-t-a", 503, 200)
        assert (await post(client, seeded, chat())).status_code == 200
        assert await pins(session_factory) == []
        upstreams.calls.clear()
        assert (await post(client, seeded, chat())).status_code == 200
        assert upstreams.calls == ["up-t-a"]

    @pytest.mark.parametrize(
        ("headers", "body_extra"),
        [
            ({"x-session-id": "k"}, {}),
            ({"x-session-affinity": "k"}, {}),
            ({}, {"prompt_cache_key": "k"}),
            ({}, {"user": "k"}),
        ],
        ids=["x-session-id", "x-session-affinity", "prompt_cache_key", "user"],
    )
    async def test_every_documented_key_pins(
        self,
        headers: dict[str, str],
        body_extra: dict[str, Any],
        client: httpx.AsyncClient,
        seeded: Seeded,
        session_factory: Any,
        upstreams: Upstreams,
    ) -> None:
        await make_route(session_factory, seeded, ("t-a", 1), ("t-b", 1))
        upstreams.set("up-t-a", 503, 200)
        assert (await post(client, seeded, chat(**body_extra), **headers)).status_code == 200
        upstreams.calls.clear()
        assert (await post(client, seeded, chat(**body_extra), **headers)).status_code == 200
        assert upstreams.calls == ["up-t-b"]

    async def test_the_stored_key_is_a_hash_not_the_session_id(
        self, client: httpx.AsyncClient, seeded: Seeded, session_factory: Any, upstreams: Upstreams
    ) -> None:
        await make_route(session_factory, seeded, ("t-a", 1), ("t-b", 1))
        assert (
            await post(client, seeded, chat(), **{"x-session-id": "my-secret-conversation"})
        ).status_code == 200
        (row,) = await pins(session_factory)
        assert "my-secret-conversation" not in row.key_hash
        assert len(row.key_hash) == 64

    async def test_the_pin_expires(
        self, client: httpx.AsyncClient, seeded: Seeded, session_factory: Any, upstreams: Upstreams
    ) -> None:
        await make_route(session_factory, seeded, ("t-a", 1), ("t-b", 1))
        upstreams.set("up-t-a", 503, 200)
        await post(client, seeded, chat(), **{"x-session-id": "s"})
        async with session_factory() as session:
            row = (await session.execute(select(RouteSession))).scalar_one()
            row.expires_at = utcnow() - timedelta(seconds=1)
            await session.commit()

        upstreams.calls.clear()
        assert (await post(client, seeded, chat(), **{"x-session-id": "s"})).status_code == 200
        # Expired: the rule decides again, and A is first.
        assert upstreams.calls == ["up-t-a"]
        # And the session is now pinned to A.
        (row,) = await pins(session_factory)
        async with session_factory() as session:
            a = (await session.execute(select(ModelDef).where(ModelDef.name == "t-a"))).scalar_one()
        assert row.target_model_id == a.id

    async def test_the_pin_is_dropped_and_remade_when_the_pinned_target_fails(
        self, client: httpx.AsyncClient, seeded: Seeded, session_factory: Any, upstreams: Upstreams
    ) -> None:
        await make_route(session_factory, seeded, ("t-a", 1), ("t-b", 1))
        upstreams.set("up-t-a", 503, 200)
        await post(client, seeded, chat(), **{"x-session-id": "s"})  # falls to B, pinned to B
        # B now fails; A has recovered.
        upstreams.set("up-t-b", 503)
        upstreams.calls.clear()
        response = await post(client, seeded, chat(), **{"x-session-id": "s"})
        assert response.status_code == 200
        assert upstreams.calls == ["up-t-b", "up-t-a"]
        (row,) = await pins(session_factory)
        async with session_factory() as session:
            a = (await session.execute(select(ModelDef).where(ModelDef.name == "t-a"))).scalar_one()
        assert row.target_model_id == a.id

    async def test_a_pinned_target_that_is_no_longer_available_is_passed_over(
        self, client: httpx.AsyncClient, seeded: Seeded, session_factory: Any, upstreams: Upstreams
    ) -> None:
        await make_route(session_factory, seeded, ("t-a", 1), ("t-b", 1))
        upstreams.set("up-t-a", 503, 200)
        await post(client, seeded, chat(), **{"x-session-id": "s"})  # pinned to B
        async with session_factory() as session:
            b = (await session.execute(select(ModelDef).where(ModelDef.name == "t-b"))).scalar_one()
            b.is_active = False
            await session.commit()
        upstreams.calls.clear()
        assert (await post(client, seeded, chat(), **{"x-session-id": "s"})).status_code == 200
        assert upstreams.calls == ["up-t-a"]

    async def test_a_pinned_target_that_is_not_admitted_is_passed_over(
        self,
        app: Any,
        monkeypatch: pytest.MonkeyPatch,
        client: httpx.AsyncClient,
        seeded: Seeded,
        session_factory: Any,
        upstreams: Upstreams,
    ) -> None:
        await make_route(session_factory, seeded, ("t-a", 1), ("t-b", 1))
        upstreams.set("up-t-a", 503, 200)
        await post(client, seeded, chat(), **{"x-session-id": "s"})  # pinned to B
        upstreams.calls.clear()
        # B (the pin) is tried first and refused at admission; A serves and the
        # session is re-pinned to it.
        refuse_calls(app, monkeypatch, 1)
        response = await post(client, seeded, chat(), **{"x-session-id": "s"})
        assert response.status_code == 200, response.text
        assert upstreams.calls == ["up-t-a"]
        (row,) = await pins(session_factory)
        async with session_factory() as session:
            a = (await session.execute(select(ModelDef).where(ModelDef.name == "t-a"))).scalar_one()
        assert row.target_model_id == a.id

    async def test_a_client_error_does_not_pin(
        self, client: httpx.AsyncClient, seeded: Seeded, session_factory: Any, upstreams: Upstreams
    ) -> None:
        await make_route(session_factory, seeded, ("t-a", 1), ("t-b", 1))
        upstreams.set("up-t-a", 400)
        assert (await post(client, seeded, chat(), **{"x-session-id": "s"})).status_code == 400
        assert await pins(session_factory) == []

    async def test_ttl_zero_turns_stickiness_off(
        self, client: httpx.AsyncClient, seeded: Seeded, session_factory: Any, upstreams: Upstreams
    ) -> None:
        await make_route(session_factory, seeded, ("t-a", 1), ("t-b", 1), ttl=0)
        upstreams.set("up-t-a", 503, 200)
        await post(client, seeded, chat(), **{"x-session-id": "s"})
        assert await pins(session_factory) == []
        upstreams.calls.clear()
        await post(client, seeded, chat(), **{"x-session-id": "s"})
        assert upstreams.calls == ["up-t-a"]

    async def test_a_busy_session_does_not_write_on_every_request(
        self, client: httpx.AsyncClient, seeded: Seeded, session_factory: Any, upstreams: Upstreams
    ) -> None:
        await make_route(session_factory, seeded, ("t-a", 1), ("t-b", 1))
        await post(client, seeded, chat(), **{"x-session-id": "s"})
        (first,) = await pins(session_factory)
        await post(client, seeded, chat(), **{"x-session-id": "s"})
        (second,) = await pins(session_factory)
        assert second.updated_at == first.updated_at

    async def test_deleting_a_target_forgets_the_sessions_pinned_to_it(
        self, client: httpx.AsyncClient, seeded: Seeded, session_factory: Any, upstreams: Upstreams
    ) -> None:
        await make_route(session_factory, seeded, ("t-a", 1), ("t-b", 1))
        await post(client, seeded, chat(), **{"x-session-id": "s"})
        assert len(await pins(session_factory)) == 1
        async with session_factory() as session:
            a = (await session.execute(select(ModelDef).where(ModelDef.name == "t-a"))).scalar_one()
            await session.delete(a)
            await session.commit()
        assert await pins(session_factory) == []


# --------------------------------------------------------------------------
# streaming
# --------------------------------------------------------------------------


def sse_frames() -> list[bytes]:
    chunk = {
        "id": "s1",
        "object": "chat.completion.chunk",
        "model": "upstream/x",
        "choices": [{"index": 0, "delta": {"content": "hi"}}],
    }
    usage = {
        "id": "s1",
        "object": "chat.completion.chunk",
        "model": "upstream/x",
        "choices": [],
        "usage": {"prompt_tokens": 3, "completion_tokens": 1, "total_tokens": 4},
    }
    return [
        b"data: " + orjson.dumps(chunk) + b"\n\n",
        b"data: " + orjson.dumps(usage) + b"\n\n",
        b"data: [DONE]\n\n",
    ]


def streaming(
    frames: list[bytes], *, then_fail: bool = False
) -> Callable[[httpx.Request], httpx.Response]:
    def responder(_: httpx.Request) -> httpx.Response:
        async def body() -> Any:
            for frame in frames:
                yield frame
            if then_fail:
                raise httpx.ReadError("connection reset")

        return httpx.Response(200, content=body(), headers={"content-type": "text/event-stream"})

    return responder


class TestStreaming:
    async def test_an_error_before_the_first_byte_falls_back(
        self, client: httpx.AsyncClient, seeded: Seeded, session_factory: Any, upstreams: Upstreams
    ) -> None:
        await make_route(session_factory, seeded, ("t-a", 1), ("t-b", 1))
        upstreams.set("up-t-a", 503)
        upstreams.set("up-t-b", streaming(sse_frames()))
        response = await post(client, seeded, chat(stream=True))
        assert response.status_code == 200
        assert "hi" in response.text
        assert '"model":"routed"' in response.text.replace(" ", "")
        assert upstreams.calls == ["up-t-a", "up-t-b"]
        failed, served = await ledger(session_factory)
        assert (failed.model_name, failed.status) == ("t-a", UsageStatus.UPSTREAM_ERROR)
        assert (served.model_name, served.status) == ("t-b", UsageStatus.COMPLETED)
        assert served.streamed

    async def test_a_connection_that_fails_to_open_falls_back(
        self, client: httpx.AsyncClient, seeded: Seeded, session_factory: Any, upstreams: Upstreams
    ) -> None:
        await make_route(session_factory, seeded, ("t-a", 1), ("t-b", 1))
        upstreams.set("up-t-a", "connect")
        upstreams.set("up-t-b", streaming(sse_frames()))
        response = await post(client, seeded, chat(stream=True))
        assert response.status_code == 200
        assert upstreams.calls == ["up-t-a", "up-t-b"]

    async def test_a_failure_after_the_first_byte_never_falls_back(
        self, client: httpx.AsyncClient, seeded: Seeded, session_factory: Any, upstreams: Upstreams
    ) -> None:
        await make_route(session_factory, seeded, ("t-a", 1), ("t-b", 1))
        upstreams.set("up-t-a", streaming(sse_frames()[:1], then_fail=True))
        # The in-process transport buffers the body, so the reset surfaces as an
        # exception on the client; what matters is on the server side.
        with pytest.raises(httpx.ReadError):
            await post(client, seeded, chat(stream=True))
        # The upstream had already answered 200 and sent a frame, so the answer
        # was committed to A. B was never asked to continue it: a second model's
        # text spliced into a half-sent answer is not an answer.
        assert upstreams.calls == ["up-t-a"]
        await asyncio.sleep(0.3)  # the detached finaliser
        rows = await ledger(session_factory)
        assert [(r.model_name, r.route_attempt) for r in rows] == [("t-a", 1)]
        assert rows[0].status != UsageStatus.COMPLETED

    async def test_a_stream_pins_the_session_once_it_has_opened(
        self, client: httpx.AsyncClient, seeded: Seeded, session_factory: Any, upstreams: Upstreams
    ) -> None:
        await make_route(session_factory, seeded, ("t-a", 1), ("t-b", 1))
        upstreams.set("up-t-a", 503, 200)
        upstreams.set("up-t-b", streaming(sse_frames()))
        await post(client, seeded, chat(stream=True), **{"x-session-id": "s"})
        (row,) = await pins(session_factory)
        async with session_factory() as session:
            b = (await session.execute(select(ModelDef).where(ModelDef.name == "t-b"))).scalar_one()
        assert row.target_model_id == b.id


# --------------------------------------------------------------------------
# the other surfaces share the wrapper
# --------------------------------------------------------------------------


class TestOtherSurfaces:
    async def test_messages_falls_back(
        self, client: httpx.AsyncClient, seeded: Seeded, session_factory: Any, upstreams: Upstreams
    ) -> None:
        await make_route(session_factory, seeded, ("t-a", 1), ("t-b", 1))
        anthropic = {
            "id": "m1",
            "type": "message",
            "role": "assistant",
            "model": "up",
            "content": [{"type": "text", "text": "ok"}],
            "usage": {"input_tokens": 5, "output_tokens": 2},
        }
        upstreams.set("up-t-a", 503)
        upstreams.set("up-t-b", lambda _: httpx.Response(200, json=anthropic))
        response = await client.post(
            "/v1/messages",
            json={
                "model": "routed",
                "max_tokens": 50,
                "messages": [{"role": "user", "content": "hello"}],
            },
            headers=seeded.auth,
        )
        assert response.status_code == 200, response.text
        assert response.json()["model"] == "routed"
        assert upstreams.calls == ["up-t-a", "up-t-b"]

    async def test_responses_falls_back(
        self, client: httpx.AsyncClient, seeded: Seeded, session_factory: Any, upstreams: Upstreams
    ) -> None:
        await make_route(session_factory, seeded, ("t-a", 1), ("t-b", 1))
        body = {
            "id": "r1",
            "object": "response",
            "model": "up",
            "output": [
                {
                    "type": "message",
                    "role": "assistant",
                    "content": [{"type": "output_text", "text": "ok"}],
                }
            ],
            "usage": {"input_tokens": 5, "output_tokens": 2, "total_tokens": 7},
        }
        upstreams.set("up-t-a", 502)
        upstreams.set("up-t-b", lambda _: httpx.Response(200, json=body))
        response = await client.post(
            "/v1/responses", json={"model": "routed", "input": "hello"}, headers=seeded.auth
        )
        assert response.status_code == 200, response.text
        assert upstreams.calls == ["up-t-a", "up-t-b"]

    async def test_embeddings_falls_back(
        self, client: httpx.AsyncClient, seeded: Seeded, session_factory: Any, upstreams: Upstreams
    ) -> None:
        await make_route(session_factory, seeded, ("e-a", 1), ("e-b", 1), kind=ModelKind.EMBEDDING)
        body = {
            "object": "list",
            "model": "up",
            "data": [{"object": "embedding", "index": 0, "embedding": [0.1]}],
            "usage": {"prompt_tokens": 3, "total_tokens": 3},
        }
        upstreams.set("up-e-a", "connect")
        upstreams.set("up-e-b", lambda _: httpx.Response(200, json=body))
        response = await client.post(
            "/v1/embeddings", json={"model": "routed", "input": "hello"}, headers=seeded.auth
        )
        assert response.status_code == 200, response.text
        assert upstreams.calls == ["up-e-a", "up-e-b"]

    async def test_images_falls_back(
        self, client: httpx.AsyncClient, seeded: Seeded, session_factory: Any, upstreams: Upstreams
    ) -> None:
        await make_route(session_factory, seeded, ("i-a", 1), ("i-b", 1), kind=ModelKind.IMAGE)
        body = {"created": 1, "data": [{"b64_json": "aaa"}]}
        upstreams.set("up-i-a", 503)
        upstreams.set("up-i-b", lambda _: httpx.Response(200, json=body))
        response = await client.post(
            "/v1/images/generations",
            json={"model": "routed", "prompt": "a cat"},
            headers=seeded.auth,
        )
        assert response.status_code == 200, response.text
        assert upstreams.calls == ["up-i-a", "up-i-b"]


async def test_an_unreadable_provider_is_a_failed_attempt_not_a_failed_request(
    client: httpx.AsyncClient,
    seeded: Seeded,
    session_factory: Any,
    upstreams: Upstreams,
) -> None:
    """A target whose provider credential cannot be read is the operator's fault."""
    await make_route(session_factory, seeded, ("t-a", 1), ("t-b", 1))
    async with session_factory() as session:
        broken = Provider(
            name="broken",
            base_url=seeded.provider.base_url,
            api_key_encrypted="not-a-ciphertext",
        )
        session.add(broken)
        await session.flush()
        a = (await session.execute(select(ModelDef).where(ModelDef.name == "t-a"))).scalar_one()
        a.provider_id = broken.id
        await session.commit()
    response = await post(client, seeded, chat())
    assert response.status_code == 200, response.text
    assert upstreams.calls == ["up-t-b"]


class TestRequestLog:
    """The log says which target served, and which tries failed (issue P1)."""

    async def test_a_fallback_shows_one_line_per_attempt_in_json_and_csv(
        self,
        app: Any,
        client: httpx.AsyncClient,
        seeded: Seeded,
        session_factory: Any,
        upstreams: Upstreams,
    ) -> None:
        import csv
        import io

        from test_admin import as_user, make_admin

        await make_route(session_factory, seeded, ("t-a", 1), ("t-b", 1))
        upstreams.set("up-t-a", 503)
        assert (await post(client, seeded, chat())).status_code == 200

        as_user(app, await make_admin(session_factory, seeded))
        items = (await client.get("/api/admin/requests")).json()["items"]
        by_attempt = {item["route_attempt"]: item for item in items}
        assert set(by_attempt) == {1, 2}
        assert (by_attempt[1]["requested_model"], by_attempt[1]["model"]) == ("routed", "t-a")
        assert by_attempt[1]["status"] == "upstream_error"
        assert (by_attempt[2]["requested_model"], by_attempt[2]["model"]) == ("routed", "t-b")
        assert by_attempt[2]["status"] == "completed"
        assert by_attempt[1]["request_id"] == by_attempt[2]["request_id"]

        rows = list(csv.DictReader(io.StringIO((await client.get("/api/admin/requests.csv")).text)))
        assert {row["attempt"]: row["model"] for row in rows} == {"1": "t-a", "2": "t-b"}
