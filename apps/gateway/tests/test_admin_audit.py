"""The admin audit log (issue P9): what is recorded, what is never recorded,
and that the event commits with the change and not without it."""

from __future__ import annotations

import json
import uuid
from datetime import timedelta
from typing import Any

import httpx
import pytest
from conftest import Seeded
from gateway.models import AdminAuditEvent, User
from gateway.types import utcnow
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from test_admin import as_user, make_admin

SECRET = "sk-super-secret-value-123456"
HEADER_SECRET = "Bearer hdr-secret-999"


@pytest.fixture
async def admin(
    app: object,
    client: httpx.AsyncClient,
    seeded: Seeded,
    session_factory: async_sessionmaker[AsyncSession],
) -> User:
    user = await make_admin(session_factory, seeded)
    as_user(app, user)
    return user


async def events(
    session_factory: async_sessionmaker[AsyncSession], prefix: str = ""
) -> list[AdminAuditEvent]:
    async with session_factory() as session:
        rows = (
            (await session.execute(select(AdminAuditEvent).order_by(AdminAuditEvent.created_at)))
            .scalars()
            .all()
        )
    return [r for r in rows if r.action.startswith(prefix)]


async def only(session_factory: async_sessionmaker[AsyncSession], action: str) -> AdminAuditEvent:
    found = [e for e in await events(session_factory) if e.action == action]
    assert len(found) == 1, [e.action for e in await events(session_factory)]
    return found[0]


