"""The request log: metadata per request, never content, scoped by who asks."""

from __future__ import annotations

import csv
import io
import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import httpx
import pytest
from conftest import Seeded
from gateway.models import ApiKey, Group, UsageRecord, UsageStatus, User
from gateway.periods import Period
from gateway.request_log import RequestLogFilter, _listing_query
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from test_admin import as_user, make_admin
from test_reporting import inside_this_month, record


@pytest.fixture
async def admin_client(
    app: object, client: httpx.AsyncClient, seeded: Seeded, session_factory: object
) -> httpx.AsyncClient:
    as_user(app, await make_admin(session_factory, seeded))  # type: ignore[arg-type]
    return client


SECRET = "the assistant said something private"
ERROR = "provider echoed the prompt"


async def _row_with_content(session: AsyncSession, **kw: object) -> UsageRecord:
    row = await record(session, **kw)  # type: ignore[arg-type]
    row.assistant_text = SECRET
    row.error_message = ERROR
    await session.commit()
    return row


async def _other(session: AsyncSession) -> User:
    other = User(issuer="https://idp.test", subject="other", email="other@example.org")
    session.add(other)
    await session.flush()
    return other


class TestAdminListing:
    async def test_lists_newest_first_in_the_pagination_envelope(
        self, admin_client: httpx.AsyncClient, session: AsyncSession, seeded: Seeded
    ) -> None:
        old = await record(session, at=inside_this_month(3), cost="1")
        new = await record(session, at=inside_this_month(1), cost="2.5")
        body = (await admin_client.get("/api/admin/requests")).json()
        assert body["total"] == 2
        assert (body["limit"], body["offset"]) == (50, 0)
        assert [r["request_id"] for r in body["items"]] == [new.request_id, old.request_id]
        assert body["items"][0]["cost"] == "2.500000000000"

    async def test_paginates(
        self, admin_client: httpx.AsyncClient, session: AsyncSession, seeded: Seeded
    ) -> None:
        for i in range(3):
            await record(session, at=inside_this_month(i + 1))
        body = (await admin_client.get("/api/admin/requests?limit=2&offset=2")).json()
        assert body["total"] == 3
        assert len(body["items"]) == 1

    async def test_never_returns_content(
        self, admin_client: httpx.AsyncClient, session: AsyncSession, seeded: Seeded
    ) -> None:
        await _row_with_content(session, at=inside_this_month())
        for path in ("/api/admin/requests", "/api/admin/requests.csv"):
            text_ = (await admin_client.get(path)).text
            assert SECRET not in text_
            assert ERROR not in text_
        item = (await admin_client.get("/api/admin/requests")).json()["items"][0]
        assert "assistant_text" not in item
        assert "error_message" not in item

    async def test_shape_of_a_line(
        self, admin_client: httpx.AsyncClient, session: AsyncSession, seeded: Seeded
    ) -> None:
        key = ApiKey(user_id=seeded.user.id, name="k", prefix="pk_abc", key_hash="h" * 64)
        session.add(key)
        await session.flush()
        row = await record(
            session,
            user_id=seeded.user.id,
            group_id=seeded.group.id,
            api_key_id=key.id,
            at=inside_this_month(),
        )
        row.requested_model = "fast"
        row.latency_ms = 123
        row.cached_prompt_tokens = 7
        await session.commit()
        item = (await admin_client.get("/api/admin/requests")).json()["items"][0]
        assert item["requested_model"] == "fast"
        assert item["model"] == "test-model"
        assert item["source"] == "api_key"
        assert item["latency_ms"] == 123
        assert item["cached_prompt_tokens"] == 7
        assert item["status"] == "completed"
        assert item["group_name"] == seeded.group.name
        assert item["user_label"]

    async def test_a_token_request_has_source_token(
        self, admin_client: httpx.AsyncClient, session: AsyncSession, seeded: Seeded
    ) -> None:
        await record(session, user_id=seeded.user.id, at=inside_this_month())
        item = (await admin_client.get("/api/admin/requests")).json()["items"][0]
        assert item["source"] == "token"

    async def test_filters(
        self, admin_client: httpx.AsyncClient, session: AsyncSession, seeded: Seeded
    ) -> None:
        other = await _other(session)
        group2 = Group(name="g2")
        session.add(group2)
        await session.flush()
        a = await record(session, user_id=seeded.user.id, group_id=seeded.group.id, model="m1")
        b = await record(session, user_id=other.id, group_id=group2.id, model="m2")
        c = await record(session, user_id=other.id, status=UsageStatus.UPSTREAM_ERROR, model="m1")
        c.requested_model = "fast"
        await session.commit()

        async def ids(query: str) -> set[str]:
            body = (await admin_client.get(f"/api/admin/requests?{query}")).json()
            return {r["request_id"] for r in body["items"]}

        assert await ids(f"user_id={other.id}") == {b.request_id, c.request_id}
        assert await ids(f"group_id={group2.id}") == {b.request_id}
        assert await ids("model=m1") == {a.request_id, c.request_id}
        assert await ids("model=fast") == {c.request_id}  # by the alias asked for
        assert await ids("status=upstream_error") == {c.request_id}
        assert (await admin_client.get("/api/admin/requests?status=bogus")).status_code == 400

    async def test_period_filter_is_half_open(
        self, admin_client: httpx.AsyncClient, session: AsyncSession, seeded: Seeded
    ) -> None:
        await record(session, at=datetime(2026, 8, 31, 21, 0, tzinfo=UTC))
        inside = await record(session, at=datetime(2026, 8, 15, 12, tzinfo=UTC))
        await record(session, at=datetime(2026, 9, 15, 12, tzinfo=UTC))
        body = (await admin_client.get("/api/admin/requests?period=2026-08")).json()
        assert inside.request_id in {r["request_id"] for r in body["items"]}
        assert body["total"] == 2
        r = await admin_client.get(
            "/api/admin/requests?start=2026-08-15T00:00:00Z&end=2026-08-16T00:00:00Z"
        )
        assert r.json()["total"] == 1
        both = await admin_client.get(
            "/api/admin/requests?period=2026-08&start=2026-08-01T00:00:00Z"
        )
        assert both.status_code == 400


