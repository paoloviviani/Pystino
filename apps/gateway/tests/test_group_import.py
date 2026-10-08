"""Directory groups imported by hand, and the default group everyone joins.

The deployment that asked for this: GitLab as the identity provider, and the
operator's first sign-in created 67 groups, one per GitLab group they were in,
because every claimed name became a group. `GATEWAY_OIDC__GROUP_IMPORT=manual`
(the default now) records those names instead, for the console to import; the
default group (`GATEWAY_OIDC__DEFAULT_GROUP`, `users`) is what keeps a new
person able to bill something when nothing of theirs has been imported.
"""

from __future__ import annotations

from typing import Any

import pytest
from conftest import BEARER_ISSUER, FakeUpstream, Seeded, make_token
from conftest import bearer_auth as auth
from fastapi import FastAPI
from gateway.config import OIDCSettings, Settings
from gateway.models import (
    Group,
    GroupSource,
    GroupSync,
    IdentityEvent,
    IdentityProvider,
    Membership,
    MembershipSource,
    SeenGroup,
    SeenGroupUser,
    User,
)
from gateway.oidc import provision_user, sync_user_from_claims
from joserfc.jwk import RSAKey
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from test_admin import as_user, make_admin
from test_query_counts import counted, summarise

ISSUER = "https://gitlab.example.org"


async def _login(
    session: AsyncSession,
    subject: str,
    groups: list[str],
    settings: OIDCSettings | None = None,
    *,
    touch_login: bool = True,
) -> User:
    user = await provision_user(
        session,
        issuer=ISSUER,
        subject=subject,
        email=f"{subject}@example.org",
        display_name=None,
        group_names=groups,
        settings=settings or OIDCSettings(),
        touch_login=touch_login,
    )
    await session.commit()
    return user


async def _names(session: AsyncSession, user: User) -> set[str]:
    await session.refresh(user, attribute_names=["memberships"])
    return {m.group.name for m in user.memberships}


