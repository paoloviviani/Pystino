"""`identity_events`: append-only enforcement, the allowlist, and the API (ADR 0093).

No route in this stage writes a row yet — the actions this closes belong to
later steps and stages — so these tests exercise `record_event` directly and
the read-only endpoint, rather than a call site that does not exist.
"""

from __future__ import annotations

import uuid

import httpx
import pytest
from conftest import Seeded
from gateway.identity_events import DETAIL_ALLOWLIST, record_event
from gateway.models import IdentityEvent, IdentityEventAction, IdentityEventActor
from sqlalchemy import delete, select, update
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from test_admin import as_user, make_admin


class TestRecordEvent:
    async def test_writes_a_row_and_flushes_it(self, session: AsyncSession, seeded: Seeded) -> None:
        row = await record_event(
            session,
            actor_type=IdentityEventActor.SYSTEM,
            actor_label="cli:idp-check",
            action=IdentityEventAction.IDP_RESEED,
            target_user_id=seeded.user.id,
            target_label=seeded.user.email or "",
            detail={"old_issuer": "https://old.test", "new_issuer": "https://new.test"},
        )
        await session.commit()

        fetched = (
            await session.execute(select(IdentityEvent).where(IdentityEvent.id == row.id))
        ).scalar_one()
        assert fetched.action == IdentityEventAction.IDP_RESEED
        assert fetched.detail == {
            "old_issuer": "https://old.test",
            "new_issuer": "https://new.test",
        }
        assert fetched.at is not None

    async def test_rejects_a_detail_key_outside_the_allowlist(self, session: AsyncSession) -> None:
        with pytest.raises(ValueError, match="unexpected key"):
            await record_event(
                session,
                actor_type=IdentityEventActor.SYSTEM,
                actor_label="cli:test",
                action=IdentityEventAction.IDP_RESEED,
                detail={"old_issuer": "https://old.test", "client_secret": "leaked"},
            )

    async def test_every_action_has_an_allowlist_entry(self) -> None:
        assert set(DETAIL_ALLOWLIST) == set(IdentityEventAction)

    async def test_no_allowlisted_key_looks_like_a_secret(self) -> None:
        for keys in DETAIL_ALLOWLIST.values():
            for key in keys:
                lowered = key.lower()
                assert not any(bad in lowered for bad in ("password", "digest", "token", "secret"))

    @pytest.mark.parametrize(
        "action", [IdentityEventAction.USER_MERGE, IdentityEventAction.BREAK_GLASS]
    )
    @pytest.mark.parametrize("reason", [None, "", "   "])
    async def test_merge_and_break_glass_take_no_reason(
        self, session: AsyncSession, action: IdentityEventAction, reason: str | None
    ) -> None:
        row = await record_event(
            session,
            actor_type=IdentityEventActor.USER,
            actor_label="admin@example.org",
            action=action,
            reason=reason,
        )
        assert row.reason is None

    async def test_a_given_reason_is_stripped_and_kept(self, session: AsyncSession) -> None:
        row = await record_event(
            session,
            actor_type=IdentityEventActor.USER,
            actor_label="admin@example.org",
            action=IdentityEventAction.BREAK_GLASS,
            reason="  lost every admin ",
        )
        assert row.reason == "lost every admin"

    async def test_no_action_requires_a_reason(self, session: AsyncSession) -> None:
        row = await record_event(
            session,
            actor_type=IdentityEventActor.SYSTEM,
            actor_label="system",
            action=IdentityEventAction.ADMIN_REFUSED_LAST,
        )
        assert row.reason is None


