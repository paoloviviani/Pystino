"""Chat erasure (ADR 0093 §9.3): the retry loop's own mechanics, isolated
from the delete endpoint that queues the first row. A fake chat is an
``httpx.MockTransport``, not a real server — sufficient for a call whose
whole contract is "one POST, one JSON body back".
"""

from __future__ import annotations

import uuid
from datetime import timedelta

import httpx
from gateway.config import ChatSettings, Settings
from gateway.erasure import (
    attempt_erasure,
    backoff_seconds,
    queue_erasure,
    run_pending_erasures,
)
from gateway.identity_events import DETAIL_ALLOWLIST
from gateway.models import ChatErasure, ChatErasureStatus, IdentityEvent, IdentityEventAction
from gateway.types import utcnow
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

ERASURE_URL = "http://chat.internal/chat/internal/erasure"


def _settings(*, token: str = "test-token") -> Settings:  # noqa: S107 -- a test double, not a secret
    return Settings(chat=ChatSettings(erasure_url=ERASURE_URL, erasure_token=token))


class TestBackoff:
    def test_the_schedule_floors_at_thirty_and_doubles(self) -> None:
        assert backoff_seconds(1) == 30
        assert backoff_seconds(2) == 60
        assert backoff_seconds(3) == 120
        assert backoff_seconds(4) == 240

    def test_the_schedule_caps_at_one_hour(self) -> None:
        assert backoff_seconds(20) == 3600


class TestQueueErasure:
    async def test_inserts_a_pending_row(self, session: AsyncSession) -> None:
        gateway_user_id = uuid.uuid4()
        identities = [{"issuer": "https://idp.test", "subject": "s"}]
        row = await queue_erasure(session, gateway_user_id=gateway_user_id, identities=identities)
        await session.commit()

        fetched = await session.get(ChatErasure, row.id)
        assert fetched is not None
        assert fetched.status is ChatErasureStatus.PENDING
        assert fetched.gateway_user_id == gateway_user_id
        assert fetched.identities == identities
        assert fetched.attempts == 0


class TestAttemptErasure:
    async def test_success_marks_done_clears_identities_and_audits(
        self, session: AsyncSession
    ) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            assert request.headers["authorization"] == "Bearer test-token"
            body = request.read()
            assert b"erasure_id" in body
            return httpx.Response(200, json={"counts": {"conversations": 3}})

        http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        row = await queue_erasure(
            session,
            gateway_user_id=uuid.uuid4(),
            identities=[{"issuer": "https://idp.test", "subject": "s"}],
        )
        await session.commit()

        done = await attempt_erasure(session, _settings(), http, row)
        assert done is True

        await session.refresh(row)
        assert row.status is ChatErasureStatus.DONE
        assert row.done_at is not None
        assert row.chat_counts == {"conversations": 3}
        assert row.identities == []

        event = (
            await session.execute(
                select(IdentityEvent).where(
                    IdentityEvent.action == IdentityEventAction.CHAT_ERASURE_DONE
                )
            )
        ).scalar_one()
        assert event.detail == {"counts": {"conversations": 3}}

    async def test_failure_backs_off_and_audits_the_first_attempt(
        self, session: AsyncSession
    ) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(503, json={"error": "down"})

        http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        row = await queue_erasure(session, gateway_user_id=uuid.uuid4(), identities=[])
        await session.commit()

        done = await attempt_erasure(session, _settings(), http, row)
        assert done is False

        await session.refresh(row)
        assert row.status is ChatErasureStatus.PENDING
        assert row.attempts == 1
        assert row.last_error
        assert row.next_attempt_at is not None
        assert row.next_attempt_at <= utcnow() + timedelta(seconds=31)

        event = (
            await session.execute(
                select(IdentityEvent).where(
                    IdentityEvent.action == IdentityEventAction.CHAT_ERASURE_RETRYING
                )
            )
        ).scalar_one()
        assert event.detail == {"attempts": 1}

    async def test_only_the_first_and_every_tenth_failure_is_audited(
        self, session: AsyncSession
    ) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(503)

        http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        row = await queue_erasure(session, gateway_user_id=uuid.uuid4(), identities=[])
        await session.commit()

        for _ in range(11):
            row.next_attempt_at = utcnow()  # force it due for this test
            await attempt_erasure(session, _settings(), http, row)

        events = (
            (
                await session.execute(
                    select(IdentityEvent).where(
                        IdentityEvent.action == IdentityEventAction.CHAT_ERASURE_RETRYING
                    )
                )
            )
            .scalars()
            .all()
        )
        assert [e.detail["attempts"] for e in events] == [1, 10]

    async def test_a_repeat_after_done_is_idempotent(self, session: AsyncSession) -> None:
        calls = 0

        def handler(request: httpx.Request) -> httpx.Response:
            nonlocal calls
            calls += 1
            return httpx.Response(200, json={"counts": {"conversations": 1}})

        http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        row = await queue_erasure(session, gateway_user_id=uuid.uuid4(), identities=[])
        await session.commit()

        assert await attempt_erasure(session, _settings(), http, row) is True
        assert await attempt_erasure(session, _settings(), http, row) is True
        assert calls == 1, "already done: no second call to the chat at all"

    async def test_no_erasure_url_backs_off_without_calling_anything(
        self, session: AsyncSession
    ) -> None:
        called = False

        def handler(request: httpx.Request) -> httpx.Response:
            nonlocal called
            called = True
            return httpx.Response(200, json={})

        http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        row = await queue_erasure(session, gateway_user_id=uuid.uuid4(), identities=[])
        await session.commit()

        done = await attempt_erasure(session, Settings(), http, row)
        assert done is False
        assert not called
        await session.refresh(row)
        assert row.attempts == 1


class TestRunPendingErasures:
    async def test_processes_every_due_row(
        self, session_factory: async_sessionmaker[AsyncSession]
    ) -> None:
        http = httpx.AsyncClient(
            transport=httpx.MockTransport(lambda r: httpx.Response(200, json={"counts": {}}))
        )
        async with session_factory() as session:
            first = await queue_erasure(session, gateway_user_id=uuid.uuid4(), identities=[])
            await session.commit()
            second = await queue_erasure(session, gateway_user_id=uuid.uuid4(), identities=[])
            await session.commit()

        attempted = await run_pending_erasures(session_factory, _settings(), http)
        assert attempted == 2

        async with session_factory() as session:
            assert (await session.get(ChatErasure, first.id)).status is ChatErasureStatus.DONE
            assert (await session.get(ChatErasure, second.id)).status is ChatErasureStatus.DONE

    async def test_a_row_not_yet_due_is_left_alone(
        self, session_factory: async_sessionmaker[AsyncSession]
    ) -> None:
        http = httpx.AsyncClient(
            transport=httpx.MockTransport(lambda r: httpx.Response(200, json={"counts": {}}))
        )
        async with session_factory() as session:
            row = await queue_erasure(session, gateway_user_id=uuid.uuid4(), identities=[])
            row.next_attempt_at = utcnow() + timedelta(hours=1)
            await session.commit()
            row_id = row.id

        attempted = await run_pending_erasures(session_factory, _settings(), http)
        assert attempted == 0

        async with session_factory() as session:
            assert (await session.get(ChatErasure, row_id)).status is ChatErasureStatus.PENDING


def test_the_detail_allowlist_covers_both_new_actions() -> None:
    assert DETAIL_ALLOWLIST[IdentityEventAction.CHAT_ERASURE_DONE] == frozenset({"counts"})
    assert DETAIL_ALLOWLIST[IdentityEventAction.CHAT_ERASURE_RETRYING] == frozenset({"attempts"})
