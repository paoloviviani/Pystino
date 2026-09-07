"""Group management endpoints (ADR 0050).

The boundary under test: membership is edited on *manual* groups only. An
OIDC-sourced group is the identity provider's — a member added to one would be
reconciled away at the next login, which is access granted and then silently
revoked, and both halves of that are worse than a 400 with the reason.
"""

from __future__ import annotations

from typing import Any

import httpx
import pytest_asyncio
from gateway.models import (
    Group,
    GroupModelAccess,
    GroupSource,
    Membership,
    MembershipSource,
    User,
)
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from test_my_limits import session_cookie
from test_oidc_policy import admin_session as admin_session


@pytest_asyncio.fixture
async def manual_group(
    client: httpx.AsyncClient, admin_session: dict[str, str]
) -> dict[str, Any]:
    response = await client.post(
        "/api/admin/groups",
        json={"name": "contractors", "description": "Bought-in hands"},
        headers=admin_session,
    )
    assert response.status_code == 201
    return response.json()


@pytest_asyncio.fixture
async def oidc_group(
    session_factory: async_sessionmaker[AsyncSession],
) -> Group:
    async with session_factory() as session:
        group = Group(name="idp-team", source=GroupSource.OIDC)
        session.add(group)
        await session.commit()
        return group


class TestCreateGroup:
    async def test_a_group_is_created_manual(
        self, client: httpx.AsyncClient, admin_session: dict[str, str]
    ) -> None:
        response = await client.post(
            "/api/admin/groups",
            json={"name": "contractors"},
            headers=admin_session,
        )
        assert response.status_code == 201
        assert response.json()["source"] == "manual"
        assert response.json()["member_count"] == 0

    async def test_a_duplicate_name_is_refused(
        self, client: httpx.AsyncClient, admin_session: dict[str, str]
    ) -> None:
        payload = {"name": "duplicated"}
        assert (
            await client.post("/api/admin/groups", json=payload, headers=admin_session)
        ).status_code == 201
        again = await client.post("/api/admin/groups", json=payload, headers=admin_session)
        assert again.status_code == 400
        assert again.json()["error"]["code"] == "group_exists"


