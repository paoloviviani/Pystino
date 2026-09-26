"""Batch directory sync (ADR 0088 draft): adapters, the engine, the runs, the schedule."""

from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest
from conftest import Seeded
from gateway.config import OIDCSettings
from gateway.directory.adapters import (
    AdapterError,
    AutheliaFileAdapter,
    Entry,
    KeycloakAdminAdapter,
)
from gateway.directory.engine import apply_entries, link_at_login
from gateway.identity_registry import record_from_row
from gateway.models import (
    DirectoryEntry,
    GroupSync,
    IdentityProvider,
    MembershipSource,
    User,
)
from gateway.secrets import SecretBox
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from test_admin import as_user, make_admin

BOX = SecretBox(["test-encryption-key-not-for-production"])
ISS = "https://llm.example.org/authelia"
USERS = """users:
  alice:
    displayname: Alice
    password: '$argon2id$v=19$m=65536,t=3,p=4$c2FsdA$aGFzaA'
    email: alice@example.org
    groups: [users, ops]
  bob:
    disabled: true
    email: bob@example.org
    groups: [users]
"""


async def _provider(session: AsyncSession, **fields: object) -> IdentityProvider:
    row = IdentityProvider(
        name=str(fields.pop("name", "authelia")),
        issuer=str(fields.pop("issuer", ISS)),
        client_id="pystino-console",
        client_secret_encrypted=BOX.encrypt("s"),
        scopes=["openid"],
        groups_claim="groups",
        fetch_userinfo=False,
        group_mappings=[],
        link_by_email=False,
        group_sync=fields.pop("group_sync", GroupSync.EVERY_LOGIN),
        kind=str(fields.pop("kind", "authelia")),
        group_source=str(fields.pop("group_source", "directory")),
        sync_adapter=str(fields.pop("sync_adapter", "authelia_file")),
        admin_source=str(fields.pop("admin_source", "console")),
        admin_values=list(fields.pop("admin_values", [])),
        sync_deprovision="disable",
        sync_create_users=True,
        sync_confirmed=bool(fields.pop("sync_confirmed", False)),
        sync_interval_minutes=int(fields.pop("sync_interval_minutes", 60)),
        **fields,
    )
    session.add(row)
    await session.commit()
    return row


def _user(subject: str, username: str, **kw: object) -> User:
    return User(
        issuer=ISS, subject=subject, username=username, email=f"{username}@example.org", **kw
    )


async def _groups(session: AsyncSession, user: User) -> set[str]:
    await session.refresh(user, attribute_names=["memberships"])
    return {m.group.name for m in user.memberships}


