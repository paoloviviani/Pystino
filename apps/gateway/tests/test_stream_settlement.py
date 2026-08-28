"""Settling a stream that the client abandons at the last moment.

Ground rule 3: accounting gets tests specifically, because a wrong answer here
is a wrong invoice. The failure these cover is a request the provider generated
and billed us for in full, recorded as `in_progress` with zero cost — money
spent that no report can see.

Two shapes, and the second is the one that was broken for as long as streaming
has existed:

* the client leaves **mid-stream**, which `spawn_finalisation` has always
  handled;
* the client leaves **while the settle is being written**, having read the
  terminal frame. That await lived inside the request task, so uvicorn's
  cancellation tore the database connection down mid-statement.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest
from fastapi import FastAPI
from gateway.routers.chat import settle_completed


class _App:
    def __init__(self) -> None:
        self.state = type("S", (), {"background_tasks": set()})()


class _Request:
    def __init__(self, app: Any) -> None:
        self.app = app


class SlowMetered:
    """A settle that takes long enough for a cancellation to land inside it.

    The real one is a database round trip. The sleep stands in for it, which is
    the honest way to reproduce a race whose whole nature is *when* the
    cancellation arrives relative to the write.
    """

    def __init__(self, delay: float = 0.05) -> None:
        self.delay = delay
        self.settled = False
        self.upstream_status: int | None = None

    async def completed(self, *, upstream_status: int | None = None) -> None:
        await asyncio.sleep(self.delay)
        self.settled = True
        self.upstream_status = upstream_status


class TestSettleCompleted:
    @pytest.mark.asyncio
    async def test_settles_normally_and_in_order(self) -> None:
        """The happy path must not become asynchronous.

        If this returned before the row was written, every test that asserts on
        the ledger immediately after a response would start racing — so the fix
        for the cancellation case must not be "detach and forget".
        """
        app, metered = _App(), SlowMetered()
        await settle_completed(_Request(app), metered=metered, upstream_status=200)
        assert metered.settled
        assert metered.upstream_status == 200

    @pytest.mark.asyncio
    async def test_the_write_survives_the_caller_being_cancelled(self) -> None:
        """The bug. Cancel the request task while the settle is in flight.

        Without the shield this leaves `settled` False: the await raises
        CancelledError part-way through the write, and the row stays
        `in_progress` at zero cost for a request that really ran.
        """
        app, metered = _App(), SlowMetered(delay=0.05)

        async def request_task() -> None:
            await settle_completed(_Request(app), metered=metered, upstream_status=200)

        task = asyncio.create_task(request_task())
        # Long enough to be inside settle_completed, short enough to be inside
        # the write rather than after it.
        await asyncio.sleep(0.01)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

        assert not metered.settled, "the fixture cancelled too late to prove anything"
        # The detached write is still running. It has to finish on its own.
        await asyncio.sleep(0.1)
        assert metered.settled, "the settle was lost when the client hung up"

    @pytest.mark.asyncio
    async def test_the_task_is_referenced_while_it_runs(self) -> None:
        """A task nothing refers to can be collected mid-flight.

        Which would lose exactly the write being protected — the same reason
        `spawn_finalisation` keeps a set on app.state.
        """
        app, metered = _App(), SlowMetered(delay=0.05)

        async def request_task() -> None:
            await settle_completed(_Request(app), metered=metered, upstream_status=200)

        task = asyncio.create_task(request_task())
        await asyncio.sleep(0.01)
        assert app.state.background_tasks, "the settle task is unreferenced while in flight"
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        await asyncio.sleep(0.1)
        assert not app.state.background_tasks, "the finished task was never discarded"


class TestAgainstTheRealApp:
    @pytest.mark.asyncio
    async def test_the_app_holds_a_background_task_set(self, app: FastAPI) -> None:
        """`settle_completed` reads it on every streamed request.

        Pinned because the attribute is created in one place and used in
        another, and an AttributeError here would surface as a failed settle
        rather than as a startup error.
        """
        assert isinstance(app.state.background_tasks, set)
