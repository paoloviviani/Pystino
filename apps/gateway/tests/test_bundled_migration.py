"""The first-start migration for bundled users (ADR 0093 §13.4).

Against a raw session and a real (tmp_path) users file — no HTTP — since
the question is entirely "given this pre-stage-b state, does the migration
converge it", which three ORM rows and a YAML file answer directly.
"""

from __future__ import annotations

import json

import yaml
from gateway.directory.bundled_migration import migrate_bundled_directory
from gateway.models import (
    DirectoryEntry,
    Group,
    IdentityEvent,
    IdentityEventAction,
    IdentityProvider,
    Membership,
    MembershipSource,
    User,
)
from gateway.secrets import SecretBox
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

BOX = SecretBox(["test-encryption-key-not-for-production"])
ISS = "https://gw.example.org/authelia"

SEED = """users:
  alice:
    disabled: false
    displayname: Alice
    password: '$argon2id$v=19$m=65536,t=3,p=4$c2FsdA$aGFzaA'
    email: alice@example.org
    groups: [users, admins]
  bob:
    disabled: false
    displayname: Bob
    password: '$argon2id$v=19$m=65536,t=3,p=4$c2FsdA$aGFzaA'
    email: bob@example.org
    groups: [users]
  orphan:
    disabled: false
    displayname: Orphan
    password: '$argon2id$v=19$m=65536,t=3,p=4$c2FsdA$aGFzaA'
    email: orphan@example.org
    groups: [users, legacy]
"""


async def _provider(session_factory: async_sessionmaker[AsyncSession], path) -> IdentityProvider:
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
            sync_adapter="authelia_file",
            admin_source="console",
            admin_values=[],
            sync_deprovision="disable",
            sync_create_users=True,
            sync_confirmed=False,
            sync_interval_minutes=60,
            sync_config_encrypted=BOX.encrypt(json.dumps({"path": str(path)})),
            is_enabled=True,
        )
        session.add(row)
        await session.commit()
        await session.refresh(row)
        return row


async def test_memberships_oidc_to_manual_only_for_the_bundled_issuers_users(
    session_factory: async_sessionmaker[AsyncSession], tmp_path
) -> None:
    path = tmp_path / "users_database.yml"
    path.write_text(SEED)
    provider = await _provider(session_factory, path)

    async with session_factory() as session:
        group = Group(name="research")
        session.add(group)
        await session.flush()

        bundled_user = User(issuer=ISS, subject="alice-sub", username="alice", email="a@x.org")
        other_user = User(issuer="https://elsewhere.example.org", subject="s", email="o@x.org")
        session.add_all([bundled_user, other_user])
        await session.flush()
        session.add(
            Membership(user_id=bundled_user.id, group_id=group.id, source=MembershipSource.OIDC)
        )
        session.add(
            Membership(user_id=other_user.id, group_id=group.id, source=MembershipSource.OIDC)
        )
        await session.commit()
        bundled_user_id, other_user_id = bundled_user.id, other_user.id

    async with session_factory() as session:
        await migrate_bundled_directory(session, provider, BOX)
        await session.commit()

    async with session_factory() as session:
        bundled_membership = (
            await session.execute(select(Membership).where(Membership.user_id == bundled_user_id))
        ).scalar_one()
        assert bundled_membership.source == MembershipSource.MANUAL

        other_membership = (
            await session.execute(select(Membership).where(Membership.user_id == other_user_id))
        ).scalar_one()
        assert other_membership.source == MembershipSource.OIDC


