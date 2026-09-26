"""The disable cascade (ADR 0093 §9.1, gateway column): one PATCH
`is_active=false` ends every session and credential a person already
holds, not just requests from here on. Enable reverses only the flag and
Authelia's own — everything the cascade deleted stays deleted.
"""

from __future__ import annotations

import json
import uuid
from datetime import timedelta
from pathlib import Path

import httpx
import yaml
from conftest import Seeded
from fastapi import FastAPI
from gateway.deps import get_management_user
from gateway.models import (
    ApiKey,
    IdentityEvent,
    IdentityEventAction,
    IdentityProvider,
    IdpAuthorizationCode,
    RefreshCredential,
    User,
)
from gateway.secrets import SecretBox
from gateway.types import utcnow
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

BOX = SecretBox(["test-encryption-key-not-for-production"])
ISS = "https://gw.example.org/authelia"

SEED = """users:
  admin:
    disabled: false
    displayname: Admin
    password: '$argon2id$v=19$m=65536,t=3,p=4$c2FsdA$aGFzaA'
    email: admin@example.org
    groups: [users]
  target:
    disabled: false
    displayname: Target
    password: '$argon2id$v=19$m=65536,t=3,p=4$c2FsdA$aGFzaA'
    email: target@example.org
    groups: [users]
"""


def as_user(app: object, user: User) -> None:
    app.dependency_overrides[get_management_user] = lambda: user  # type: ignore[attr-defined]


async def make_admin(
    session_factory: async_sessionmaker[AsyncSession], seeded: Seeded, *, admin: bool = True
) -> User:
    async with session_factory() as session:
        user = (await session.execute(select(User).where(User.id == seeded.user.id))).scalar_one()
        user.is_admin = admin
        await session.commit()
        await session.refresh(user, attribute_names=["memberships"])
        return user


async def _bundled_provider(
    session_factory: async_sessionmaker[AsyncSession], tmp_path: Path
) -> tuple[IdentityProvider, Path]:
    path = tmp_path / "users_database.yml"
    path.write_text(SEED)
    async with session_factory() as session:
        row = IdentityProvider(
            name="authelia",
            issuer=ISS,
            client_id="pystino-console",
            client_secret_encrypted=BOX.encrypt("s"),
            scopes=["openid"],
            groups_claim="groups",
            fetch_userinfo=False,
            group_mappings=[],
            link_by_email=False,
            kind="authelia",
            group_source="none",
            group_sync="never",
            sync_adapter="none",
            admin_source="console",
            admin_values=[],
            sync_deprovision="disable",
            sync_create_users=True,
            sync_confirmed=False,
            sync_interval_minutes=60,
            sync_config_encrypted=BOX.encrypt(json.dumps({"path": str(path)})),
        )
        session.add(row)
        await session.commit()
        await session.refresh(row)
        return row, path


async def _target_with_credentials(
    session_factory: async_sessionmaker[AsyncSession], provider: IdentityProvider
) -> uuid.UUID:
    from gateway.models import DirectoryEntry

    async with session_factory() as session:
        user = User(
            issuer="https://old.example.org", subject="target-sub", email="target@example.org"
        )
        session.add(user)
        await session.flush()
        session.add(
            DirectoryEntry(
                provider_id=provider.id, external_id="target", username="target", user_id=user.id
            )
        )
        session.add(
            RefreshCredential(
                user_id=user.id,
                client="galopin",
                prefix="rc_p",
                secret_hash="h" * 64,
                expires_at=utcnow() + timedelta(days=90),
            )
        )
        session.add(
            IdpAuthorizationCode(
                code_hash="c" * 64,
                client_id="console",
                redirect_uri="https://gw.example.org/callback",
                user_id=user.id,
                code_challenge="challenge",
            )
        )
        session.add(
            ApiKey(user_id=user.id, prefix="gwa_minted", key_hash="m" * 64, minted_by="console")
        )
        session.add(
            ApiKey(user_id=user.id, prefix="gwk_personal", key_hash="p" * 64, name="my key")
        )
        await session.commit()
        return user.id


