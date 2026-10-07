"""ADR 0060: the final ledger write happens after the answer is sent.

The row is inserted before the upstream call and finalised after it. Finalising
used to be awaited *inside* the request, so a caller waited for an
``UPDATE usage_records`` and a counter settle it has no use for. It now rides on
the response as a Starlette background task, which runs once the last byte is
out.

Three properties are worth pinning, and the second is why this is safe rather
than merely faster:

* the row still ends up **complete and correct** — deferred is not dropped;
* a caller sees no difference, because the transport awaits the background task
  before the request returns, which is also why the suite stayed deterministic
  when this changed;
* a failure after the response is **logged, not raised** — by then the caller
  has a 200 and the upstream has served them, so raising would report a failure
  for a request that succeeded.
"""

from __future__ import annotations

import logging
from decimal import Decimal
from typing import Any

import httpx
import orjson
import pytest
from conftest import FakeUpstream, Seeded
from gateway.models import UsageRecord, UsageStatus
from gateway.quota import QuotaAmounts
from helpers import completion_body
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.background import BackgroundTask


def basic(seeded: Seeded) -> dict[str, Any]:
    return {
        "model": seeded.model.name,
        "messages": [{"role": "user", "content": "hello"}],
    }


async def latest(session: AsyncSession) -> UsageRecord:
    return (
        (
            await session.execute(
                select(UsageRecord).order_by(UsageRecord.created_at.desc()).limit(1)
            )
        )
        .scalars()
        .one()
    )


class TestTheRowStillLands:
    async def test_a_completion_is_finalised_with_its_real_figures(
        self,
        client: httpx.AsyncClient,
        seeded: Seeded,
        fake_upstream: FakeUpstream,
        session: AsyncSession,
    ) -> None:
        """Deferred, not dropped: the same row the awaited version wrote."""
        fake_upstream.set_json(completion_body(prompt=1000, completion=500))

        response = await client.post(
            "/v1/chat/completions", json=basic(seeded), headers=seeded.auth
        )
        assert response.status_code == 200

        record = await latest(session)
        assert record.status is UsageStatus.COMPLETED
        assert record.prompt_tokens == 1000
        assert record.completion_tokens == 500
        # 1000 in at 1/Mtok and 500 out at 2/Mtok, from the seeded price.
        assert record.cost == Decimal("0.002000000000")

    async def test_a_second_surface_settles_too(
        self,
        client: httpx.AsyncClient,
        seeded: Seeded,
        fake_upstream: FakeUpstream,
        session: AsyncSession,
    ) -> None:
        """Each route attaches its own task, so more than one is worth checking.

        Not all six: a surface that failed to attach one would leave its row
        `in_progress`, and every existing per-surface test asserts on a
        finalised row. That is the coverage; this is the reminder.
        """
        fake_upstream.set_json(
            {
                "id": "resp-1",
                "object": "response",
                "output": [],
                "output_text": "hi",
                "usage": {"input_tokens": 7, "output_tokens": 3},
            }
        )

        response = await client.post(
            "/v1/responses",
            json={"model": seeded.model.name, "input": "hello"},
            headers=seeded.auth,
        )
        assert response.status_code == 200

        record = await latest(session)
        assert record.status is UsageStatus.COMPLETED
        assert record.prompt_tokens == 7


class TestTheMechanism:
    """White-box, because the point is *where* the work runs, not what it does."""

    async def test_the_body_reaches_the_client_before_the_write(
        self,
        app: Any,
        seeded: Seeded,
        fake_upstream: FakeUpstream,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """The whole point of the change, asserted as an ordering.

        Driven as raw ASGI rather than through the client, because the client
        hides exactly what is being tested: it awaits the background task
        before returning, so from its side deferred and awaited look identical.
        Here the ASGI ``send`` callable records when the body goes out, and a
        patched ``finalise`` records when the row is written. Without this test
        a refactor could put the write back on the critical path with every
        other test still green.
        """
        from gateway.accounting.recorder import RequestAccounting

        order: list[str] = []
        real_finalise = RequestAccounting.finalise

        async def recording_finalise(self: Any, **kwargs: Any) -> Any:
            order.append("finalise")
            return await real_finalise(self, **kwargs)

        monkeypatch.setattr(RequestAccounting, "finalise", recording_finalise)
        fake_upstream.set_json(completion_body())

        payload = orjson.dumps(basic(seeded))
        scope = {
            "type": "http",
            "asgi": {"version": "3.0", "spec_version": "2.3"},
            "http_version": "1.1",
            "method": "POST",
            "scheme": "http",
            "path": "/v1/chat/completions",
            "raw_path": b"/v1/chat/completions",
            "query_string": b"",
            "root_path": "",
            "headers": [
                (b"host", b"gateway"),
                (b"content-type", b"application/json"),
                (b"content-length", str(len(payload)).encode()),
                *[(key.lower().encode(), value.encode()) for key, value in seeded.auth.items()],
            ],
            "client": ("127.0.0.1", 1234),
            "server": ("gateway", 80),
        }

        async def receive() -> dict[str, Any]:
            return {"type": "http.request", "body": payload, "more_body": False}

        async def send(message: dict[str, Any]) -> None:
            if message["type"] == "http.response.body" and message.get("body"):
                order.append("body")

        await app(scope, receive, send)

        assert order == ["body", "finalise"], (
            f"expected the answer to go out before the ledger write, got {order}"
        )

    async def test_the_task_finalises_and_settles(self) -> None:
        """The task does both halves, in that order."""
        from gateway.routers._metered import Metered

        done: list[str] = []

        class FakeAccounting:
            request_id = "req-1"

            async def finalise(self, **_: Any) -> QuotaAmounts:
                done.append("finalise")
                return QuotaAmounts()

        class FakeQuota:
            async def settle(self, *_: Any) -> None:
                done.append("settle")

        metered = Metered(
            accounting=FakeAccounting(),  # type: ignore[arg-type]
            reservation=object(),  # type: ignore[arg-type]
            quota=FakeQuota(),  # type: ignore[arg-type]
        )
        task = metered.completed_after_response(upstream_status=200)
        assert isinstance(task, BackgroundTask)
        await task()
        assert done == ["finalise", "settle"]

    async def test_a_failure_after_the_response_is_logged_not_raised(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """The caller already has a 200 and the upstream already served them.

        Raising here would report a failure for a request that succeeded — and
        they would be billed for it anyway, because the row exists.
        """
        from gateway.routers._metered import Metered

        class Exploding:
            request_id = "req-2"

            async def finalise(self, **_: Any) -> QuotaAmounts:
                raise RuntimeError("postgres went away")

        class FakeQuota:
            async def settle(self, *_: Any) -> None:  # pragma: no cover - never reached
                raise AssertionError("settle should not run after finalise failed")

        metered = Metered(
            accounting=Exploding(),  # type: ignore[arg-type]
            reservation=object(),  # type: ignore[arg-type]
            quota=FakeQuota(),  # type: ignore[arg-type]
        )
        with caplog.at_level(logging.ERROR):
            await metered.completed_after_response(upstream_status=200)()

        assert "req-2" in caplog.text
        assert "in_progress" in caplog.text
