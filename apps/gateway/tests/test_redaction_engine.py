"""Choosing the redaction engine from the console (ADR 0033).

This is the one screen where a wrong answer means personal data left the
deployment, so what is pinned here is not "the endpoint works" but every way it
could go **quietly** wrong:

* an engine saved that cannot run, so the change looks applied and is not;
* an engine enabled whose service is dead, so every request fails or every
  prompt goes upstream unredacted depending on ``fail_open``;
* the layer switched off with no record of who did it or why;
* a worker still running the previous engine with nothing saying so;
* a database blip changing policy in either direction.

Ground rule 3's argument transfers: a wrong answer here is not a stack trace.
"""

from __future__ import annotations

import httpx
import pytest
from conftest import Seeded
from gateway.config import RedactionSettings
from gateway.models import RedactionConfig
from gateway.redaction.registry import EngineInfo
from gateway.redaction.resolver import RedactionResolver, current_engine
from gateway.routers.admin import _engine_options, _engine_redacts
from gateway.schemas import RedactionServiceHealth
from pydantic import SecretStr
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from test_admin import as_user, make_admin

ENGINE_URL = "/api/admin/redaction/engine"


def settings(**overrides: object) -> RedactionSettings:
    base: dict[str, object] = {
        "engine": "http",
        "endpoint": "http://redaction:8080",
        "placeholder_key": SecretStr("k"),
    }
    base.update(overrides)
    return RedactionSettings(**base)  # type: ignore[arg-type]


class TestWhatCanBeEnabled:
    """``blocked_reason`` is computed server-side, and this is why.

    The console must not offer a button the API would refuse. The rule that
    decides is the engine's own constructor plus the environment it would run in,
    so working it out in the browser would mean the browser knowing which
    settings each engine needs.
    """

    def test_both_builtins_are_offered_when_the_environment_is_complete(self) -> None:
        from gateway.config import Settings

        options = _engine_options(
            Settings(redaction=settings()), "http", RedactionServiceHealth(reachable=True)
        )
        by_name = {option.name: option for option in options}
        assert by_name["http"].blocked_reason is None
        assert by_name["noop"].blocked_reason is None
        assert by_name["http"].is_active is True

    def test_a_detection_engine_with_no_endpoint_is_blocked_with_the_variable_named(
        self,
    ) -> None:
        """Naming the variable, because "cannot be enabled" alone is a dead end."""
        from gateway.config import Settings

        options = _engine_options(
            Settings(redaction=RedactionSettings(engine="noop")), "noop", None
        )
        http = next(option for option in options if option.name == "http")
        assert http.blocked_reason is not None
        assert "GATEWAY_REDACTION__ENDPOINT" in http.blocked_reason

    def test_turning_it_off_is_never_blocked(self) -> None:
        """Whatever else is broken, the way out must stay available."""
        from gateway.config import Settings

        options = _engine_options(
            Settings(redaction=settings()),
            "http",
            RedactionServiceHealth(reachable=False, detail="connection refused"),
        )
        noop = next(option for option in options if option.name == "noop")
        assert noop.blocked_reason is None

    def test_the_engine_in_force_can_be_re_enabled_even_when_its_service_is_down(
        self,
    ) -> None:
        """Or an operator cannot switch away from a broken engine and back."""
        from gateway.config import Settings

        options = _engine_options(
            Settings(redaction=settings()),
            "http",
            RedactionServiceHealth(reachable=False, detail="connection refused"),
        )
        assert next(o for o in options if o.name == "http").blocked_reason is None

    def test_a_dead_service_blocks_an_engine_that_is_not_yet_running(self) -> None:
        from gateway.config import Settings

        options = _engine_options(
            Settings(redaction=settings(engine="noop")),
            "noop",
            RedactionServiceHealth(reachable=False, detail="connection refused"),
        )
        http = next(o for o in options if o.name == "http")
        assert http.blocked_reason is not None and "not answering" in http.blocked_reason


