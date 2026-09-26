"""`bind_bundled_login` (ADR 0093 §8.2): the bundled Authelia's own binding.

Unit-level, against a raw session — no HTTP, no real Authelia — because the
question is entirely "given this directory entry and this login's claims,
does the right user row end up holding the right identity", which a session
and three ORM rows answer directly.
"""

from __future__ import annotations

import uuid

from gateway.models import (
    DirectoryEntry,
    IdentityEvent,
    IdentityEventAction,
    IdentityProvider,
    User,
    UserIdentity,
)
from gateway.oidc import PENDING_USER_ISSUER, bind_bundled_login, claim_unbound_bundled_login
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

ISS = "https://gw.example.org/authelia"
NEW_SUBJECT = "authelia-sub-1"


async def _provider(session: AsyncSession, *, kind: str = "authelia") -> IdentityProvider:
    row = IdentityProvider(
        name="authelia",
        issuer=ISS,
        client_id="pystino-console",
        client_secret_encrypted="s",
        scopes=["openid"],
        groups_claim="groups",
        fetch_userinfo=False,
        group_mappings=[],
        link_by_email=False,
        kind=kind,
        group_source="none",
        sync_adapter="none",
        admin_source="console",
        admin_values=[],
        sync_deprovision="disable",
        sync_create_users=True,
        sync_confirmed=False,
        sync_interval_minutes=60,
    )
    session.add(row)
    await session.commit()
    return row


async def _pending_user(session: AsyncSession, *, email: str = "carol@example.org") -> User:
    user = User(
        issuer=PENDING_USER_ISSUER,
        subject=str(uuid.uuid4()),
        email=email,
        email_normalized=email,
        display_name="Carol",
        username="carol",
        admin_edited_fields=["email"],
    )
    session.add(user)
    await session.commit()
    await session.refresh(user)
    return user


async def _bound_entry(
    session: AsyncSession, provider: IdentityProvider, user: User, *, login: str = "carol"
) -> DirectoryEntry:
    entry = DirectoryEntry(
        provider_id=provider.id,
        external_id=login,
        username=login,
        email=user.email,
        user_id=user.id,
    )
    session.add(entry)
    await session.commit()
    return entry


async def _unbound_entry(
    session: AsyncSession,
    provider: IdentityProvider,
    *,
    login: str = "frank",
    email: str = "frank@example.org",
) -> DirectoryEntry:
    """As `migrate_bundled_directory` (§13.4) leaves a users-file login that
    pre-dates stage (b) and has no gateway user yet to bind it to."""
    entry = DirectoryEntry(
        provider_id=provider.id,
        external_id=login,
        username=login,
        email=email,
        user_id=None,
    )
    session.add(entry)
    await session.commit()
    return entry


class TestBindsAPendingUser:
    async def test_replaces_the_placeholder_pair(
        self, session_factory: async_sessionmaker[AsyncSession]
    ) -> None:
        async with session_factory() as session:
            provider = await _provider(session)
            user = await _pending_user(session)
            await _bound_entry(session, provider, user)

            await bind_bundled_login(
                session,
                provider,
                issuer=ISS,
                subject=NEW_SUBJECT,
                preferred_username="carol",
                email="carol@example.org",
                email_verified=True,
            )
            await session.commit()

            refreshed = (await session.execute(select(User).where(User.id == user.id))).scalar_one()
            assert refreshed.issuer == ISS
            assert refreshed.subject == NEW_SUBJECT
            # No secondary row: the placeholder is replaced, not kept beside it.
            assert (
                await session.execute(select(UserIdentity).where(UserIdentity.user_id == user.id))
            ).scalar_one_or_none() is None

            event = (
                await session.execute(
                    select(IdentityEvent).where(
                        IdentityEvent.action == IdentityEventAction.IDENTITY_BIND
                    )
                )
            ).scalar_one()
            assert event.target_user_id == user.id
            assert event.issuer == ISS

    async def test_a_pending_user_stays_unable_to_hold_admin_after_binding(
        self, session_factory: async_sessionmaker[AsyncSession]
    ) -> None:
        """Not this function's own refusal (that's the PATCH route's, §5.4) —
        pinned here so a future change to the bind order cannot silently let
        a first login through before the pending check would apply."""
        async with session_factory() as session:
            provider = await _provider(session)
            user = await _pending_user(session)
            await _bound_entry(session, provider, user)
            assert user.issuer == PENDING_USER_ISSUER
            assert not user.is_admin