async def test_normalises_groups_and_audits_the_removed_names_once(
    session_factory: async_sessionmaker[AsyncSession], tmp_path
) -> None:
    path = tmp_path / "users_database.yml"
    path.write_text(SEED)
    provider = await _provider(session_factory, path)

    async with session_factory() as session:
        await migrate_bundled_directory(session, provider, BOX)
        await session.commit()

    data = yaml.safe_load(path.read_text())["users"]
    assert data["alice"]["groups"] == ["users"]
    assert data["bob"]["groups"] == ["users"]
    assert data["orphan"]["groups"] == ["users"]

    async with session_factory() as session:
        events = (
            (
                await session.execute(
                    select(IdentityEvent).where(
                        IdentityEvent.action == IdentityEventAction.IDP_RESEED
                    )
                )
            )
            .scalars()
            .all()
        )
        assert len(events) == 1
        assert sorted(events[0].detail["removed_groups"]) == ["admins", "legacy"]

    # Idempotent: a second run changes nothing further and writes no
    # second audit row, since every entry already reads ["users"].
    mtime_before = path.stat().st_mtime_ns
    async with session_factory() as session:
        await migrate_bundled_directory(session, provider, BOX)
        await session.commit()
    assert path.stat().st_mtime_ns == mtime_before

    async with session_factory() as session:
        events = (
            (
                await session.execute(
                    select(IdentityEvent).where(
                        IdentityEvent.action == IdentityEventAction.IDP_RESEED
                    )
                )
            )
            .scalars()
            .all()
        )
        assert len(events) == 1


async def test_directory_entries_bound_where_a_matching_user_exists(
    session_factory: async_sessionmaker[AsyncSession], tmp_path
) -> None:
    path = tmp_path / "users_database.yml"
    path.write_text(SEED)
    provider = await _provider(session_factory, path)

    async with session_factory() as session:
        alice = User(issuer=ISS, subject="alice-sub", username="alice", email="alice@example.org")
        bob = User(issuer=ISS, subject="bob-sub", username="bob", email="bob@example.org")
        session.add_all([alice, bob])
        # A pre-existing, already-bound entry for bob (from an earlier
        # directory-sync pass) — the migration must leave it exactly as is,
        # never re-derive or duplicate it.
        await session.flush()
        session.add(
            DirectoryEntry(
                provider_id=provider.id,
                external_id="bob",
                username="bob",
                user_id=bob.id,
                email="bob-pre-existing@example.org",
            )
        )
        await session.commit()
        alice_id, bob_id = alice.id, bob.id

    async with session_factory() as session:
        await migrate_bundled_directory(session, provider, BOX)
        await session.commit()

    async with session_factory() as session:
        entries = {
            row.external_id: row
            for row in (
                await session.execute(
                    select(DirectoryEntry).where(DirectoryEntry.provider_id == provider.id)
                )
            )
            .scalars()
            .all()
        }
        assert set(entries) == {"alice", "bob", "orphan"}

        # alice: newly created, bound by (issuer, username) match.
        assert entries["alice"].user_id == alice_id

        # bob: the pre-existing entry is untouched (not recreated).
        assert entries["bob"].user_id == bob_id
        assert entries["bob"].email == "bob-pre-existing@example.org"

        # orphan: no matching user anywhere — created, left unbound.
        assert entries["orphan"].user_id is None

    # Idempotent: a second run creates no duplicates.
    async with session_factory() as session:
        await migrate_bundled_directory(session, provider, BOX)
        await session.commit()
    async with session_factory() as session:
        count = len(
            (
                await session.execute(
                    select(DirectoryEntry).where(DirectoryEntry.provider_id == provider.id)
                )
            )
            .scalars()
            .all()
        )
        assert count == 3


async def test_a_non_bundled_provider_is_never_passed_in_by_the_caller(
    session_factory: async_sessionmaker[AsyncSession], tmp_path
) -> None:
    """`main.py` only calls this for `active_bundled_provider`'s answer, which
    is already gated on `kind == "authelia"` — this test documents that
    contract rather than re-testing `active_bundled_provider` itself."""
    from gateway.identity_registry import active_bundled_provider

    async with session_factory() as session:
        session.add(
            IdentityProvider(
                name="default",
                issuer="https://keycloak.example.org",
                client_id="c",
                client_secret_encrypted=BOX.encrypt("s"),
                scopes=["openid"],
                groups_claim="groups",
                kind="keycloak",
                group_source="directory",
                sync_adapter="none",
                admin_source="console",
                admin_values=[],
                is_enabled=True,
            )
        )
        await session.commit()
        assert await active_bundled_provider(session) is None