class TestEnabledIsAskedOfTheRegistry:
    """Not compared against the string "noop"."""

    def test_the_builtins_answer_correctly(self) -> None:
        assert _engine_redacts("http") is True
        assert _engine_redacts("noop") is False

    def test_an_unknown_engine_is_assumed_to_redact(self) -> None:
        """Assuming the opposite would show a working layer as switched off."""
        assert _engine_redacts("some-installed-plugin") is True

    def test_a_third_party_engine_that_redacts_nothing_reports_as_off(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The reason this is a registry question at all.

        An installable engine could redact nothing under any name. Comparing
        against "noop" would report it as enabled, and "enabled" is the single
        word this whole screen exists to get right.
        """
        from gateway.redaction import registry

        monkeypatch.setattr(
            registry,
            "describe",
            lambda: [EngineInfo(name="passthrough", label="p", description="", redacts=False)],
        )
        assert _engine_redacts("passthrough") is False


class TestTheRoute:
    async def test_an_engine_that_is_not_installed_is_refused_and_lists_what_is(
        self, app: object, client: httpx.AsyncClient, seeded: Seeded, session_factory: object
    ) -> None:
        as_user(app, await make_admin(session_factory, seeded))  # type: ignore[arg-type]
        response = await client.put(ENGINE_URL, json={"engine": "wishful", "reason": "x"})
        assert response.status_code == 400
        assert "noop" in response.text

    async def test_switching_off_without_a_reason_is_allowed(
        self, app: object, client: httpx.AsyncClient, seeded: Seeded, session_factory: object
    ) -> None:
        """Refused until this was asked to be dropped, and the drop is the point.

        Turning redaction off used to require a written reason on the grounds
        that a later review would read it. A sentence typed to get past a
        dialog is not an audit trail, and what a review can actually rely on —
        which engine, who chose it, when — is recorded either way. The
        confirmation stays; the form does not.
        """
        as_user(app, await make_admin(session_factory, seeded))  # type: ignore[arg-type]
        response = await client.put(ENGINE_URL, json={"engine": "noop", "reason": "   "})
        assert response.status_code == 200
        assert response.json()["engine"] == "noop"
        assert response.json()["configured"]["reason"] == ""

    async def test_the_reason_may_be_omitted_altogether(
        self,
        app: object,
        client: httpx.AsyncClient,
        seeded: Seeded,
        session: AsyncSession,
        session_factory: object,
    ) -> None:
        as_user(app, await make_admin(session_factory, seeded))  # type: ignore[arg-type]
        response = await client.put(ENGINE_URL, json={"engine": "noop"})
        assert response.status_code == 200, response.text
        row = (await session.execute(select(RedactionConfig))).scalar_one()
        assert row.reason == ""

    async def test_a_reason_is_still_kept_when_one_is_given(
        self, app: object, client: httpx.AsyncClient, seeded: Seeded, session_factory: object
    ) -> None:
        """Optional, not removed: an operator who explains is still recorded."""
        as_user(app, await make_admin(session_factory, seeded))  # type: ignore[arg-type]
        response = await client.put(
            ENGINE_URL, json={"engine": "noop", "reason": "detector down for maintenance"}
        )
        assert response.status_code == 200
        assert response.json()["configured"]["reason"] == "detector down for maintenance"

    async def test_an_engine_the_environment_cannot_satisfy_is_refused_before_saving(
        self,
        app: object,
        client: httpx.AsyncClient,
        seeded: Seeded,
        session: AsyncSession,
        session_factory: object,
    ) -> None:
        """The test suite runs with no detection endpoint, so `http` cannot run.

        Refused rather than written: a stored configuration that cannot be built
        means every worker logging a construction failure on its next poll and
        continuing with the old engine, which looks like the change not working
        and reads like a bug.
        """
        as_user(app, await make_admin(session_factory, seeded))  # type: ignore[arg-type]
        response = await client.put(ENGINE_URL, json={"engine": "http", "reason": ""})
        assert response.status_code == 400
        assert (await session.execute(select(RedactionConfig))).first() is None

    async def test_a_change_is_recorded_with_who_and_why(
        self,
        app: object,
        client: httpx.AsyncClient,
        seeded: Seeded,
        session: AsyncSession,
        session_factory: object,
    ) -> None:
        admin = await make_admin(session_factory, seeded)  # type: ignore[arg-type]
        as_user(app, admin)
        response = await client.put(
            ENGINE_URL, json={"engine": "noop", "reason": "detection service migration"}
        )
        assert response.status_code == 200

        row = (await session.execute(select(RedactionConfig))).scalar_one()
        assert row.engine == "noop"
        assert row.reason == "detection service migration"
        assert row.created_by == admin.id

        body = response.json()
        assert body["source"] == "console"
        assert body["configured"]["reason"] == "detection service migration"
        assert body["engine"] == "noop"
        assert body["enabled"] is False

    async def test_presidio_families_are_independently_recorded_and_reported(
        self,
        app: object,
        client: httpx.AsyncClient,
        seeded: Seeded,
        session: AsyncSession,
        session_factory: object,
    ) -> None:
        as_user(app, await make_admin(session_factory, seeded))  # type: ignore[arg-type]
        response = await client.put(
            ENGINE_URL,
            json={"engine": "noop", "presidio_pattern_matching": False, "presidio_ner": True},
        )
        assert response.status_code == 200
        row = (await session.execute(select(RedactionConfig))).scalar_one()
        assert row.presidio_pattern_matching is False
        assert row.presidio_ner is True
        body = response.json()
        assert body["presidio_pattern_matching"] is False
        assert body["presidio_ner"] is True
        assert body["configured"]["presidio_pattern_matching"] is False
        assert body["configured"]["presidio_ner"] is True

    async def test_the_response_says_how_long_other_workers_may_lag(
        self, app: object, client: httpx.AsyncClient, seeded: Seeded, session_factory: object
    ) -> None:
        """A change that looks instant and is not is worse than one that says so."""
        as_user(app, await make_admin(session_factory, seeded))  # type: ignore[arg-type]
        body = (await client.put(ENGINE_URL, json={"engine": "noop", "reason": "why"})).json()
        assert body["propagation_seconds"] > 0

    async def test_this_worker_switches_immediately(
        self, app: object, client: httpx.AsyncClient, seeded: Seeded, session_factory: object
    ) -> None:
        """The operator's own next request must not show the old engine."""
        as_user(app, await make_admin(session_factory, seeded))  # type: ignore[arg-type]
        await client.put(ENGINE_URL, json={"engine": "noop", "reason": "why"})
        assert app.state.redactor.name == "noop"  # type: ignore[attr-defined]
        assert (await client.get("/api/admin/redaction")).json()["engine"] == "noop"

    async def test_rows_are_appended_rather_than_updated(
        self,
        app: object,
        client: httpx.AsyncClient,
        seeded: Seeded,
        session: AsyncSession,
        session_factory: object,
    ) -> None:
        """The history is the feature.

        "Who turned it off, when, and why" is asked about a window that has
        already closed, and a mutable row answers it only for the most recent
        change — the one nobody needs to ask about.
        """
        as_user(app, await make_admin(session_factory, seeded))  # type: ignore[arg-type]
        await client.put(ENGINE_URL, json={"engine": "noop", "reason": "first"})
        await client.put(ENGINE_URL, json={"engine": "noop", "reason": "second"})

        rows = (await session.execute(select(RedactionConfig))).scalars().all()
        assert [row.reason for row in rows] == ["first", "second"]
        # And the newest wins.
        newest = await current_engine(session)
        assert newest is not None and newest.reason == "second"

    async def test_a_non_admin_cannot_change_it(
        self, client: httpx.AsyncClient, seeded: Seeded
    ) -> None:
        assert (
            await client.put(ENGINE_URL, json={"engine": "noop", "reason": "x"})
        ).status_code in (401, 403)


# The policy route is gone. Setting what a deployment redacts is now writing a
# rule scoped to `all`, like every other scope (ADR 0039), and
# test_redaction_rules.py covers it there — including that it is the base of the
# fold. Deleted rather than left asserting a 404: a test whose subject no longer
# exists is a test nobody can read.


class TestTheResolver:
    """How a worker that did not handle the request finds out."""

    async def test_no_row_means_the_environment_decides(
        self, session_factory: async_sessionmaker[AsyncSession]
    ) -> None:
        """A deployment that never opens the console behaves as it always did."""
        resolver = RedactionResolver(RedactionSettings(engine="noop"), session_factory)
        assert await resolver.refresh() is False
        assert resolver.source == "environment"
        assert resolver.redactor.name == "noop"

    async def test_a_row_overrides_the_environment_and_says_so(
        self,
        session: AsyncSession,
        session_factory: async_sessionmaker[AsyncSession],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from gateway.redaction import registry

        # A stand-in for an installed engine, so this tests the override rather
        # than the HTTP engine's own prerequisites.
        registered = registry.describe()
        monkeypatch.setitem(registry._BUILTIN, "second", registry._BUILTIN["noop"])
        monkeypatch.setitem(
            registry._INFO,
            "second",
            EngineInfo(name="second", label="Second", description=""),
        )
        assert len(registry.describe()) == len(registered) + 1

        resolver = RedactionResolver(RedactionSettings(engine="noop"), session_factory)
        session.add(RedactionConfig(engine="second", reason="because"))
        await session.commit()

        assert await resolver.refresh() is True
        assert resolver.source == "console"

    async def test_the_same_row_twice_does_not_rebuild(
        self, session: AsyncSession, session_factory: async_sessionmaker[AsyncSession]
    ) -> None:
        """Rebuilding would throw away the per-process detection cache.

        That cache is most of the value of the HTTP engine on a long
        conversation (docs/performance.md), and a poll every ten seconds that
        rebuilt would quietly destroy the hit rate.
        """
        resolver = RedactionResolver(RedactionSettings(engine="noop"), session_factory)
        session.add(RedactionConfig(engine="noop", reason="x"))
        await session.commit()

        assert await resolver.refresh() is True
        first = resolver.redactor
        assert await resolver.refresh() is False
        assert resolver.redactor is first

    async def test_an_engine_that_cannot_be_built_leaves_the_current_one_running(
        self, session: AsyncSession, session_factory: async_sessionmaker[AsyncSession]
    ) -> None:
        """Falling back to noop would switch the layer off as a side effect.

        The row was validated when it was saved, so reaching here means the
        deployment changed underneath it — a plugin uninstalled, an endpoint
        removed. Keep serving and log; do not silently stop protecting anything.
        """
        resolver = RedactionResolver(RedactionSettings(engine="noop"), session_factory)
        # `http` with no endpoint cannot be constructed.
        session.add(RedactionConfig(engine="http", reason="x"))
        await session.commit()

        assert await resolver.refresh() is False
        assert resolver.redactor.name == "noop"
        assert resolver.source == "environment"

    async def test_a_database_failure_changes_nothing(
        self, session_factory: async_sessionmaker[AsyncSession]
    ) -> None:
        """In either direction. A blip must not be a policy change."""

        def broken() -> object:
            raise RuntimeError("no database")

        resolver = RedactionResolver(RedactionSettings(engine="noop"), session_factory)
        resolver._session_factory = broken  # type: ignore[assignment]
        assert await resolver.refresh() is False
        assert resolver.redactor.name == "noop"