class TestBindsAnExistingUser:
    async def test_adds_the_identity_rather_than_replacing_anything(
        self, session_factory: async_sessionmaker[AsyncSession]
    ) -> None:
        """break-glass, or Create sign-in: a real account with a history,
        just no bundled login until now — its own (issuer, subject) is
        untouched, and the new one arrives beside it."""
        async with session_factory() as session:
            provider = await _provider(session)
            user = User(
                issuer="https://old-idp.example.org",
                subject="old-sub",
                email="dave@example.org",
                email_normalized="dave@example.org",
                display_name="Dave",
            )
            session.add(user)
            await session.commit()
            await _bound_entry(session, provider, user, login="dave")

            await bind_bundled_login(
                session,
                provider,
                issuer=ISS,
                subject=NEW_SUBJECT,
                preferred_username="dave",
                email="dave@example.org",
                email_verified=True,
            )
            await session.commit()

            refreshed = (await session.execute(select(User).where(User.id == user.id))).scalar_one()
            assert refreshed.issuer == "https://old-idp.example.org"
            assert refreshed.subject == "old-sub"
            identity = (
                await session.execute(select(UserIdentity).where(UserIdentity.user_id == user.id))
            ).scalar_one()
            assert identity.issuer == ISS
            assert identity.subject == NEW_SUBJECT

    async def test_a_stale_subject_at_the_same_issuer_is_updated_not_duplicated(
        self, session_factory: async_sessionmaker[AsyncSession]
    ) -> None:
        """A lost authelia-data volume mints a new opaque subject for the same
        login — updating in place is what keeps this from tripping
        `uq_user_identities_user_issuer`."""
        async with session_factory() as session:
            provider = await _provider(session)
            user = User(
                issuer="https://old-idp.example.org",
                subject="old-sub",
                email="erin@example.org",
                email_normalized="erin@example.org",
            )
            session.add(user)
            await session.commit()
            session.add(
                UserIdentity(user_id=user.id, issuer=ISS, subject="stale-subject")
            )
            await session.commit()
            await _bound_entry(session, provider, user, login="erin")

            await bind_bundled_login(
                session,
                provider,
                issuer=ISS,
                subject="fresh-subject",
                preferred_username="erin",
                email="erin@example.org",
                email_verified=True,
            )
            await session.commit()

            identities = (
                (await session.execute(select(UserIdentity).where(UserIdentity.user_id == user.id)))
                .scalars()
                .all()
            )
            assert [i.subject for i in identities] == ["fresh-subject"]


class TestRefusals:
    async def test_unverified_email_is_refused(
        self, session_factory: async_sessionmaker[AsyncSession]
    ) -> None:
        async with session_factory() as session:
            provider = await _provider(session)
            user = await _pending_user(session)
            await _bound_entry(session, provider, user)

            await bind_bundled_login(
                session,
                provider,
                issuer=ISS,
                subject=NEW_SUBJECT,
                preferred_username="carol",
                email="carol@example.org",
                email_verified=False,
            )
            await session.commit()

            refreshed = (await session.execute(select(User).where(User.id == user.id))).scalar_one()
            assert refreshed.issuer == PENDING_USER_ISSUER

    async def test_email_mismatch_is_refused(
        self, session_factory: async_sessionmaker[AsyncSession]
    ) -> None:
        async with session_factory() as session:
            provider = await _provider(session)
            user = await _pending_user(session)
            await _bound_entry(session, provider, user)

            await bind_bundled_login(
                session,
                provider,
                issuer=ISS,
                subject=NEW_SUBJECT,
                preferred_username="carol",
                email="someone-else@example.org",
                email_verified=True,
            )
            await session.commit()

            refreshed = (await session.execute(select(User).where(User.id == user.id))).scalar_one()
            assert refreshed.issuer == PENDING_USER_ISSUER

    async def test_a_non_bundled_provider_is_a_no_op(
        self, session_factory: async_sessionmaker[AsyncSession]
    ) -> None:
        async with session_factory() as session:
            provider = await _provider(session, kind="keycloak")
            user = await _pending_user(session)
            await _bound_entry(session, provider, user)

            await bind_bundled_login(
                session,
                provider,
                issuer=ISS,
                subject=NEW_SUBJECT,
                preferred_username="carol",
                email="carol@example.org",
                email_verified=True,
            )
            await session.commit()

            refreshed = (await session.execute(select(User).where(User.id == user.id))).scalar_one()
            assert refreshed.issuer == PENDING_USER_ISSUER

    async def test_no_matching_bound_entry_is_a_no_op(
        self, session_factory: async_sessionmaker[AsyncSession]
    ) -> None:
        async with session_factory() as session:
            provider = await _provider(session)

            # No error, no write — an unmatched login is an ordinary new user.
            await bind_bundled_login(
                session,
                provider,
                issuer=ISS,
                subject=NEW_SUBJECT,
                preferred_username="nobody",
                email="nobody@example.org",
                email_verified=True,
            )
            await session.commit()

            assert (await session.execute(select(User))).scalars().all() == []