class TestCsv:
    async def test_same_filtered_set_money_as_strings(
        self, admin_client: httpx.AsyncClient, session: AsyncSession, seeded: Seeded
    ) -> None:
        other = await _other(session)
        await record(session, user_id=seeded.user.id, cost="0.0000001", at=inside_this_month())
        await record(session, user_id=other.id, cost="3", at=inside_this_month())
        response = await admin_client.get(f"/api/admin/requests.csv?user_id={other.id}")
        assert response.status_code == 200
        assert "attachment" in response.headers["content-disposition"]
        rows = list(csv.DictReader(io.StringIO(response.text)))
        assert len(rows) == 1
        assert Decimal(rows[0]["cost"]) == Decimal(3)
        every = list(
            csv.DictReader(io.StringIO((await admin_client.get("/api/admin/requests.csv")).text))
        )
        assert "E" not in {r["cost"] for r in every}
        assert any(r["cost"] == "0.000000100000" for r in every)

    async def test_formula_cells_are_neutralised(
        self, admin_client: httpx.AsyncClient, session: AsyncSession, seeded: Seeded
    ) -> None:
        evil = User(issuer="https://idp.test", subject="e", email="=HYPERLINK(1)@example.org")
        session.add(evil)
        await session.flush()
        await record(session, user_id=evil.id, at=inside_this_month())
        rows = list(
            csv.DictReader(io.StringIO((await admin_client.get("/api/admin/requests.csv")).text))
        )
        assert rows[0]["user"].startswith("'=")

    async def test_oversized_export_is_refused_not_cut(
        self,
        admin_client: httpx.AsyncClient,
        session: AsyncSession,
        seeded: Seeded,
        monkeypatch: object,
    ) -> None:
        import gateway.request_log as rl

        monkeypatch.setattr(rl, "MAX_EXPORT_ROWS", 1)  # type: ignore[attr-defined]
        await record(session, at=inside_this_month(1))
        await record(session, at=inside_this_month(2))
        response = await admin_client.get("/api/admin/requests.csv")
        assert response.status_code == 400
        assert response.json()["error"]["code"] == "export_too_large"


