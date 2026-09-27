"""`GET /admin/users/{id}/merge-preview` and `POST /admin/users/{id}/merge`
(ADR 0093 §7.1), over real HTTP: the refusal-to-status mapping, the typed
confirmation, and the one side effect `gateway.merge` deliberately leaves to
the caller -- disabling a dropped bundled login in the real Authelia file,
after the commit.
"""

from __future__ import annotations

import json
import uuid
from pathlib import Path
from typing import Any

import yaml
from fastapi import FastAPI
from gateway.deps import get_management_user
from gateway.models import DirectoryEntry, IdentityProvider, User
from gateway.secrets import SecretBox
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

BOX = SecretBox(["test-encryption-key-not-for-production"])
BUNDLED_ISSUER = "https://gw.example.org/authelia"

SEED = """users:
  source-login:
    disabled: false
    displayname: Source
    password: '$argon2id$v=19$m=65536,t=3,p=4$c2FsdA$aGFzaA'
    email: source@example.org
    groups: [users]
"""


def as_user(app: FastAPI, user: User) -> None:
    app.dependency_overrides[get_management_user] = lambda: user


async def make_admin_caller(session_factory: async_sessionmaker[AsyncSession]) -> User:
    async with session_factory() as session:
        admin = User(issuer="https://idp.test", subject="admin-sub", email="admin@example.org")
        admin.is_admin = True
        session.add(admin)
        await session.commit()
        await session.refresh(admin, attribute_names=["memberships"])
        return admin


async def make_user(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    email: str | None,
    issuer: str,
    subject: str,
) -> User:
    async with session_factory() as session:
        user = User(issuer=issuer, subject=subject, email=email)
        session.add(user)
        await session.commit()
        await session.refresh(user, attribute_names=["memberships"])
        return user


class TestRefusalMapping:
    async def test_no_such_source_is_404(
        self,
        app: FastAPI,
        client: Any,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        admin = await make_admin_caller(session_factory)
        as_user(app, admin)
        target = await make_user(
            session_factory, email="t@example.org", issuer="https://idp.test", subject="t"
        )
        response = await client.get(
            f"/api/admin/users/{uuid.uuid4()}/merge-preview", params={"into": str(target.id)}
        )
        assert response.status_code == 404, response.text

    async def test_source_is_caller_is_400(
        self,
        app: FastAPI,
        client: Any,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        admin = await make_admin_caller(session_factory)
        as_user(app, admin)
        target = await make_user(
            session_factory, email="t@example.org", issuer="https://idp.test", subject="t"
        )
        response = await client.get(
            f"/api/admin/users/{admin.id}/merge-preview", params={"into": str(target.id)}
        )
        assert response.status_code == 400, response.text


class TestMergeEndpoint:
    async def test_the_wrong_confirmation_is_refused(
        self,
        app: FastAPI,
        client: Any,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        admin = await make_admin_caller(session_factory)
        as_user(app, admin)
        source = await make_user(
            session_factory, email="source@example.org", issuer="https://idp.test", subject="s"
        )
        target = await make_user(
            session_factory, email="target@example.org", issuer="https://idp.test", subject="t"
        )
        response = await client.post(
            f"/api/admin/users/{source.id}/merge",
            json={"into": str(target.id), "confirm": "not-the-right-address", "reason": "dup"},
        )
        assert response.status_code == 400, response.text

    async def test_confirming_by_id_when_there_is_no_email(
        self,
        app: FastAPI,
        client: Any,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        admin = await make_admin_caller(session_factory)
        as_user(app, admin)
        source = await make_user(
            session_factory, email=None, issuer="https://idp.test", subject="s"
        )
        target = await make_user(
            session_factory, email="target@example.org", issuer="https://idp.test", subject="t"
        )
        response = await client.post(
            f"/api/admin/users/{source.id}/merge",
            json={"into": str(target.id), "confirm": str(source.id), "reason": "dup"},
        )
        assert response.status_code == 200, response.text

    async def test_a_successful_merge_end_to_end(
        self,
        app: FastAPI,
        client: Any,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        admin = await make_admin_caller(session_factory)
        as_user(app, admin)
        source = await make_user(
            session_factory, email="source@example.org", issuer="https://idp.test", subject="s"
        )
        target = await make_user(
            session_factory, email="target@example.org", issuer="https://idp.test", subject="t"
        )
        preview = await client.get(
            f"/api/admin/users/{source.id}/merge-preview", params={"into": str(target.id)}
        )
        assert preview.status_code == 200, preview.text
        assert preview.json()["counts"] is not None
        assert preview.json()["duplicate_rules_dropped"] == 0

        response = await client.post(
            f"/api/admin/users/{source.id}/merge",
            json={"into": str(target.id), "confirm": "source@example.org", "reason": "duplicate"},
        )
        assert response.status_code == 200, response.text
        assert response.json()["target_id"] == str(target.id)
        assert response.json()["duplicate_rules_dropped"] == 0

        async with session_factory() as session:
            assert await session.get(User, source.id) is None
            remaining_target = await session.get(User, target.id)
            assert remaining_target is not None
            assert remaining_target.merged_at is not None

    async def test_a_dropped_bundled_login_is_disabled_after_the_commit(
        self,
        app: FastAPI,
        client: Any,
        session_factory: async_sessionmaker[AsyncSession],
        tmp_path: Path,
    ) -> None:
        path = tmp_path / "users_database.yml"
        path.write_text(SEED)

        async with session_factory() as session:
            admin = User(issuer="https://idp.test", subject="admin-sub", email="admin@example.org")
            admin.is_admin = True
            session.add(admin)
            provider = IdentityProvider(
                name="authelia",
                issuer=BUNDLED_ISSUER,
                client_id="pystino-console",
                client_secret_encrypted=BOX.encrypt("s"),
                scopes=["openid"],
                kind="authelia",
                sync_config_encrypted=BOX.encrypt(json.dumps({"path": str(path)})),
            )
            session.add(provider)
            await session.flush()
            # The source's primary identity is at the bundled issuer, and the
            # target already has one there too -- this identity is what will
            # be dropped, and the bundled login bound to it is what must end
            # up disabled.
            source = User(
                issuer=BUNDLED_ISSUER, subject="source-opaque-sub", email="source@example.org"
            )
            target = User(
                issuer=BUNDLED_ISSUER, subject="target-opaque-sub", email="target@example.org"
            )
            session.add_all([source, target])
            await session.flush()
            session.add(
                DirectoryEntry(
                    provider_id=provider.id,
                    external_id="source-login",
                    username="source-login",
                    user_id=source.id,
                )
            )
            await session.commit()
            source_id, target_id, admin_id = source.id, target.id, admin.id

        async with session_factory() as session:
            admin_row = await session.get(User, admin_id)
            assert admin_row is not None
            await session.refresh(admin_row, attribute_names=["memberships"])
        as_user(app, admin_row)

        response = await client.post(
            f"/api/admin/users/{source_id}/merge",
            json={
                "into": str(target_id),
                "confirm": "source@example.org",
                "reason": "same person, both issuers",
            },
        )
        assert response.status_code == 200, response.text
        assert response.json()["bundled_logins_disabled"] == ["source-login"]

        data = yaml.safe_load(path.read_text())["users"]
        assert data["source-login"]["disabled"] is True