class TestAdapters:
    async def test_authelia_file(self, tmp_path: Path) -> None:
        path = tmp_path / "users_database.yml"
        path.write_text(USERS)
        entries = {e.username: e for e in await AutheliaFileAdapter(path).list_entries()}
        assert entries["alice"].groups == ("users", "ops") and entries["alice"].active
        assert not entries["bob"].active
        assert not entries["alice"].is_subject  # Authelia's sub is opaque

    async def test_a_broken_file_is_an_error_not_an_empty_directory(self, tmp_path: Path) -> None:
        path = tmp_path / "users_database.yml"
        path.write_text("")
        with pytest.raises(AdapterError):
            await AutheliaFileAdapter(path).list_entries()

    async def test_keycloak_admin_rest(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path.endswith("/protocol/openid-connect/token"):
                return httpx.Response(200, json={"access_token": "t"})
            if request.url.path.endswith("/admin/realms/corp/users"):
                assert request.headers["authorization"] == "Bearer t"
                return httpx.Response(
                    200,
                    json=[{"id": "u-1", "username": "alice", "email": "a@x.org", "enabled": True}],
                )
            if request.url.path.endswith("/users/u-1/groups"):
                return httpx.Response(200, json=[{"name": "ops"}])
            return httpx.Response(404)

        adapter = KeycloakAdminAdapter(
            "https://kc.example.org/realms/corp",
            "sync",
            "secret",
            httpx.AsyncClient(transport=httpx.MockTransport(handler)),
        )
        (entry,) = await adapter.list_entries()
        assert (entry.external_id, entry.groups, entry.is_subject) == ("u-1", ("ops",), True)


class TestEngine:
    async def test_authelia_entries_link_by_username_and_bring_groups(
        self, session: AsyncSession
    ) -> None:
        row = await _provider(session)
        alice = _user("opaque-1", "alice")
        session.add(alice)
        await session.commit()
        entries = [Entry("alice", "alice", "alice@example.org", "Alice", ("users", "ops"))]
        report = await apply_entries(
            session,
            record_from_row(row, BOX),
            entries,
            settings=OIDCSettings(),
            dry_run=False,
            full=True,
        )
        assert report.linked == 1 and report.updated == 1
        assert await _groups(session, alice) == {"users", "ops"}
        mirror = (await session.execute(select(DirectoryEntry))).scalar_one()
        assert mirror.user_id == alice.id

    async def test_a_dry_run_reports_and_changes_nothing(self, session: AsyncSession) -> None:
        row = await _provider(session)
        alice = _user("opaque-1", "alice")
        session.add(alice)
        await session.commit()
        alice_id = alice.id  # the dry run's rollback expires every loaded object
        report = await apply_entries(
            session,
            record_from_row(row, BOX),
            [Entry("alice", "alice", None, None, ("ops",))],
            settings=OIDCSettings(),
            dry_run=True,
            full=True,
        )
        assert report.dry_run and report.linked == 1
        assert any(c["change"] == "groups" for c in report.changes)
        assert (await session.execute(select(DirectoryEntry))).first() is None
        alice = await session.get(User, alice_id)
        assert await _groups(session, alice) == set()

    async def test_gone_and_disabled_people_are_deactivated_and_can_come_back(
        self, session: AsyncSession
    ) -> None:
        row = await _provider(session)
        alice, bob = _user("s-a", "alice"), _user("s-b", "bob")
        session.add_all([alice, bob])
        await session.commit()
        record = record_from_row(row, BOX)
        both = [Entry("alice", "alice"), Entry("bob", "bob")]
        await apply_entries(
            session, record, both, settings=OIDCSettings(), dry_run=False, full=True
        )
        report = await apply_entries(
            session,
            record,
            [Entry("alice", "alice", active=False)],
            settings=OIDCSettings(),
            dry_run=False,
            full=True,
        )
        assert report.deactivated == 2
        for user in (alice, bob):
            fresh = await session.get(User, user.id)
            await session.refresh(fresh)
            assert not fresh.is_active and fresh.deactivated_by == "directory"
        back = await apply_entries(
            session, record, both, settings=OIDCSettings(), dry_run=False, full=True
        )
        # Both come back: the directory deactivated them, so it may reactivate them.
        assert back.reactivated == 2

    async def test_a_console_deactivation_is_not_undone(self, session: AsyncSession) -> None:
        row = await _provider(session)
        alice = _user("s-a", "alice", is_active=False, deactivated_by="manual")
        session.add(alice)
        await session.commit()
        await apply_entries(
            session,
            record_from_row(row, BOX),
            [Entry("alice", "alice")],
            settings=OIDCSettings(),
            dry_run=False,
            full=True,
        )
        await session.refresh(alice)
        assert not alice.is_active

    async def test_the_valve_stops_a_mass_deactivation(self, session: AsyncSession) -> None:
        row = await _provider(session)
        people = [_user(f"s{i}", f"u{i}") for i in range(8)]
        session.add_all(people)
        await session.commit()
        record = record_from_row(row, BOX)
        everyone = [Entry(f"u{i}", f"u{i}") for i in range(8)]
        await apply_entries(
            session, record, everyone, settings=OIDCSettings(), dry_run=False, full=True
        )
        report = await apply_entries(
            session, record, [], settings=OIDCSettings(), dry_run=False, full=True
        )
        assert report.status == "needs_confirmation" and report.deactivated == 8
        active = (
            (await session.execute(select(User).where(User.is_active.is_(True)))).scalars().all()
        )
        assert len(active) == 8, "nothing applied"
        forced = await apply_entries(
            session, record, [], settings=OIDCSettings(), dry_run=False, full=True, force=True
        )
        assert forced.status == "ok"

    async def test_subject_keyed_directories_create_accounts_before_login(
        self, session: AsyncSession
    ) -> None:
        row = await _provider(
            session,
            kind="keycloak",
            sync_adapter="keycloak_admin",
            issuer="https://kc.example.org/realms/corp",
        )
        report = await apply_entries(
            session,
            record_from_row(row, BOX),
            [Entry("u-1", "alice", "a@x.org", "Alice", ("ops",), is_subject=True)],
            settings=OIDCSettings(),
            dry_run=False,
            full=True,
        )
        assert report.created == 1
        user = (await session.execute(select(User).where(User.subject == "u-1"))).scalar_one()
        assert user.issuer == "https://kc.example.org/realms/corp"
        assert await _groups(session, user) == {"ops"}

    async def test_admin_from_directory_groups(self, session: AsyncSession) -> None:
        row = await _provider(session, admin_source="claim", admin_values=["ops"])
        keeper = _user("k", "keeper", is_admin=True)
        alice = _user("s-a", "alice")
        session.add_all([keeper, alice])
        await session.commit()
        record = record_from_row(row, BOX)
        await apply_entries(
            session,
            record,
            [Entry("alice", "alice", groups=("ops",))],
            settings=OIDCSettings(),
            dry_run=False,
            full=False,
        )
        await session.refresh(alice)
        assert alice.is_admin and alice.admin_source == "oidc"
        await apply_entries(
            session,
            record,
            [Entry("alice", "alice", groups=())],
            settings=OIDCSettings(),
            dry_run=False,
            full=False,
        )
        await session.refresh(alice)
        assert not alice.is_admin

    async def test_preassigned_groups_become_manual_memberships_at_login(
        self, session: AsyncSession
    ) -> None:
        row = await _provider(session, group_source="claim")
        record = record_from_row(row, BOX)
        await apply_entries(
            session,
            record,
            [Entry("carol", "carol", "carol@example.org")],
            settings=OIDCSettings(),
            dry_run=False,
            full=True,
        )
        entry = (await session.execute(select(DirectoryEntry))).scalar_one()
        entry.preassigned_groups = ["research"]
        carol = _user("opaque-c", "carol")
        session.add(carol)
        await session.commit()
        # An unverified email does not link.
        assert not await link_at_login(
            session,
            record,
            carol,
            {"preferred_username": "carol", "email": "carol@example.org", "email_verified": "true"},
            settings=OIDCSettings(),
        )
        assert await link_at_login(
            session,
            record,
            carol,
            {"preferred_username": "carol", "email": "carol@example.org", "email_verified": True},
            settings=OIDCSettings(),
        )
        await session.commit()
        await session.refresh(carol, attribute_names=["memberships"])
        (membership,) = carol.memberships
        assert membership.group.name == "research" and membership.source == MembershipSource.MANUAL


class TestRunsAndApi:
    async def test_first_run_is_forced_dry_then_confirm_then_apply(
        self,
        app: object,
        client: httpx.AsyncClient,
        seeded: Seeded,
        session_factory: async_sessionmaker[AsyncSession],
        tmp_path: Path,
    ) -> None:
        as_user(app, await make_admin(session_factory, seeded))
        users = tmp_path / "users_database.yml"
        users.write_text(USERS)
        async with session_factory() as session:
            row = await _provider(session)
            session.add(_user("opaque-a", "alice"))
            await session.commit()
            pid = row.id
        await client.put(
            f"/api/admin/identity-providers/{pid}/sync-config",
            json={"config": {"path": str(users)}},
        )
        test = await client.post(f"/api/admin/identity-providers/{pid}/sync/test")
        assert test.json()["total"] == 2
        early = await client.post(f"/api/admin/identity-providers/{pid}/sync/confirm")
        assert early.status_code == 400
        first = await client.post(
            f"/api/admin/identity-providers/{pid}/sync", json={"dry_run": False}
        )
        assert first.json()["run"]["dry_run"] is True, "unconfirmed: forced dry"
        assert (
            await client.post(f"/api/admin/identity-providers/{pid}/sync/confirm")
        ).status_code == 200
        real = await client.post(
            f"/api/admin/identity-providers/{pid}/sync", json={"dry_run": False}
        )
        assert real.json()["run"]["dry_run"] is False and real.json()["run"]["linked"] == 1
        runs = (await client.get(f"/api/admin/identity-providers/{pid}/sync/runs")).json()
        assert len(runs) == 2
        unlinked = (
            await client.get(f"/api/admin/identity-providers/{pid}/directory?unlinked=true")
        ).json()
        assert [e["username"] for e in unlinked] == ["bob"]
        pre = await client.put(
            f"/api/admin/identity-providers/{pid}/directory/{unlinked[0]['id']}/preassigned",
            json={"groups": ["research"]},
        )
        assert pre.json()["preassigned_groups"] == ["research"]

    async def test_the_scheduler_runs_only_confirmed_due_providers(
        self, app: object, session_factory: async_sessionmaker[AsyncSession], tmp_path: Path
    ) -> None:
        from gateway.directory.service import DirectoryScheduler

        users = tmp_path / "users_database.yml"
        users.write_text(USERS)
        async with session_factory() as session:
            ready = await _provider(session, name="ready", sync_confirmed=True)
            ready.sync_config_encrypted = BOX.encrypt(json.dumps({"path": str(users)}))
            await _provider(session, name="unconfirmed", issuer="https://other.example.org")
            await session.commit()
            ready_id = ready.id
        settings = type("S", (), {"oidc": OIDCSettings()})()
        scheduler = DirectoryScheduler(session_factory, BOX, httpx.AsyncClient(), settings, None)  # type: ignore[arg-type]
        assert await scheduler.run_due() == [ready_id]
        assert await scheduler.run_due() == [], "not due again within the interval"