class TestTheSetting:
    def test_manual_is_the_default(self) -> None:
        assert OIDCSettings().group_import == "manual"
        assert OIDCSettings().default_group == "users"

    def test_the_old_spelling_keeps_its_meaning(self) -> None:
        """An upgrade that had set AUTO_CREATE_GROUPS keeps what it chose."""
        assert OIDCSettings(auto_create_groups=True).group_import == "auto"
        assert OIDCSettings(auto_create_groups=False).group_import == "manual"

    def test_the_two_spellings_may_not_disagree(self) -> None:
        with pytest.raises(ValueError, match="contradicts"):
            OIDCSettings(auto_create_groups=True, group_import="manual")
        assert OIDCSettings(auto_create_groups=True, group_import="auto").group_import == "auto"

    def test_the_allowlist_reads_a_comma_list_or_json_from_the_environment(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        for raw, expected in [
            ("eng, ops", ["eng", "ops"]),
            ('["eng","ops"]', ["eng", "ops"]),
            ("", []),
        ]:
            monkeypatch.setenv("GATEWAY_OIDC__GROUP_ALLOWLIST", raw)
            assert Settings(_env_file=None).oidc.group_allowlist == expected  # type: ignore[call-arg]


class TestManualImport:
    async def test_nothing_is_created_and_the_names_are_recorded(
        self, session: AsyncSession
    ) -> None:
        user = await _login(session, "op", ["gitlab/a", "gitlab/b"])

        groups = (await session.execute(select(Group.name))).scalars().all()
        assert groups == ["users"], "only the default group exists"
        assert await _names(session, user) == {"users"}
        seen = (await session.execute(select(SeenGroup))).scalars().all()
        assert {(s.issuer, s.name) for s in seen} == {(ISSUER, "gitlab/a"), (ISSUER, "gitlab/b")}
        links = (await session.execute(select(SeenGroupUser))).scalars().all()
        assert {link.user_id for link in links} == {user.id}
        assert sorted(user.unresolved_group_names or []) == ["gitlab/a", "gitlab/b"]

    async def test_existing_groups_still_resolve(self, session: AsyncSession) -> None:
        """The upgrade guarantee: a group that exists keeps being granted."""
        session.add(Group(name="gitlab/a", source=GroupSource.OIDC))
        await session.commit()

        user = await _login(session, "op", ["gitlab/a", "gitlab/b"])

        assert await _names(session, user) == {"gitlab/a", "users"}
        membership = next(m for m in user.memberships if m.group.name == "gitlab/a")
        assert membership.source is MembershipSource.OIDC
        assert user.unresolved_group_names == ["gitlab/b"]

    async def test_people_are_counted_and_follow_their_tokens(self, session: AsyncSession) -> None:
        one = await _login(session, "one", ["shared", "only-one"])
        await _login(session, "two", ["shared"])
        # One leaves "only-one" in the directory: they stop counting for it.
        await _login(session, "one", ["shared"])

        rows = (
            await session.execute(
                select(SeenGroup.name, SeenGroupUser.user_id).join(
                    SeenGroupUser, SeenGroupUser.seen_group_id == SeenGroup.id
                )
            )
        ).all()
        by_name: dict[str, int] = {}
        for name, _ in rows:
            by_name[name] = by_name.get(name, 0) + 1
        assert by_name == {"shared": 2}
        assert one.unresolved_group_names == ["shared"]

    async def test_an_unchanged_sign_in_writes_only_the_last_seen_time(
        self, session: AsyncSession
    ) -> None:
        await _login(session, "op", ["gitlab/a", "gitlab/b"])
        engine = session.bind
        with counted(engine) as statements:  # type: ignore[arg-type]
            await _login(session, "op", ["gitlab/a", "gitlab/b"])
        touching = [
            s
            for s in statements
            if "seen_group" in s and s.lstrip().upper().startswith(("INSERT", "UPDATE", "DELETE"))
        ]
        assert len(touching) == 1, summarise(touching)
        assert touching[0].lstrip().upper().startswith("UPDATE SEEN_GROUPS")

    async def test_auto_mode_is_unchanged(self, session: AsyncSession) -> None:
        user = await _login(session, "op", ["gitlab/a"], OIDCSettings(group_import="auto"))
        assert await _names(session, user) == {"gitlab/a", "users"}
        group = (await session.execute(select(Group).where(Group.name == "gitlab/a"))).scalar_one()
        assert group.source is GroupSource.OIDC
        assert (await session.execute(select(SeenGroup))).scalars().all() == []


class TestTheDefaultGroup:
    async def test_a_new_person_joins_it_and_bills_it(self, session: AsyncSession) -> None:
        user = await _login(session, "new", ["not-imported"])
        group = (await session.execute(select(Group).where(Group.name == "users"))).scalar_one()
        assert group.source is GroupSource.MANUAL
        assert [m.source for m in user.memberships] == [MembershipSource.MANUAL]
        assert user.default_billing_group_id == group.id
        assert user.default_group_granted_at is not None

    async def test_a_sole_directory_group_is_still_the_default_bill(
        self, session: AsyncSession
    ) -> None:
        """`users` is not a choice anyone made, so it does not make a person
        with one real group "multi-group" and leave them without a default."""
        session.add(Group(name="research"))
        await session.commit()
        user = await _login(session, "new", ["research"])
        assert await _names(session, user) == {"research", "users"}
        assert user.default_billing_group is not None
        assert user.default_billing_group.name == "research"

    async def test_several_groups_and_no_choice_fall_back_to_it(
        self, session: AsyncSession
    ) -> None:
        session.add_all([Group(name="a"), Group(name="b")])
        await session.commit()
        user = await _login(session, "new", ["a", "b"])
        assert user.default_billing_group is not None
        assert user.default_billing_group.name == "users"

    async def test_the_claim_sync_never_removes_it(self, session: AsyncSession) -> None:
        session.add(Group(name="research"))
        await session.commit()
        user = await _login(session, "new", ["research"])
        user = await _login(session, "new", [])
        assert await _names(session, user) == {"users"}

    async def test_an_administrators_removal_sticks(self, session: AsyncSession) -> None:
        user = await _login(session, "new", [])
        membership = (
            await session.execute(select(Membership).where(Membership.user_id == user.id))
        ).scalar_one()
        await session.delete(membership)
        await session.commit()

        user = await _login(session, "new", [])
        assert await _names(session, user) == set()

    async def test_an_existing_person_gets_it_at_their_next_sign_in_only(
        self, session: AsyncSession
    ) -> None:
        """The backfill: a person from before the default group existed (no
        stamp). A plain `/v1` call does not do it — that is the hot path —
        their next sign-in does."""
        session.add(Group(name="research"))
        legacy = User(issuer=ISSUER, subject="old", email="old@example.org")
        session.add(legacy)
        await session.commit()

        user = await _login(session, "old", ["research"], touch_login=False)
        assert await _names(session, user) == {"research"}
        user = await _login(session, "old", ["research"])
        assert await _names(session, user) == {"research", "users"}

    async def test_it_converges_with_the_bundled_authelia_users_group(
        self, session: AsyncSession
    ) -> None:
        """An existing Authelia deployment's `users` group, made by the old
        auto-create (source oidc) and holding its people: the default group is
        that group by name, so nobody gets a second one or a changed row."""
        old = Group(name="users", source=GroupSource.OIDC)
        person = User(issuer=ISSUER, subject="bundled", email="b@example.org")
        session.add_all([old, person])
        await session.flush()
        session.add(Membership(user_id=person.id, group_id=old.id, source=MembershipSource.OIDC))
        await session.commit()

        # The bundled provider's own provisioning: group_source "none".
        user = await provision_user(
            session,
            issuer=ISSUER,
            subject="bundled",
            email="b@example.org",
            display_name=None,
            group_names=["users"],
            settings=OIDCSettings(),
            group_source="none",
        )
        await session.commit()

        groups = (await session.execute(select(Group))).scalars().all()
        assert [(g.name, g.source) for g in groups] == [("users", GroupSource.OIDC)]
        await session.refresh(user, attribute_names=["memberships"])
        assert [m.group_id for m in user.memberships] == [old.id]
        assert user.default_group_granted_at is not None

    async def test_a_directory_membership_in_it_becomes_the_consoles(
        self, session: AsyncSession
    ) -> None:
        """Held through a claim before the upgrade (a directory group called
        `users`, auto-created): the grant keeps it and makes it the console's,
        so the claim sync cannot take away the group the grant promises."""
        old = Group(name="users", source=GroupSource.OIDC)
        person = User(issuer=ISSUER, subject="gl", email="gl@example.org")
        session.add_all([old, person])
        await session.flush()
        session.add(Membership(user_id=person.id, group_id=old.id, source=MembershipSource.OIDC))
        await session.commit()

        user = await _login(session, "gl", [])
        assert [m.source for m in user.memberships] == [MembershipSource.MANUAL]
        user = await _login(session, "gl", [])
        assert await _names(session, user) == {"users"}

    async def test_empty_turns_it_off(self, session: AsyncSession) -> None:
        user = await _login(session, "new", [], OIDCSettings(default_group=""))
        assert user.memberships == []
        assert (await session.execute(select(Group))).scalars().all() == []


class TestTheHotPath:
    async def test_an_unimported_name_is_not_a_divergence(self, session: AsyncSession) -> None:
        """Without the recorded names, every request from someone in one
        unimported group would re-provision: `claimed - held` never empties."""
        await _login(session, "op", ["gitlab/a"])
        claims = {"iss": ISSUER, "sub": "op", "groups": ["gitlab/a"]}
        engine = session.bind
        with counted(engine) as statements:  # type: ignore[arg-type]
            await sync_user_from_claims(session, claims=claims, settings=OIDCSettings())
            await session.commit()
        # The user and its memberships, nothing else: re-provisioning writes nothing
        # either when nothing changed, so the count is what tells them apart.
        assert len(statements) == 2, summarise(statements)

    async def test_a_steady_bearer_request_writes_nothing(
        self,
        bearer_app: FastAPI,
        client: Any,
        signing_key: RSAKey,
    ) -> None:
        token = make_token(signing_key, sub="gitlab-op", groups=[f"gl/{i}" for i in range(67)])
        bearer_app.state.settings.oidc.chat_client_id = "llm-chat"
        assert (await client.post("/v1/session/announce", headers=auth(token))).status_code == 200

        engine = bearer_app.state.engine
        with counted(engine) as statements:
            assert (await client.get("/v1/models", headers=auth(token))).status_code == 200
        writes = [
            s for s in statements if s.lstrip().upper().startswith(("INSERT", "UPDATE", "DELETE"))
        ]
        assert writes == [], summarise(writes)
        # test_query_counts' bearer budget, unchanged by 67 unimported groups.
        selects = [s for s in statements if s.lstrip().upper().startswith("SELECT")]
        assert len(selects) <= 5, summarise(selects)


class TestTheAdminApi:
    async def _sign_in_people(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        async with session_factory() as session:
            for subject, groups in [
                ("p1", ["gitlab/eng", "gitlab/ops"]),
                ("p2", ["gitlab/eng"]),
            ]:
                await provision_user(
                    session,
                    issuer=BEARER_ISSUER,
                    subject=subject,
                    email=f"{subject}@example.org",
                    display_name=None,
                    group_names=groups,
                    settings=OIDCSettings(),
                )
            await session.commit()

    async def test_list_search_dismiss_restore(
        self,
        bearer_app: FastAPI,
        client: Any,
        seeded: Seeded,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        as_user(bearer_app, await make_admin(session_factory, seeded))
        await self._sign_in_people(session_factory)

        listing = (await client.get("/api/admin/groups/seen")).json()
        assert listing["group_import"] == "manual"
        assert listing["default_group"] == "users"
        assert [(i["name"], i["people"]) for i in listing["items"]] == [
            ("gitlab/eng", 2),
            ("gitlab/ops", 1),
        ]
        assert listing["items"][0]["provider"] == "default"

        found = (await client.get("/api/admin/groups/seen", params={"q": "OPS"})).json()
        assert [i["name"] for i in found["items"]] == ["gitlab/ops"]

        ops = found["items"][0]["id"]
        assert (await client.post(f"/api/admin/groups/seen/{ops}/dismiss")).status_code == 204
        listing = (await client.get("/api/admin/groups/seen")).json()
        assert [i["name"] for i in listing["items"]] == ["gitlab/eng"]
        dismissed = (await client.get("/api/admin/groups/seen?dismissed=true")).json()
        assert [i["name"] for i in dismissed["items"]] == ["gitlab/ops"]

        assert (await client.post(f"/api/admin/groups/seen/{ops}/restore")).status_code == 204
        listing = (await client.get("/api/admin/groups/seen")).json()
        assert {i["name"] for i in listing["items"]} == {"gitlab/eng", "gitlab/ops"}

        async with session_factory() as session:
            actions = (
                (await session.execute(select(IdentityEvent.action, IdentityEvent.target_label)))
                .tuples()
                .all()
            )
        assert ("group.dismiss", "gitlab/ops") in actions
        assert ("group.restore", "gitlab/ops") in actions

    async def test_import_grants_at_once_on_an_every_login_provider(
        self,
        bearer_app: FastAPI,
        client: Any,
        seeded: Seeded,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        as_user(bearer_app, await make_admin(session_factory, seeded))
        await self._sign_in_people(session_factory)
        eng = next(
            i
            for i in (await client.get("/api/admin/groups/seen")).json()["items"]
            if i["name"] == "gitlab/eng"
        )

        response = await client.post(f"/api/admin/groups/seen/{eng['id']}/import")
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["applied"] == "now"
        assert body["members_added"] == 2
        assert body["group"]["source"] == "oidc"

        async with session_factory() as session:
            people = (
                (
                    await session.execute(
                        select(User.subject)
                        .join(Membership, Membership.user_id == User.id)
                        .join(Group, Group.id == Membership.group_id)
                        .where(Group.name == "gitlab/eng")
                    )
                )
                .scalars()
                .all()
            )
            assert sorted(people) == ["p1", "p2"]
            assert (
                await session.execute(select(SeenGroup).where(SeenGroup.name == "gitlab/eng"))
            ).scalar_one_or_none() is None
            event = (
                await session.execute(
                    select(IdentityEvent).where(IdentityEvent.action == "group.import")
                )
            ).scalar_one()
            assert event.detail == {"members": 2} and event.issuer == BEARER_ISSUER

        listing = (await client.get("/api/admin/groups/seen")).json()
        assert [i["name"] for i in listing["items"]] == ["gitlab/ops"]
        again = await client.post(f"/api/admin/groups/seen/{eng['id']}/import")
        assert again.status_code == 404

    async def test_import_waits_for_the_next_sign_in_elsewhere(
        self,
        bearer_app: FastAPI,
        client: Any,
        seeded: Seeded,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        """Under first_login the claim of a past sign-in is not applied again,
        so the import grants nothing now; a person who signs in for the first
        time afterwards gets it."""
        as_user(bearer_app, await make_admin(session_factory, seeded))
        async with session_factory() as session:
            row = (await session.execute(select(IdentityProvider))).scalar_one()
            row.group_sync = GroupSync.FIRST_LOGIN
            await session.commit()
        await self._sign_in_people(session_factory)
        ops = next(
            i
            for i in (await client.get("/api/admin/groups/seen")).json()["items"]
            if i["name"] == "gitlab/ops"
        )
        body = (await client.post(f"/api/admin/groups/seen/{ops['id']}/import")).json()
        assert body["applied"] == "next_login" and body["members_added"] == 0

        async with session_factory() as session:
            newcomer = await provision_user(
                session,
                issuer=BEARER_ISSUER,
                subject="p3",
                email="p3@example.org",
                display_name=None,
                group_names=["gitlab/ops"],
                settings=OIDCSettings(),
                group_sync=GroupSync.FIRST_LOGIN,
            )
            await session.commit()
            assert {m.group.name for m in newcomer.memberships} == {"gitlab/ops", "users"}

    async def test_a_name_that_became_a_group_is_not_listed(
        self,
        bearer_app: FastAPI,
        client: Any,
        seeded: Seeded,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        as_user(bearer_app, await make_admin(session_factory, seeded))
        await self._sign_in_people(session_factory)
        await client.post("/api/admin/groups", json={"name": "gitlab/ops"})
        listing = (await client.get("/api/admin/groups/seen")).json()
        assert [i["name"] for i in listing["items"]] == ["gitlab/eng"]

    async def test_it_is_for_administrators(
        self,
        bearer_app: FastAPI,
        client: Any,
        seeded: Seeded,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        as_user(bearer_app, await make_admin(session_factory, seeded, admin=False))
        assert (await client.get("/api/admin/groups/seen")).status_code == 403


class TestANewGitLabUserCanChat:
    async def test_sign_in_then_send_a_chat_message(
        self,
        bearer_app: FastAPI,
        client: Any,
        seeded: Seeded,
        signing_key: RSAKey,
        session_factory: async_sessionmaker[AsyncSession],
        fake_upstream: FakeUpstream,
    ) -> None:
        """The whole point of the default group: a brand-new person whose
        directory groups are all unimported still has a group to bill, and a
        public model to use, at their first message. No quota rule is needed:
        a group with none is unlimited, as every new group always was."""
        async with session_factory() as session:
            model = await session.get(type(seeded.model), seeded.model.id)
            assert model is not None
            model.is_public = True
            await session.commit()
        bearer_app.state.settings.oidc.chat_client_id = "llm-chat"
        token = make_token(
            signing_key,
            sub="gitlab-newcomer",
            email="newcomer@example.org",
            groups=["gitlab/one", "gitlab/two"],
        )
        announced = await client.post("/v1/session/announce", headers=auth(token))
        assert announced.status_code == 200, announced.text

        fake_upstream.set_json(
            {
                "id": "c1",
                "object": "chat.completion",
                "choices": [{"index": 0, "message": {"role": "assistant", "content": "hi"}}],
                "usage": {"prompt_tokens": 3, "completion_tokens": 1, "total_tokens": 4},
            }
        )
        response = await client.post(
            "/v1/chat/completions",
            json={"model": seeded.model.name, "messages": [{"role": "user", "content": "hi"}]},
            headers=auth(token),
        )
        assert response.status_code == 200, response.text

        me = (await client.get("/v1/me", headers=auth(token))).json()
        assert me["billing_group"] == "users"