class TestDeleteGroup:
    async def test_model_access_dies_with_the_group(
        self,
        client: httpx.AsyncClient,
        admin_session: dict[str, str],
        seeded: Any,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        # The seeded deployment has model access for the research group: this
        # is the deletion's real blast radius, not the empty row.
        group_id = str(seeded.group.id)
        deleted = await client.delete(f"/api/admin/groups/{group_id}", headers=admin_session)
        assert deleted.status_code == 204
        async with session_factory() as session:
            remaining = (
                await session.execute(
                    select(GroupModelAccess).where(GroupModelAccess.group_id == seeded.group.id)
                )
            ).scalars().all()
            assert remaining == []

    async def test_an_unknown_group_is_404(
        self, client: httpx.AsyncClient, admin_session: dict[str, str]
    ) -> None:
        response = await client.delete(
            "/api/admin/groups/00000000-0000-0000-0000-000000000000",
            headers=admin_session,
        )
        assert response.status_code == 404


class TestMembers:
    async def test_add_and_list_and_remove(
        self,
        client: httpx.AsyncClient,
        admin_session: dict[str, str],
        seeded: Any,
        manual_group: dict[str, Any],
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        user_id = str(seeded.user.id)
        added = await client.post(
            f"/api/admin/groups/{manual_group['id']}/members",
            json={"user_id": user_id},
            headers=admin_session,
        )
        assert added.status_code == 204

        members = await client.get(
            f"/api/admin/groups/{manual_group['id']}/members", headers=admin_session
        )
        assert members.status_code == 200
        assert [item["id"] for item in members.json()["items"]] == [user_id]

        removed = await client.delete(
            f"/api/admin/groups/{manual_group['id']}/members/{user_id}",
            headers=admin_session,
        )
        assert removed.status_code == 204

    async def test_a_duplicate_member_is_refused(
        self,
        client: httpx.AsyncClient,
        admin_session: dict[str, str],
        seeded: Any,
        manual_group: dict[str, Any],
    ) -> None:
        payload = {"user_id": str(seeded.user.id)}
        await client.post(
            f"/api/admin/groups/{manual_group['id']}/members",
            json=payload,
            headers=admin_session,
        )
        again = await client.post(
            f"/api/admin/groups/{manual_group['id']}/members",
            json=payload,
            headers=admin_session,
        )
        assert again.status_code == 400

    async def test_removing_the_default_billing_group_clears_it(
        self,
        client: httpx.AsyncClient,
        admin_session: dict[str, str],
        seeded: Any,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        # The seeded user's default billing group IS this group: after removal
        # the default must be gone, or the next request bills a group the user
        # no longer belongs to — which is how a spend ends up attributed to
        # someone the access rule no longer names.
        async with session_factory() as session:
            group = Group(name="solo", source=GroupSource.MANUAL)
            session.add(group)
            await session.flush()
            user = (
                await session.execute(select(User).where(User.id == seeded.user.id))
            ).scalar_one()
            user.default_billing_group_id = group.id
            session.add(Membership(user_id=user.id, group_id=group.id))
            group_id, user_id = group.id, user.id
            await session.commit()

        removed = await client.delete(
            f"/api/admin/groups/{group_id}/members/{user_id}", headers=admin_session
        )
        assert removed.status_code == 204
        async with session_factory() as session:
            refreshed = (
                await session.execute(select(User).where(User.id == user_id))
            ).scalar_one()
            assert refreshed.default_billing_group_id is None

    async def test_membership_of_an_oidc_group_is_allowed(
        self,
        client: httpx.AsyncClient,
        admin_session: dict[str, str],
        seeded: Any,
        oidc_group: Group,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        """Refused until ADR 0057, and the refusal's premise is what changed.

        It was refused because the next login would have replaced the group's
        members from the token, so the grant would have disappeared without a
        word. Now the grant is recorded as the administrator's and no login
        touches it, which is exactly what an administrator asking for this
        wants: somebody in a directory's group whom the directory does not
        name.
        """
        response = await client.post(
            f"/api/admin/groups/{oidc_group.id}/members",
            json={"user_id": str(seeded.user.id)},
            headers=admin_session,
        )
        assert response.status_code == 204

        async with session_factory() as session:
            membership = (
                await session.execute(
                    select(Membership).where(
                        Membership.group_id == oidc_group.id,
                        Membership.user_id == seeded.user.id,
                    )
                )
            ).scalar_one()
            assert membership.source is MembershipSource.MANUAL

    async def test_an_unknown_user_is_404(
        self, client: httpx.AsyncClient, admin_session: dict[str, str], manual_group: dict[str, Any]
    ) -> None:
        response = await client.post(
            f"/api/admin/groups/{manual_group['id']}/members",
            json={"user_id": "00000000-0000-0000-0000-000000000000"},
            headers=admin_session,
        )
        assert response.status_code == 404

    async def test_the_member_listing_searches(
        self,
        client: httpx.AsyncClient,
        admin_session: dict[str, str],
        seeded: Any,
        manual_group: dict[str, Any],
    ) -> None:
        await client.post(
            f"/api/admin/groups/{manual_group['id']}/members",
            json={"user_id": str(seeded.user.id)},
            headers=admin_session,
        )
        found = await client.get(
            f"/api/admin/groups/{manual_group['id']}/members?q=member",
            headers=admin_session,
        )
        assert found.json()["total"] == 1
        none = await client.get(
            f"/api/admin/groups/{manual_group['id']}/members?q=zzz",
            headers=admin_session,
        )
        assert none.json()["total"] == 0


class TestAdminOnly:
    async def test_a_non_admin_cannot_manage_groups(
        self, client: httpx.AsyncClient, seeded: Any, app: Any
    ) -> None:
        # Identity-adjacent, so the 403s are asserted rather than assumed: the
        # group a user belongs to decides what they may bill.
        cookie = session_cookie(seeded.user.id, app)
        assert (
            await client.post("/api/admin/groups", json={"name": "x"}, headers=cookie)
        ).status_code == 403
        assert (
            await client.get("/api/admin/groups", headers=cookie)
        ).status_code == 403