class TestProviders:
    async def test_create_update_delete_and_no_secret_anywhere(
        self,
        admin: User,
        client: httpx.AsyncClient,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        created = await client.post(
            "/api/admin/providers",
            json={
                "name": "acme",
                "base_url": "https://user:pw-in-url@acme.test/v1",
                "api_key": SECRET,
                "extra_headers": {"Authorization": HEADER_SECRET},
            },
        )
        assert created.status_code == 201, created.text
        pid = created.json()["id"]

        rotated = await client.patch(
            f"/api/admin/providers/{pid}",
            json={"api_key": SECRET + "-2", "description": "new text"},
        )
        assert rotated.status_code == 200, rotated.text
        swapped = await client.patch(
            f"/api/admin/providers/{pid}", json={"extra_headers": {"Authorization": "Bearer other"}}
        )
        assert swapped.status_code == 200, swapped.text
        cleared = await client.patch(f"/api/admin/providers/{pid}", json={"api_key": ""})
        assert cleared.status_code == 200, cleared.text
        assert (await client.delete(f"/api/admin/providers/{pid}")).status_code == 204

        rows = await events(session_factory, "provider.")
        assert [r.action for r in rows] == [
            "provider.create",
            "provider.update",
            "provider.update",
            "provider.update",
            "provider.delete",
        ]
        create, rotate, headers, clear, delete = (r.changes for r in rows)
        assert create["api_key"] == {"after": "set"}
        assert create["extra_headers"]["after"] == ["Authorization"]
        assert create["base_url"]["after"] == "https://acme.test/v1"
        assert rotate["api_key"] == {"after": "rotated"}
        assert rotate["description"]["after"] == "new text"
        assert headers == {"extra_headers_values": {"after": "rotated"}}
        assert clear == {"api_key": {"after": "cleared"}}
        assert delete["name"]["before"] == "acme"

        dump = json.dumps([r.changes for r in await events(session_factory)])
        for secret in (SECRET, HEADER_SECRET, "Bearer other", "pw-in-url"):
            assert secret not in dump
        assert rows[0].actor_user_id == admin.id
        assert rows[0].actor_label == admin.email
        assert rows[0].target_type == "provider"
        assert rows[0].target_id == pid


class TestModelsAndPrices:
    async def test_model_lifecycle_records_only_changed_fields(
        self,
        admin: User,
        client: httpx.AsyncClient,
        seeded: Seeded,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        created = await client.post(
            "/api/admin/models",
            json={
                "name": "m1",
                "upstream_model": "up/m1",
                "provider_id": str(seeded.provider.id),
            },
        )
        assert created.status_code == 201, created.text
        mid = created.json()["id"]
        patched = await client.patch(
            f"/api/admin/models/{mid}", json={"is_active": False, "description": "x"}
        )
        assert patched.status_code == 200, patched.text
        price = await client.post(
            f"/api/admin/models/{mid}/prices",
            json={"input_per_mtok": "1.5", "output_per_mtok": "3", "currency": "EUR"},
        )
        assert price.status_code == 201, price.text
        assert (await client.delete(f"/api/admin/models/{mid}")).status_code == 204

        update = await only(session_factory, "model.update")
        assert update.changes == {
            "description": {"before": None, "after": "x"},
            "is_active": {"before": True, "after": False},
        }
        assert update.target_label == "m1"
        pc = await only(session_factory, "price.create")
        assert pc.changes["input_per_mtok"]["after"].startswith("1.5")
        assert pc.target_type == "price"
        assert pc.target_label == "m1"
        deleted = await only(session_factory, "model.delete")
        assert deleted.changes["name"] == {"before": "m1"}
        assert (await only(session_factory, "model.create")).changes["name"] == {"after": "m1"}

    async def test_a_no_op_patch_writes_no_event(
        self,
        admin: User,
        client: httpx.AsyncClient,
        seeded: Seeded,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        res = await client.patch(f"/api/admin/models/{seeded.model.id}", json={"is_active": True})
        assert res.status_code == 200
        assert await events(session_factory) == []

    async def test_a_failed_change_leaves_no_event(
        self,
        admin: User,
        client: httpx.AsyncClient,
        seeded: Seeded,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        body = {
            "name": seeded.model.name,
            "upstream_model": "u",
            "provider_id": str(seeded.provider.id),
        }
        assert (await client.post("/api/admin/models", json=body)).status_code == 409
        assert await events(session_factory) == []


class TestGrantsAndLimits:
    async def test_grants_record_only_real_changes(
        self,
        admin: User,
        client: httpx.AsyncClient,
        seeded: Seeded,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        g = f"/api/admin/groups/{seeded.group.id}/models/{seeded.model.id}"
        u = f"/api/admin/users/{seeded.user.id}/models/{seeded.model.id}"
        # The seeded group already holds the grant: removing it is the first
        # real change, and the repeats are idempotent no-ops.
        for method, url in (
            ("DELETE", g),
            ("PUT", g),
            ("PUT", g),
            ("PUT", u),
            ("PUT", u),
            ("DELETE", g),
            ("DELETE", g),
            ("DELETE", u),
            ("DELETE", u),
        ):
            assert (await client.request(method, url)).status_code == 204
        rows = await events(session_factory)
        assert [e.action for e in rows] == [
            "grant.group.remove",
            "grant.group.add",
            "grant.user.add",
            "grant.group.remove",
            "grant.user.remove",
        ]
        assert rows[1].changes["model"] == {"after": seeded.model.name}
        assert rows[1].changes["group"] == {"after": seeded.group.name}
        assert rows[4].changes["model"] == {"before": seeded.model.name}

    async def test_limit_create_update_delete(
        self,
        admin: User,
        client: httpx.AsyncClient,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        created = await client.post(
            "/api/admin/limits",
            json={
                "name": "cap",
                "scope": "global",
                "metric": "cost",
                "window_seconds": 60,
                "limit_value": "5",
            },
        )
        assert created.status_code == 201, created.text
        rid = created.json()["id"]
        assert (
            await client.patch(f"/api/admin/limits/{rid}", json={"limit_value": "9"})
        ).status_code == 200
        assert (await client.delete(f"/api/admin/limits/{rid}")).status_code == 204
        update = await only(session_factory, "limit.update")
        assert set(update.changes) == {"limit_value"}
        assert update.changes["limit_value"]["after"].startswith("9")
        await only(session_factory, "limit.create")
        await only(session_factory, "limit.delete")


class TestAliases:
    async def test_alias_events_are_in_both_logs(
        self,
        admin: User,
        client: httpx.AsyncClient,
        seeded: Seeded,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        created = await client.post(
            "/api/admin/aliases",
            json={"name": "fast", "target_model_id": str(seeded.model.id)},
        )
        assert created.status_code == 201, created.text
        aid = created.json()["id"]
        patched = await client.patch(f"/api/admin/aliases/{aid}", json={"display_name": "Fast"})
        assert patched.status_code == 200, patched.text
        assert (await client.delete(f"/api/admin/aliases/{aid}")).status_code == 204
        assert [e.action for e in await events(session_factory)] == [
            "alias.create",
            "alias.update",
            "alias.delete",
        ]
        update = await only(session_factory, "alias.update")
        assert update.changes == {"display_name": {"before": None, "after": "Fast"}}
        assert (await only(session_factory, "alias.create")).changes["target"] == {
            "after": seeded.model.name
        }


class TestListing:
    async def seed(self, session_factory: async_sessionmaker[AsyncSession], admin: User) -> None:
        now = utcnow()
        async with session_factory() as session:
            for i, (action, target_type, label) in enumerate(
                [
                    ("model.create", "model", "a"),
                    ("grant.group.add", "grant", "b"),
                    ("grant.user.add", "grant", "c"),
                ]
            ):
                session.add(
                    AdminAuditEvent(
                        id=uuid.uuid4(),
                        created_at=now - timedelta(hours=3 - i),
                        actor_user_id=admin.id if i < 2 else None,
                        actor_label=admin.email if i < 2 else "gone@example.org",
                        action=action,
                        target_type=target_type,
                        target_label=label,
                        changes={},
                    )
                )
            await session.commit()

    async def get(self, client: httpx.AsyncClient, **params: Any) -> dict[str, Any]:
        res = await client.get("/api/admin/audit", params=params)
        assert res.status_code == 200, res.text
        return res.json()

    async def test_newest_first_with_filters_and_envelope(
        self,
        admin: User,
        client: httpx.AsyncClient,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        await self.seed(session_factory, admin)
        body = await self.get(client)
        assert [i["target_label"] for i in body["items"]] == ["c", "b", "a"]
        assert body["total"] == 3 and body["limit"] == 50 and body["offset"] == 0
        assert (await self.get(client, limit=1, offset=1))["items"][0]["target_label"] == "b"
        assert (await self.get(client, action="grant."))["total"] == 2
        assert (await self.get(client, target_type="model"))["total"] == 1
        assert (await self.get(client, actor="gone@"))["total"] == 1
        assert (await self.get(client, actor=str(admin.id)))["total"] == 2
        cutoff = (utcnow() - timedelta(hours=2, minutes=30)).isoformat()
        assert (await self.get(client, start=cutoff))["total"] == 2
        assert (await self.get(client, end=cutoff))["total"] == 1

    async def test_admin_only(
        self,
        app: object,
        client: httpx.AsyncClient,
        seeded: Seeded,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        assert (await client.get("/api/admin/audit")).status_code == 401
        as_user(app, await make_admin(session_factory, seeded, admin=False))
        assert (await client.get("/api/admin/audit")).status_code == 403
