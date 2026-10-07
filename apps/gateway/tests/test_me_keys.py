"""The /api/me/keys surface: revoke, and delete.

The two are deliberately different answers to "I no longer want this key":
revoking keeps the row as an audit trail, deleting removes it because a list
of dead keys grows without limit and only the owner can prune it. What delete
must never do is disturb the ledger — that is what every test here watches.
"""

from __future__ import annotations

import uuid

import httpx
from conftest import Seeded
from gateway.deps import get_management_user
from gateway.models import ApiKey, UsageRecord, UsageStatus, User
from gateway.types import utcnow
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker


def as_user(app: object, user: User) -> None:
    """Bypass the OIDC session cookie by overriding the dependency.

    Same reasoning as test_admin.py: the cookie path is covered end to end by
    scripts/test_oidc_flow.py against a real Keycloak.
    """
    app.dependency_overrides[get_management_user] = lambda: user  # type: ignore[attr-defined]


class TestRevokeKey:
    async def test_revoke_sets_revoked_and_keeps_the_row(
        self,
        app: object,
        client: httpx.AsyncClient,
        seeded: Seeded,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        as_user(app, seeded.user)
        response = await client.delete(f"/api/me/keys/{seeded.api_key.id}")
        assert response.status_code == 200
        assert response.json()["revoked_at"] is not None

        listing = (await client.get("/api/me/keys")).json()["items"]
        assert any(key["id"] == str(seeded.api_key.id) for key in listing)

    async def test_another_users_key_is_not_found(
        self,
        app: object,
        client: httpx.AsyncClient,
        seeded: Seeded,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        """Keys are per-person: the 404 reads as "no such key", not "exists"."""
        as_user(app, seeded.user)
        response = await client.delete(f"/api/me/keys/{uuid.uuid4()}")
        assert response.status_code == 403


class TestDeleteKey:
    async def test_delete_removes_the_row_but_not_the_ledger(
        self,
        app: object,
        client: httpx.AsyncClient,
        seeded: Seeded,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        """Spend stays attributed to the user; only the per-key link is dropped."""
        as_user(app, seeded.user)
        async with session_factory() as db:
            db.add(
                UsageRecord(
                    request_id="req-key-delete-1",
                    status=UsageStatus.COMPLETED,
                    user_id=seeded.user.id,
                    group_id=seeded.group.id,
                    api_key_id=seeded.api_key.id,
                    model_id=seeded.model.id,
                    model_name=seeded.model.name,
                    currency="EUR",
                )
            )
            await db.commit()

        response = await client.delete(f"/api/me/keys/{seeded.api_key.id}/permanent")
        assert response.status_code == 204

        listing = (await client.get("/api/me/keys")).json()["items"]
        assert all(key["id"] != str(seeded.api_key.id) for key in listing)

        async with session_factory() as db:
            keys = (await db.execute(select(ApiKey))).scalars().all()
            assert keys == []
            usage = (
                await db.execute(
                    select(UsageRecord).where(UsageRecord.request_id == "req-key-delete-1")
                )
            ).scalar_one()
            assert usage.api_key_id is None
            assert usage.user_id == seeded.user.id
            assert usage.group_id == seeded.group.id

    async def test_delete_a_revoked_key(
        self,
        app: object,
        client: httpx.AsyncClient,
        seeded: Seeded,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        """The common pruning case: revoke today, delete once nobody needs it."""
        as_user(app, seeded.user)
        assert (await client.delete(f"/api/me/keys/{seeded.api_key.id}")).status_code == 200
        assert (
            await client.delete(f"/api/me/keys/{seeded.api_key.id}/permanent")
        ).status_code == 204

        async with session_factory() as db:
            assert (await db.execute(select(ApiKey))).scalars().all() == []

    async def test_delete_works_on_an_expiring_key(
        self,
        app: object,
        client: httpx.AsyncClient,
        seeded: Seeded,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        as_user(app, seeded.user)
        async with session_factory() as db:
            key = ApiKey(
                user_id=seeded.user.id,
                prefix="gwk_test",
                key_hash="a" * 64,
                name="expiring",
                expires_at=utcnow(),
            )
            db.add(key)
            await db.commit()
            key_id = key.id

        assert (await client.delete(f"/api/me/keys/{key_id}/permanent")).status_code == 204

    async def test_another_users_key_is_not_found(
        self,
        app: object,
        client: httpx.AsyncClient,
        seeded: Seeded,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        as_user(app, seeded.user)
        response = await client.delete(f"/api/me/keys/{uuid.uuid4()}/permanent")
        assert response.status_code == 403
