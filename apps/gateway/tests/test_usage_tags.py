"""``x-pystino-tag`` and ``GET /v1/pystino/usage/tags``: a caller's usage by its own label.

The properties that matter: the header lands on the usage row (and an invalid
one never fails the request), the totals are the caller's own and nobody
else's, and what a quota would not count (BYOK, in flight) is not summed.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

import pytest
from conftest import Seeded
from gateway.models import UsageRecord, UsageStatus, User
from gateway.routers._metered import clean_tag
from helpers import completion_body
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from test_reporting import record

TAGS = "/v1/pystino/usage/tags"


async def tagged(
    session: AsyncSession, user_id: uuid.UUID, tag: str | None, **kwargs: Any
) -> UsageRecord:
    row = await record(session, user_id=user_id, **kwargs)
    row.tag = tag
    await session.commit()
    return row


class TestCleanTag:
    def test_plain_labels_pass_trimmed(self) -> None:
        assert clean_tag("  ses_abc123 ") == "ses_abc123"
        assert clean_tag("code:dev:s1") == "code:dev:s1"

    @pytest.mark.parametrize("bad", [None, "", "   ", "x" * 129, "café", "a\nb", "a\x00b"])
    def test_anything_else_is_dropped(self, bad: str | None) -> None:
        assert clean_tag(bad) is None

    def test_the_longest_label_is_kept(self) -> None:
        assert clean_tag("x" * 128) == "x" * 128


class TestHeaderIsRecorded:
    @pytest.mark.asyncio
    async def test_a_chat_request_stores_its_tag(
        self,
        client: Any,
        seeded: Seeded,
        fake_upstream: Any,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        fake_upstream.set_json(completion_body())
        response = await client.post(
            "/v1/chat/completions",
            json={"model": "test-model", "messages": [{"role": "user", "content": "hi"}]},
            headers={**seeded.auth, "x-pystino-tag": "ses_one"},
        )
        assert response.status_code == 200, response.text
        async with session_factory() as db:
            row = (await db.execute(select(UsageRecord))).scalars().one()
        assert row.tag == "ses_one"

    @pytest.mark.asyncio
    async def test_a_bad_tag_is_dropped_and_the_request_still_succeeds(
        self,
        client: Any,
        seeded: Seeded,
        fake_upstream: Any,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        fake_upstream.set_json(completion_body())
        response = await client.post(
            "/v1/chat/completions",
            json={"model": "test-model", "messages": [{"role": "user", "content": "hi"}]},
            headers={**seeded.auth, "x-pystino-tag": "x" * 500},
        )
        assert response.status_code == 200, response.text
        async with session_factory() as db:
            row = (await db.execute(select(UsageRecord))).scalars().one()
        assert row.tag is None


class TestTotals:
    async def test_it_needs_a_credential(self, client: Any) -> None:
        assert (await client.get(TAGS, params={"tag": "a"})).status_code == 401

    async def test_it_sums_the_callers_tagged_requests(
        self, client: Any, seeded: Seeded, session: AsyncSession
    ) -> None:
        uid = seeded.user.id
        await tagged(session, uid, "s1", cost="2", prompt=100, completion=10)
        await tagged(session, uid, "s1", cost="3", prompt=50, completion=5)
        await tagged(session, uid, "s2", cost="7")
        await tagged(session, uid, None, cost="9")

        response = await client.get(
            TAGS, params=[("tag", "s1"), ("tag", "nope")], headers=seeded.auth
        )
        assert response.status_code == 200, response.text
        rows = response.json()["tags"]
        assert [r["tag"] for r in rows] == ["s1"]
        assert rows[0]["requests"] == 2
        assert rows[0]["prompt_tokens"] == 150
        assert rows[0]["completion_tokens"] == 15
        assert Decimal(rows[0]["cost"]) == Decimal(5)

    async def test_another_users_tag_is_invisible(
        self,
        client: Any,
        seeded: Seeded,
        session: AsyncSession,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        async with session_factory() as db:
            other = User(issuer="https://idp.test", subject="subject-2", email="o@example.test")
            db.add(other)
            await db.commit()
            other_id = other.id
        await tagged(session, other_id, "shared-name", cost="40")
        await tagged(session, seeded.user.id, "shared-name", cost="1")

        rows = (await client.get(TAGS, params={"tag": "shared-name"}, headers=seeded.auth)).json()[
            "tags"
        ]
        assert len(rows) == 1 and Decimal(rows[0]["cost"]) == Decimal(1)

    async def test_byok_and_in_flight_rows_are_left_out(
        self, client: Any, seeded: Seeded, session: AsyncSession
    ) -> None:
        uid = seeded.user.id
        await tagged(session, uid, "s", cost="1")
        byok = await tagged(session, uid, "s", cost="50")
        byok.byok = True
        await tagged(session, uid, "s", cost="60", status=UsageStatus.IN_PROGRESS)
        await session.commit()

        rows = (await client.get(TAGS, params={"tag": "s"}, headers=seeded.auth)).json()["tags"]
        assert rows[0]["requests"] == 1
        assert Decimal(rows[0]["cost"]) == Decimal(1)

    async def test_since_and_until_bound_the_window(
        self, client: Any, seeded: Seeded, session: AsyncSession
    ) -> None:
        now = datetime.now(UTC)
        uid = seeded.user.id
        await tagged(session, uid, "s", cost="1", at=now - timedelta(hours=3))
        await tagged(session, uid, "s", cost="2", at=now - timedelta(hours=1))
        response = await client.get(
            TAGS,
            params={"tag": "s", "since": (now - timedelta(hours=2)).isoformat()},
            headers=seeded.auth,
        )
        assert Decimal(response.json()["tags"][0]["cost"]) == Decimal(2)

    @pytest.mark.parametrize(
        "params", [[], [("tag", "")], [("tag", "x" * 200)], [("tag", f"t{i}") for i in range(101)]]
    )
    async def test_invalid_tag_lists_are_refused(
        self, client: Any, seeded: Seeded, params: list[tuple[str, str]]
    ) -> None:
        response = await client.get(TAGS, params=params, headers=seeded.auth)
        assert response.status_code in (400, 422)
