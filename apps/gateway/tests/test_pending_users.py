"""Pre-registration ("invite") for any provider (ADR 0095 §2).

An administrator invites a person who has never signed in; their first
sign-in claims the pending row by verified email instead of minting a second
account. The bundled Authelia path is unchanged — its tests live in
`test_bundled_users_admin.py` and stay green untouched.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any
from uuid import UUID

import httpx
import pytest
from conftest import LEGACY_GROUPS, Seeded
from gateway.config import OIDCSettings, Settings
from gateway.identity_registry import record_from_env
from gateway.models import (
    DirectoryEntry,
    Group,
    IdentityEvent,
    IdentityEventAction,
    Membership,
    MembershipSource,
    User,
    UserIdentity,
)
from gateway.oidc import (
    INVITE_EXPIRY_DAYS,
    PENDING_USER_ISSUER,
    ProvisioningRefused,
    claim_pending_by_email,
    sign_in,
)
from gateway.oidc_policy import OIDCPolicy
from gateway.types import utcnow
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from test_admin import make_admin
from test_my_limits import session_cookie

ISS = "https://idp.test"


def _settings(**overrides: Any) -> Settings:
    return Settings(
        oidc=OIDCSettings(enabled=True, issuer=ISS, client_id="c", **LEGACY_GROUPS, **overrides)
    )


async def _invite(
    client: httpx.AsyncClient,
    headers: dict[str, str],
    email: str,
    **extra: Any,
) -> httpx.Response:
    return await client.post("/api/admin/users", json={"email": email, **extra}, headers=headers)


async def _admin_session(
    app: Any, session_factory: async_sessionmaker[AsyncSession], seeded: Seeded
) -> dict[str, str]:
    admin = await make_admin(session_factory=session_factory, seeded=seeded)
    return session_cookie(admin.id, app)


class TestInvite:
    async def test_invite_writes_a_pending_user_with_no_login(
        self,
        app: Any,
        client: httpx.AsyncClient,
        seeded: Seeded,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        headers = await _admin_session(app, session_factory, seeded)
        response = await _invite(
            client, headers, "lena@example.org", display_name="Lena", groups=[]
        )
        assert response.status_code == 201, response.text
        body = response.json()
        assert body["user"]["email"] == "lena@example.org"
        assert body["sign_in_url"].endswith("/console")
        assert "lena@example.org" in body["invite_text"]
        assert body["email_sent"] is False

        async with session_factory() as session:
            user = (
                await session.execute(
                    select(User).where(User.email_normalized == "lena@example.org")
                )
            ).scalar_one()
            assert user.issuer == PENDING_USER_ISSUER
            assert user.is_active is True
            assert user.invited_at is not None
            assert user.invited_by == seeded.user.id
            assert user.admin_edited_fields == ["email"]
            # No login anywhere: no bundled username, no directory entry.
            assert user.username is None
            assert (
                await session.execute(
                    select(DirectoryEntry).where(DirectoryEntry.user_id == user.id)
                )
            ).scalar_one_or_none() is None
            event = (
                await session.execute(
                    select(IdentityEvent).where(
                        IdentityEvent.action == IdentityEventAction.USER_INVITE
                    )
                )
            ).scalar_one()
            assert event.target_user_id == user.id

    async def test_invite_grants_groups_and_the_default(
        self,
        app: Any,
        client: httpx.AsyncClient,
        seeded: Seeded,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        headers = await _admin_session(app, session_factory, seeded)
        # The seeded group is the one the invite names.
        response = await _invite(client, headers, "g@example.org", groups=["research"])
        assert response.status_code == 201, response.text
        async with session_factory() as session:
            user = (
                await session.execute(
                    select(User).where(User.email_normalized == "g@example.org")
                )
            ).scalar_one()
            names = sorted(m.group.name for m in user.memberships)
            assert "research" in names

    async def test_invite_refuses_a_login_and_an_untrusted_address(
        self,
        app: Any,
        client: httpx.AsyncClient,
        seeded: Seeded,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        headers = await _admin_session(app, session_factory, seeded)
        response = await _invite(client, headers, "a@example.org", login="lena")
        assert response.status_code == 400, response.text
        response = await _invite(client, headers, "not-an-address")
        assert response.status_code == 400, response.text

    async def test_invite_conflicts_naming_the_status(
        self,
        app: Any,
        client: httpx.AsyncClient,
        seeded: Seeded,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        headers = await _admin_session(app, session_factory, seeded)
        assert (await _invite(client, headers, "dup@example.org")).status_code == 201
        response = await _invite(client, headers, "dup@example.org")
        assert response.status_code == 409, response.text
        assert "dup@example.org" in response.json()["error"]["message"]
        assert "pending" in response.json()["error"]["message"]
        # ... and against a real account too (normalised the way every real
        # sign-in leaves it; the fixture row predates that).
        async with session_factory() as session:
            seeded_row = await session.get(User, seeded.user.id)
            assert seeded_row is not None
            seeded_row.email_normalized = (seeded_row.email or "").casefold()
            await session.commit()
        response = await _invite(client, headers, seeded.user.email)
        assert response.status_code == 409, response.text
        assert "active" in response.json()["error"]["message"]

    async def test_send_email_needs_smtp(
        self,
        app: Any,
        client: httpx.AsyncClient,
        seeded: Seeded,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        headers = await _admin_session(app, session_factory, seeded)
        response = await _invite(client, headers, "m@example.org", send_email=True)
        assert response.status_code == 400, response.text
        assert "Email" in response.json()["error"]["message"]

    async def test_invite_text_regenerates_and_reissues(
        self,
        app: Any,
        client: httpx.AsyncClient,
        seeded: Seeded,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        headers = await _admin_session(app, session_factory, seeded)
        created = await _invite(client, headers, "r@example.org")
        assert created.status_code == 201, created.text
        user_id = created.json()["user"]["id"]
        async with session_factory() as session:
            user = await session.get(User, UUID(user_id))
            assert user is not None
            user.invited_at = utcnow() - timedelta(days=13)
            await session.commit()
        response = await client.post(f"/api/admin/users/{user_id}/invite-text", headers=headers)
        assert response.status_code == 200, response.text
        assert "r@example.org" in response.json()["invite_text"]
        async with session_factory() as session:
            user = await session.get(User, UUID(user_id))
            assert user is not None
            assert user.invited_at is not None
            assert utcnow() - user.invited_at < timedelta(hours=1)

    async def test_invite_text_refuses_a_real_account(
        self,
        app: Any,
        client: httpx.AsyncClient,
        seeded: Seeded,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        headers = await _admin_session(app, session_factory, seeded)
        response = await client.post(
            f"/api/admin/users/{seeded.user.id}/invite-text", headers=headers
        )
        assert response.status_code == 404, response.text


class TestClaim:
    async def _pending(
        self, session: AsyncSession, email: str = "c@example.org", **overrides: Any
    ) -> User:
        fields: dict[str, Any] = {
            "issuer": PENDING_USER_ISSUER,
            "subject": f"pending-{email}",
            "email": email,
            "email_normalized": email,
            "invited_at": utcnow(),
        }
        fields.update(overrides)
        user = User(**fields)
        session.add(user)
        await session.commit()
        return user

    async def _signin(
        self, session: AsyncSession, subject: str, email: str, groups: list[str]
    ) -> Any:
        settings = _settings()
        record = record_from_env(settings)
        assert record is not None
        result = await sign_in(
            session,
            record,
            issuer=ISS,
            subject=subject,
            claims={
                "iss": ISS,
                "sub": subject,
                "email": email,
                "email_verified": True,
                "groups": groups,
            },
            settings=settings,
        )
        await session.commit()
        return result

    async def test_claim_at_sign_in_keeps_one_row(self, session: AsyncSession) -> None:
        pending = await self._pending(session)
        result = await self._signin(session, "new-sub", "c@example.org", [])
        assert result.user.id == pending.id
        assert result.user.issuer == ISS
        assert result.user.subject == "new-sub"
        # A second sign-in resolves the same row — and linking never fired.
        again = await self._signin(session, "new-sub", "c@example.org", [])
        assert again.user.id == pending.id
        count = await session.scalar(
            select(func.count()).select_from(User).where(User.email_normalized == "c@example.org")
        )
        assert count == 1

    async def test_claim_refusals(self, session: AsyncSession) -> None:
        settings = _settings()
        record = record_from_env(settings)
        assert record is not None

        async def attempt(email: Any, verified: Any) -> User | None:
            return await claim_pending_by_email(
                session,
                record,
                issuer=ISS,
                subject="s",
                email=email,
                email_verified=verified,
            )

        await self._pending(session, email="r1@example.org")
        # (1) email_verified must be the literal True.
        for bad in (False, None, "true", 1):
            assert await attempt("r1@example.org", bad) is None
        # (2) untrusted address.
        assert await attempt("not-an-address", True) is None
        # (3a) nobody invited; (3b) two invites for one address.
        assert await attempt("nobody@example.org", True) is None
        await self._pending(session, email="r2@example.org", subject="pending-a")
        await self._pending(session, email="r2@example.org", subject="pending-b")
        assert await attempt("r2@example.org", True) is None
        # (4) a disabled invite.
        await self._pending(session, email="r3@example.org", is_active=False)
        assert await attempt("r3@example.org", True) is None
        # (5) a bundled pending account is not an invite.
        await self._pending(session, email="r4@example.org", invited_at=None, username="r4login")
        assert await attempt("r4@example.org", True) is None
        # (6) expired.
        await self._pending(
            session,
            email="r5@example.org",
            invited_at=utcnow() - timedelta(days=INVITE_EXPIRY_DAYS + 1),
        )
        assert await attempt("r5@example.org", True) is None

    async def test_expired_invite_behaves_as_no_invite(self, session: AsyncSession) -> None:
        pending = await self._pending(
            session,
            email="e@example.org",
            invited_at=utcnow() - timedelta(days=INVITE_EXPIRY_DAYS + 1),
        )
        settings = _settings()
        record = record_from_env(settings)
        assert record is not None
        # Invite mode refuses the stranger the expired invite no longer vouches for —
        # and the pending row stays pending for the administrator to re-invite.
        with pytest.raises(ProvisioningRefused):
            await sign_in(
                session,
                record,
                issuer=ISS,
                subject="expired-sub",
                claims={
                    "iss": ISS,
                    "sub": "expired-sub",
                    "email": "e@example.org",
                    "email_verified": True,
                    "groups": [],
                },
                settings=settings,
                policy=OIDCPolicy(join_mode="invite"),
            )
        still = await session.get(User, pending.id)
        assert still is not None
        assert still.issuer == PENDING_USER_ISSUER

    async def test_claimed_user_keeps_console_groups(self, session: AsyncSession) -> None:
        pending = await self._pending(session, email="k@example.org")
        group = Group(name="research")
        session.add(group)
        await session.flush()
        session.add(
            Membership(user_id=pending.id, group_id=group.id, source=MembershipSource.MANUAL)
        )
        await session.commit()
        result = await self._signin(session, "k-sub", "k@example.org", [])
        names = sorted(m.group.name for m in result.user.memberships)
        assert "research" in names

    async def test_claim_then_link_by_email_do_not_both_fire(self, session: AsyncSession) -> None:
        pending = await self._pending(session, email="b@example.org")
        result = await self._signin(session, "b-sub", "b@example.org", [])
        assert result.user.id == pending.id
        rows = (
            (await session.execute(select(UserIdentity).where(UserIdentity.user_id == pending.id)))
            .scalars()
            .all()
        )
        assert rows == []


class TestDeletePending:
    async def test_delete_pending_skips_chat_erasure(
        self,
        app: Any,
        client: httpx.AsyncClient,
        seeded: Seeded,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        # No chat configured at all: a real account's delete would refuse.
        headers = await _admin_session(app, session_factory, seeded)
        created = await _invite(client, headers, "gone@example.org")
        assert created.status_code == 201, created.text
        user_id = created.json()["user"]["id"]
        response = await client.delete(f"/api/admin/users/{user_id}", headers=headers)
        assert response.status_code == 204, response.text
        async with session_factory() as session:
            assert await session.get(User, UUID(user_id)) is None
            event = (
                await session.execute(
                    select(IdentityEvent).where(
                        IdentityEvent.action == IdentityEventAction.USER_DELETE
                    )
                )
            ).scalar_one()
            assert event.detail == {"pending": True}


class TestClaimAtAnnounce:
    async def test_announce_claims_the_invite(
        self,
        bearer_app: Any,
        client: httpx.AsyncClient,
        seeded: Seeded,
        signing_key: Any,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        from conftest import BEARER_ISSUER, bearer_auth, make_token

        bearer_app.state.settings.oidc.chat_client_id = "llm-chat"
        headers = await _admin_session(bearer_app, session_factory, seeded)
        created = await _invite(client, headers, "invited@example.org")
        assert created.status_code == 201, created.text

        token = make_token(
            signing_key,
            sub="invited-sub",
            email="invited@example.org",
            email_verified=True,
            groups=[],
        )
        response = await client.post("/v1/session/announce", headers=bearer_auth(token))
        assert response.status_code == 200, response.text
        async with session_factory() as session:
            claimed = (
                await session.execute(
                    select(User).where(User.issuer == BEARER_ISSUER, User.subject == "invited-sub")
                )
            ).scalar_one_or_none()
            assert claimed is not None
            assert claimed.email_normalized == "invited@example.org"
            # Still one row for the address: no second account was minted.
            count = await session.scalar(
                select(func.count())
                .select_from(User)
                .where(User.email_normalized == "invited@example.org")
            )
            assert count == 1