class TestAccess:
    async def test_admin_routes_need_admin(
        self,
        app: object,
        client: httpx.AsyncClient,
        seeded: Seeded,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        for path in ("/api/admin/requests", "/api/admin/requests.csv"):
            assert (await client.get(path)).status_code == 401
        as_user(app, await make_admin(session_factory, seeded, admin=False))
        for path in ("/api/admin/requests", "/api/admin/requests.csv"):
            assert (await client.get(path)).status_code == 403

    async def test_me_requires_a_session(self, client: httpx.AsyncClient, seeded: Seeded) -> None:
        for path in ("/api/me/requests", "/api/me/requests.csv"):
            assert (await client.get(path)).status_code == 401

    async def test_a_person_sees_only_their_own(
        self, app: object, client: httpx.AsyncClient, session: AsyncSession, seeded: Seeded
    ) -> None:
        other = await _other(session)
        mine = await record(session, user_id=seeded.user.id, at=inside_this_month())
        await record(session, user_id=other.id, at=inside_this_month())
        await record(session, user_id=None, at=inside_this_month())
        as_user(app, seeded.user)
        body = (await client.get("/api/me/requests")).json()
        assert [r["request_id"] for r in body["items"]] == [mine.request_id]
        assert body["total"] == 1

    async def test_a_person_cannot_widen_the_user_filter(
        self, app: object, client: httpx.AsyncClient, session: AsyncSession, seeded: Seeded
    ) -> None:
        other = await _other(session)
        await record(session, user_id=other.id, at=inside_this_month())
        mine = await record(session, user_id=seeded.user.id, at=inside_this_month())
        as_user(app, seeded.user)
        for path in ("/api/me/requests", "/api/me/requests.csv"):
            r = await client.get(f"{path}?user_id={other.id}")
            assert r.status_code == 200
            assert str(other.id) not in r.text
        body = (await client.get(f"/api/me/requests?user_id={other.id}")).json()
        assert [x["request_id"] for x in body["items"]] == [mine.request_id]
        # and the admin surface stays closed to them
        assert (await client.get(f"/api/admin/requests?user_id={other.id}")).status_code == 403

    async def test_my_csv_and_no_content(
        self, app: object, client: httpx.AsyncClient, session: AsyncSession, seeded: Seeded
    ) -> None:
        await _row_with_content(session, user_id=seeded.user.id, at=inside_this_month())
        as_user(app, seeded.user)
        r = await client.get("/api/me/requests.csv")
        assert r.status_code == 200
        assert "my-requests-" in r.headers["content-disposition"]
        assert len(list(csv.DictReader(io.StringIO(r.text)))) == 1
        assert SECRET not in r.text and ERROR not in r.text
        assert SECRET not in (await client.get("/api/me/requests")).text


class TestIndexUse:
    async def test_the_listing_uses_a_created_at_index(
        self, session: AsyncSession, seeded: Seeded
    ) -> None:
        if session.bind.dialect.name != "sqlite":  # type: ignore[union-attr]
            pytest.skip("EXPLAIN QUERY PLAN is SQLite's; the PostgreSQL plan is checked by hand")
        now = datetime.now(UTC)
        period = Period(kind=None, label="x", start=now - timedelta(days=30), end=now)
        for f in (
            RequestLogFilter(period=period),
            RequestLogFilter(period=period, user_id=uuid.uuid4()),
            RequestLogFilter(period=period, group_id=uuid.uuid4()),
        ):
            stmt = _listing_query(f).compile(
                dialect=session.bind.dialect,
                compile_kwargs={"literal_binds": True},  # type: ignore[union-attr]
            )
            plan = " ".join(
                str(r[3])
                for r in (await session.execute(text(f"EXPLAIN QUERY PLAN {stmt}"))).all()
                if "usage_records" in str(r[3])
            )
            assert "ix_usage_" in plan and "created" in plan, plan
