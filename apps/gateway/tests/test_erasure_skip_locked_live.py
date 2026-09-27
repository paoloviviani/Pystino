"""``FOR UPDATE SKIP LOCKED`` against a real PostgreSQL (ADR 0093 §9.3):
the property no unit test against SQLite can see, since SQLite has no row
locking at all and every `with_for_update` there is a silent no-op. Two
claimers here run concurrently on genuinely separate connections; the only
thing that can make every pending row land exactly once is the database
itself skipping a row the other side already holds.

Gated on ``TEST_POSTGRES_URL`` (``postgresql+asyncpg://...``), skipped
without it -- this suite otherwise runs on SQLite alone and no CI leg here
brings up a real Postgres by default. Against the box's own throwaway
container:

    docker run -d --rm --name auth-pg-c -e POSTGRES_PASSWORD=pw \\
      -e POSTGRES_USER=gw -e POSTGRES_DB=gw \\
      -p 127.0.0.1:55442:5432 pgvector/pgvector:pg18
    TEST_POSTGRES_URL=postgresql+asyncpg://gw:pw@127.0.0.1:55442/gw \\
      uv run pytest -q apps/gateway/tests/test_erasure_skip_locked_live.py
"""

from __future__ import annotations

import asyncio
import json
import os
import uuid

import httpx
import pytest
from gateway.config import ChatSettings, Settings
from gateway.db import create_engine, create_session_factory
from gateway.erasure import queue_erasure, run_pending_erasures
from gateway.models import Base, ChatErasure, ChatErasureStatus
from sqlalchemy import select

TEST_POSTGRES_URL = os.environ.get("TEST_POSTGRES_URL")

pytestmark = pytest.mark.skipif(
    not TEST_POSTGRES_URL,
    reason="set TEST_POSTGRES_URL (postgresql+asyncpg://...) to run this against a real Postgres",
)

#: Enough rows that a race would show up if the locking were wrong, few
#: enough that the pass stays fast.
ROW_COUNT = 20


async def test_two_concurrent_claimers_each_claim_every_row_exactly_once() -> None:
    settings = Settings(
        database_url=TEST_POSTGRES_URL,
        chat=ChatSettings(
            erasure_url="http://chat.internal/chat/internal/erasure",
            erasure_token="test-token",
        ),
    )
    engine = create_engine(settings)
    session_factory = create_session_factory(engine)

    async with engine.begin() as connection:
        # The full schema, not just `chat_erasures`: `attempt_erasure` also
        # writes `identity_events` (the audit trail), and neither carries a
        # foreign key to the other that would let one be created alone.
        await connection.run_sync(Base.metadata.create_all)

    try:
        row_ids: list[uuid.UUID] = []
        async with session_factory() as session:
            for _ in range(ROW_COUNT):
                row = await queue_erasure(session, gateway_user_id=uuid.uuid4(), identities=[])
                row_ids.append(row.id)
            await session.commit()

        seen: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            erasure_id = json.loads(request.read())["erasure_id"]
            seen.append(erasure_id)
            return httpx.Response(200, json={"counts": {}})

        http = httpx.AsyncClient(transport=httpx.MockTransport(handler))

        # Two independent calls, interleaved by the event loop across two
        # separate pooled connections -- each iteration of the while loop
        # inside `run_pending_erasures` opens (and commits, and closes) its
        # own session, so the two coroutines genuinely hold overlapping
        # transactions on the database, not just overlapping Python frames.
        attempted = await asyncio.gather(
            run_pending_erasures(session_factory, settings, http),
            run_pending_erasures(session_factory, settings, http),
        )

        assert sum(attempted) == ROW_COUNT
        assert len(seen) == ROW_COUNT
        assert len(set(seen)) == ROW_COUNT, (
            "the same erasure_id was sent to the chat more than once"
        )
        assert set(seen) == {str(row_id) for row_id in row_ids}

        async with session_factory() as session:
            rows = (
                (await session.execute(select(ChatErasure).where(ChatErasure.id.in_(row_ids))))
                .scalars()
                .all()
            )
            assert len(rows) == ROW_COUNT
            assert all(row.status is ChatErasureStatus.DONE for row in rows)
    finally:
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.drop_all)
        await engine.dispose()