class TestAppendOnly:
    """The database refuses UPDATE and DELETE outright — not just record_event."""

    async def _one_row_id(self, session: AsyncSession) -> uuid.UUID:
        row = await record_event(
            session,
            actor_type=IdentityEventActor.SYSTEM,
            actor_label="system",
            action=IdentityEventAction.ADMIN_REFUSED_LAST,
        )
        await session.commit()
        return row.id

    async def test_update_is_refused(self, session: AsyncSession) -> None:
        row_id = await self._one_row_id(session)
        with pytest.raises(DBAPIError, match="append-only"):
            # `update()`/`delete()`, not a raw string with the id interpolated:
            # SQLite stores `Uuid` columns as a bare 32-character hex string,
            # not the dashed form `str(uuid.UUID)` produces, so a hand-written
            # `WHERE id = :id` silently matches zero rows instead of the one
            # meant to prove the trigger fires — the ORM construct binds
            # through the column's own type and always matches correctly.
            stmt = update(IdentityEvent).where(IdentityEvent.id == row_id).values(reason="edited")
            await session.execute(stmt)
            await session.commit()

    async def test_delete_is_refused(self, session: AsyncSession) -> None:
        row_id = await self._one_row_id(session)
        with pytest.raises(DBAPIError, match="append-only"):
            await session.execute(delete(IdentityEvent).where(IdentityEvent.id == row_id))
            await session.commit()


class TestListIdentityEventsEndpoint:
    async def test_requires_admin(self, client: httpx.AsyncClient, seeded: Seeded) -> None:
        assert (await client.get("/api/admin/identity-events")).status_code == 401

    async def test_lists_newest_first_and_paginates(
        self,
        app: object,
        client: httpx.AsyncClient,
        seeded: Seeded,
        session: AsyncSession,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        as_user(app, await make_admin(session_factory, seeded))
        for i in range(3):
            await record_event(
                session,
                actor_type=IdentityEventActor.SYSTEM,
                actor_label="system",
                action=IdentityEventAction.IDP_RESEED,
                detail={"old_issuer": f"https://old-{i}.test", "new_issuer": "https://new.test"},
            )
        await session.commit()

        response = await client.get("/api/admin/identity-events", params={"limit": 2})
        assert response.status_code == 200
        body = response.json()
        assert body["total"] == 3
        assert len(body["items"]) == 2
        # Newest first: the last one written is the first one returned.
        assert body["items"][0]["detail"]["old_issuer"] == "https://old-2.test"

    async def test_filters_by_user_id_on_either_side(
        self,
        app: object,
        client: httpx.AsyncClient,
        seeded: Seeded,
        session: AsyncSession,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        admin = await make_admin(session_factory, seeded)
        as_user(app, admin)
        other_id = uuid.uuid4()

        await record_event(
            session,
            actor_type=IdentityEventActor.USER,
            actor_label="admin@example.org",
            actor_user_id=admin.id,
            action=IdentityEventAction.ADMIN_REFUSED_LAST,
            target_user_id=other_id,
            target_label="other@example.org",
        )
        await record_event(
            session,
            actor_type=IdentityEventActor.SYSTEM,
            actor_label="system",
            action=IdentityEventAction.IDP_RESEED,
        )
        await session.commit()

        response = await client.get("/api/admin/identity-events", params={"user_id": str(admin.id)})
        assert response.status_code == 200
        body = response.json()
        assert body["total"] == 1
        assert body["items"][0]["action"] == "admin.refused_last"

    async def test_filters_by_action(
        self,
        app: object,
        client: httpx.AsyncClient,
        seeded: Seeded,
        session: AsyncSession,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        as_user(app, await make_admin(session_factory, seeded))
        await record_event(
            session,
            actor_type=IdentityEventActor.SYSTEM,
            actor_label="system",
            action=IdentityEventAction.IDP_RESEED,
        )
        await record_event(
            session,
            actor_type=IdentityEventActor.SYSTEM,
            actor_label="system",
            action=IdentityEventAction.ADMIN_REFUSED_LAST,
        )
        await session.commit()

        response = await client.get("/api/admin/identity-events", params={"action": "idp.reseed"})
        assert response.status_code == 200
        body = response.json()
        assert body["total"] == 1
        assert body["items"][0]["action"] == "idp.reseed"