class TestClaimsUnboundEntry:
    """`claim_unbound_bundled_login` (§13.4/§8.2 seam): the migration leaves a
    pre-existing file login's entry unbound, since it has no gateway user yet
    — this is the first sign-in that gives it one to claim the entry with.
    """

    async def test_a_migration_created_unbound_entry_is_claimed_at_first_sign_in(
        self, session_factory: async_sessionmaker[AsyncSession]
    ) -> None:
        async with session_factory() as session:
            provider = await _provider(session)
            entry = await _unbound_entry(session, provider)
            # Stands in for `provision_user`'s ordinary new-user path, which
            # runs before this function is ever called.
            user = User(
                issuer=ISS,
                subject=NEW_SUBJECT,
                email="frank@example.org",
                email_normalized="frank@example.org",
                display_name="Frank",
                username="frank",
            )
            session.add(user)
            await session.commit()
            await session.refresh(user)

            await claim_unbound_bundled_login(
                session,
                provider,
                user,
                preferred_username="frank",
                email="frank@example.org",
                email_verified=True,
            )
            await session.commit()

            refreshed = (
                await session.execute(select(DirectoryEntry).where(DirectoryEntry.id == entry.id))
            ).scalar_one()
            assert refreshed.user_id == user.id

            event = (
                await session.execute(
                    select(IdentityEvent).where(
                        IdentityEvent.action == IdentityEventAction.IDENTITY_BIND
                    )
                )
            ).scalar_one()
            assert event.target_user_id == user.id
            assert event.detail == {"claimed_unbound": True}

    async def test_a_mismatched_email_is_not_claimed(
        self, session_factory: async_sessionmaker[AsyncSession]
    ) -> None:
        async with session_factory() as session:
            provider = await _provider(session)
            entry = await _unbound_entry(session, provider)
            user = User(
                issuer=ISS,
                subject=NEW_SUBJECT,
                email="someone-else@example.org",
                email_normalized="someone-else@example.org",
                username="frank",
            )
            session.add(user)
            await session.commit()
            await session.refresh(user)

            await claim_unbound_bundled_login(
                session,
                provider,
                user,
                preferred_username="frank",
                email="someone-else@example.org",
                email_verified=True,
            )
            await session.commit()

            refreshed = (
                await session.execute(select(DirectoryEntry).where(DirectoryEntry.id == entry.id))
            ).scalar_one()
            assert refreshed.user_id is None

    async def test_an_already_bound_entry_is_never_reclaimed_by_a_different_user(
        self, session_factory: async_sessionmaker[AsyncSession]
    ) -> None:
        async with session_factory() as session:
            provider = await _provider(session)
            owner = User(
                issuer=ISS,
                subject="owner-sub",
                email="frank@example.org",
                email_normalized="frank@example.org",
                username="frank",
            )
            session.add(owner)
            await session.commit()
            await session.refresh(owner)
            entry = await _bound_entry(session, provider, owner, login="frank")

            impostor = User(
                issuer=ISS,
                subject="impostor-sub",
                email="frank@example.org",
                email_normalized="frank@example.org",
                username="frank-2",
            )
            session.add(impostor)
            await session.commit()
            await session.refresh(impostor)

            await claim_unbound_bundled_login(
                session,
                provider,
                impostor,
                preferred_username="frank",
                email="frank@example.org",
                email_verified=True,
            )
            await session.commit()

            refreshed = (
                await session.execute(select(DirectoryEntry).where(DirectoryEntry.id == entry.id))
            ).scalar_one()
            assert refreshed.user_id == owner.id