class TestDisable:
    async def test_the_full_cascade(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        seeded: Seeded,
        session_factory: async_sessionmaker[AsyncSession],
        tmp_path: Path,
    ) -> None:
        as_user(app, await make_admin(session_factory, seeded))
        provider, path = await _bundled_provider(session_factory, tmp_path)
        target_id = await _target_with_credentials(session_factory, provider)

        before = utcnow()
        response = await client.patch(
            f"/api/admin/users/{target_id}", json={"is_active": False}
        )
        assert response.status_code == 200, response.text
        assert response.json()["is_active"] is False

        async with session_factory() as session:
            row = (await session.execute(select(User).where(User.id == target_id))).scalar_one()
            assert row.is_active is False
            assert row.deactivated_by == "manual"
            assert row.sessions_valid_after is not None
            assert row.sessions_valid_after >= before

            assert (
                await session.execute(
                    select(RefreshCredential).where(RefreshCredential.user_id == target_id)
                )
            ).scalar_one_or_none() is None
            assert (
                await session.execute(
                    select(IdpAuthorizationCode).where(IdpAuthorizationCode.user_id == target_id)
                )
            ).scalar_one_or_none() is None
            keys = (
                (await session.execute(select(ApiKey).where(ApiKey.user_id == target_id)))
                .scalars()
                .all()
            )
            assert {k.prefix for k in keys} == {"gwk_personal"}

            events = (
                (
                    await session.execute(
                        select(IdentityEvent).where(IdentityEvent.target_user_id == target_id)
                    )
                )
                .scalars()
                .all()
            )
            assert {e.action for e in events} == {
                IdentityEventAction.USER_DISABLE,
                IdentityEventAction.SESSIONS_REVOKE,
                IdentityEventAction.LOGIN_DISABLE,
            }

        data = yaml.safe_load(path.read_text())["users"]
        assert data["target"]["disabled"] is True
        # The other login is untouched — this disable acted on one entry.
        assert data["admin"]["disabled"] is False

    async def test_personal_keys_are_kept_not_deleted(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        seeded: Seeded,
        session_factory: async_sessionmaker[AsyncSession],
        tmp_path: Path,
    ) -> None:
        as_user(app, await make_admin(session_factory, seeded))
        provider, _ = await _bundled_provider(session_factory, tmp_path)
        target_id = await _target_with_credentials(session_factory, provider)

        await client.patch(f"/api/admin/users/{target_id}", json={"is_active": False})

        async with session_factory() as session:
            personal = (
                await session.execute(
                    select(ApiKey).where(
                        ApiKey.user_id == target_id, ApiKey.prefix == "gwk_personal"
                    )
                )
            ).scalar_one()
            assert personal.revoked_at is None


class TestEnable:
    async def test_reverses_only_the_flag_and_authelia_disabled(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        seeded: Seeded,
        session_factory: async_sessionmaker[AsyncSession],
        tmp_path: Path,
    ) -> None:
        as_user(app, await make_admin(session_factory, seeded))
        provider, path = await _bundled_provider(session_factory, tmp_path)
        target_id = await _target_with_credentials(session_factory, provider)

        await client.patch(f"/api/admin/users/{target_id}", json={"is_active": False})
        async with session_factory() as session:
            disabled_row = (
                await session.execute(select(User).where(User.id == target_id))
            ).scalar_one()
            valid_after_disable = disabled_row.sessions_valid_after

        response = await client.patch(f"/api/admin/users/{target_id}", json={"is_active": True})
        assert response.status_code == 200, response.text
        assert response.json()["is_active"] is True

        assert yaml.safe_load(path.read_text())["users"]["target"]["disabled"] is False

        async with session_factory() as session:
            row = (await session.execute(select(User).where(User.id == target_id))).scalar_one()
            assert row.is_active is True
            assert row.deactivated_by is None
            # Not restored: the disable's clock stands, so any session
            # minted before it stays refused even after re-enabling.
            assert row.sessions_valid_after == valid_after_disable
            # The deleted minted key and refresh credential do not come back.
            assert (
                await session.execute(
                    select(RefreshCredential).where(RefreshCredential.user_id == target_id)
                )
            ).scalar_one_or_none() is None
