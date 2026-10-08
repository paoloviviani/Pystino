"""`ensure_bundled_default_group` (ADR 0093 to-do item 1).

`group_source=none` made groups console-authoritative, so a bundled sign-in's
own token stopped seeding the `users` group Authelia's claim used to grant
automatically. Unit-level for the helper itself — reuse-or-create, and
"already has something, leave it alone" — plus one test of its wiring into
`sign_in`, the call site every bundled sign-in (first or not) runs through,
and the one place "an external IdP gets nothing" is a real question.
"""

from __future__ import annotations

from gateway.config import OIDCSettings, Settings
from gateway.directory.engine import ensure_bundled_default_group
from gateway.models import Group, GroupSource, Membership, MembershipSource, User
from gateway.oidc import sign_in
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

ISS = "https://gw.example.org/authelia"


async def _bare_user(session: AsyncSession, *, issuer: str = ISS, subject: str = "s1") -> User:
    user = User(issuer=issuer, subject=subject, email=f"{subject}@example.org")
    session.add(user)
    await session.flush()
    await session.refresh(user, attribute_names=["memberships"])
    return user


class TestEnsureBundledDefaultGroup:
    async def test_creates_the_group_when_missing(self, session: AsyncSession) -> None:
        user = await _bare_user(session)
        added = await ensure_bundled_default_group(session, user)
        assert added is True

        memberships = (
            (await session.execute(select(Membership).where(Membership.user_id == user.id)))
            .scalars()
            .all()
        )
        assert len(memberships) == 1
        assert memberships[0].source == MembershipSource.MANUAL
        group = (
            await session.execute(select(Group).where(Group.id == memberships[0].group_id))
        ).scalar_one()
        assert group.name == "users"
        assert group.source == GroupSource.MANUAL
        assert user.default_billing_group_id == group.id

    async def test_reuses_an_existing_users_group_rather_than_duplicating_it(
        self, session: AsyncSession
    ) -> None:
        existing = Group(name="users", source=GroupSource.MANUAL)
        session.add(existing)
        await session.flush()

        user = await _bare_user(session, subject="s2")
        await ensure_bundled_default_group(session, user)

        rows = (await session.execute(select(Group).where(Group.name == "users"))).scalars().all()
        assert len(rows) == 1
        assert rows[0].id == existing.id

    async def test_a_user_with_any_membership_is_left_alone(self, session: AsyncSession) -> None:
        other = Group(name="finance", source=GroupSource.MANUAL)
        session.add(other)
        await session.flush()

        user = await _bare_user(session, subject="s3")
        session.add(Membership(user_id=user.id, group_id=other.id, source=MembershipSource.MANUAL))
        await session.flush()
        await session.refresh(user, attribute_names=["memberships"])

        added = await ensure_bundled_default_group(session, user)
        assert added is False

        memberships = (
            (await session.execute(select(Membership).where(Membership.user_id == user.id)))
            .scalars()
            .all()
        )
        assert {m.group_id for m in memberships} == {other.id}
        assert (
            await session.execute(select(Group).where(Group.name == "users"))
        ).scalar_one_or_none() is None


class TestSignInWiresItIn:
    async def _authelia_provider(self, session: AsyncSession) -> object:
        from gateway.models import IdentityProvider
        from gateway.secrets import SecretBox

        row = IdentityProvider(
            name="authelia",
            issuer=ISS,
            client_id="pystino-console",
            client_secret_encrypted=SecretBox(["k"]).encrypt("s"),
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
        )
        session.add(row)
        await session.flush()
        return row

    async def test_a_new_bundled_sign_in_gets_the_default_group(
        self, session: AsyncSession
    ) -> None:
        record = await self._authelia_provider(session)
        settings = Settings(oidc=OIDCSettings(kind="authelia", groups_claim="groups"))

        result = await sign_in(
            session,
            record,
            issuer=ISS,
            subject="brand-new",
            claims={"iss": ISS, "sub": "brand-new", "email": "new@example.org"},
            settings=settings,
        )
        await session.refresh(result.user, attribute_names=["memberships"])
        assert len(result.user.memberships) == 1
        assert result.user.memberships[0].group.name == "users"
        assert result.user.default_billing_group_id == result.user.memberships[0].group_id

    async def test_an_existing_groupless_bundled_user_gets_it_at_their_next_sign_in(
        self, session: AsyncSession
    ) -> None:
        """The §13.4 migration converted existing memberships, but left a
        user with none the way it found them; this is the "next sign-in"
        half of the to-do, not the migration itself."""
        record = await self._authelia_provider(session)
        user = await _bare_user(session, subject="already-here")
        await session.commit()

        settings = Settings(oidc=OIDCSettings(kind="authelia", groups_claim="groups"))
        result = await sign_in(
            session,
            record,
            issuer=ISS,
            subject="already-here",
            claims={"iss": ISS, "sub": "already-here", "email": "already-here@example.org"},
            settings=settings,
        )
        assert result.user.id == user.id
        await session.refresh(result.user, attribute_names=["memberships"])
        assert {m.group.name for m in result.user.memberships} == {"users"}

    async def test_an_external_idp_sign_in_gets_nothing_with_the_default_group_off(
        self, session: AsyncSession
    ) -> None:
        """This rule is the bundled provider's alone; an external IdP's sign-in
        gets the deployment's default group instead (test_group_import.py),
        and with that turned off, nothing."""
        from gateway.identity_registry import record_from_env

        settings = Settings(
            oidc=OIDCSettings(
                enabled=True,
                issuer="https://idp.example.org",
                client_id="c",
                groups_claim="groups",
                default_group="",
            )
        )
        record = record_from_env(settings)
        assert record is not None

        result = await sign_in(
            session,
            record,
            issuer="https://idp.example.org",
            subject="ext-1",
            claims={"iss": "https://idp.example.org", "sub": "ext-1", "email": "ext@example.org"},
            settings=settings,
        )
        await session.refresh(result.user, attribute_names=["memberships"])
        assert result.user.memberships == []
