"""Directory groups imported by hand, and the default group everyone joins.

The deployment that asked for this: GitLab as the identity provider, and the
operator's first sign-in created 67 groups, one per GitLab group they were in,
because every claimed name became a group. `GATEWAY_OIDC__GROUP_IMPORT=manual`
(the default now) records those names instead, for the console to import; the
default group (`GATEWAY_OIDC__DEFAULT_GROUP`, `users`) is what keeps a new
person able to bill something when nothing of theirs has been imported.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any

import pytest
from conftest import BEARER_ISSUER, FakeUpstream, Seeded, make_token
from conftest import bearer_auth as auth
from fastapi import FastAPI
from gateway.capabilities import Capabilities, effective_capabilities
from gateway.config import OIDCSettings, Settings
from gateway.directory.engine import add_manual_memberships
from gateway.models import (
    Group,
    GroupSource,
    GroupSync,
    IdentityEvent,
    IdentityProvider,
    Membership,
    MembershipSource,
    OIDCPolicyConfig,
    SeenGroup,
    SeenGroupUser,
    User,
)
from gateway.oidc import provision_user, sync_user_from_claims
from gateway.oidc_policy import OIDCPolicy
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
    policy: OIDCPolicy | None = None,
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
        policy=policy,
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


class TestTheDefaultGroupCarriesTheCapabilityDefaults:
    """`_group_named`'s insert used to write only name and source, so the
    default group's flags fell back to the column default (off): a fresh
    deployment whose default for new groups is "on" landed every new person
    in a chat-only `users` — the fresh-kit check caught it at the 0.13.0
    release. A group CREATED by the default-group rule carries the
    deployment's default; an existing group's flags are never touched."""

    async def test_the_first_sign_in_creates_users_usable(self, session: AsyncSession) -> None:
        """The deployment default "on" (what `ACCESS_*:-everyone` folds to)."""
        user = await _login(session, "new", [])
        group = (await session.execute(select(Group).where(Group.name == "users"))).scalar_one()
        assert group.code_agents_allowed is True
        assert group.api_keys_allowed is True
        assert effective_capabilities(user).code_agents is True

    async def test_the_default_off_creates_users_chat_only(self, session: AsyncSession) -> None:
        user = await _login(
            session, "new", [], policy=OIDCPolicy(code_agents_mode="off", api_keys_mode="off")
        )
        group = (await session.execute(select(Group).where(Group.name == "users"))).scalar_one()
        assert group.code_agents_allowed is False
        assert group.api_keys_allowed is False
        assert effective_capabilities(user).code_agents is False

    async def test_an_existing_users_group_keeps_its_flags(self, session: AsyncSession) -> None:
        """The flags apply only when the row is created: a `users` an
        administrator left off stays off, whatever the default says."""
        session.add(Group(name="users", code_agents_allowed=False, api_keys_allowed=False))
        await session.commit()
        user = await _login(session, "new", [])
        group = (await session.execute(select(Group).where(Group.name == "users"))).scalar_one()
        assert group.code_agents_allowed is False
        assert group.api_keys_allowed is False
        assert effective_capabilities(user).code_agents is False


_OFF = OIDCPolicy(code_agents_mode="off", api_keys_mode="off")


class TestEveryCreationPathCarriesTheDefaults:
    """One shared helper decides the flags a new group gets; each path that
    inserts a group is checked here for both answers of the default."""

    @pytest.mark.parametrize(("policy", "expected"), [(None, True), (_OFF, False)])
    async def test_auto_import_and_its_self_mapping(
        self, session: AsyncSession, policy: OIDCPolicy | None, expected: bool
    ) -> None:
        await _login(session, "op", ["gitlab/a"], OIDCSettings(group_import="auto"), policy=policy)
        group = (await session.execute(select(Group).where(Group.name == "gitlab/a"))).scalar_one()
        assert group.code_agents_allowed is expected
        assert group.api_keys_allowed is expected

    @pytest.mark.parametrize("expected", [True, False])
    async def test_the_directory_engine(self, session: AsyncSession, expected: bool) -> None:
        user = User(issuer="https://idp.test", subject="pre", email="pre@example.org")
        session.add(user)
        await session.flush()
        await session.refresh(user, attribute_names=["memberships"])
        await add_manual_memberships(
            session,
            user,
            ["pre-assigned"],
            defaults=Capabilities(code_agents=expected, api_keys=expected),
        )
        await session.commit()
        group = (
            await session.execute(select(Group).where(Group.name == "pre-assigned"))
        ).scalar_one()
        assert group.code_agents_allowed is expected
        assert group.api_keys_allowed is expected

    @pytest.mark.parametrize("access", ["on", "off"])
    async def test_gateway_seed(
        self,
        session: AsyncSession,
        settings: Settings,
        monkeypatch: pytest.MonkeyPatch,
        access: str,
    ) -> None:
        from gateway import cli
        from gateway.config import AccessSettings

        seeded = settings.model_copy(
            update={"access": AccessSettings(code_agents=access, api_keys=access)}  # type: ignore[arg-type]
        )
        monkeypatch.setattr(cli, "get_settings", lambda: seeded)
        await cli._seed(
            group_name="demo",
            model_name="demo-model",
            upstream_model="up",
            input_price=Decimal("1"),
            output_price=Decimal("1"),
            email="seed@example.org",
        )
        group = (await session.execute(select(Group).where(Group.name == "demo"))).scalar_one()
        assert group.code_agents_allowed is (access == "on")
        assert group.api_keys_allowed is (access == "on")

    async def test_gateway_seed_gives_a_new_default_provider_the_configured_key(
        self,
        session: AsyncSession,
        settings: Settings,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A `default` provider seed creates (the migration normally does) must
        carry GATEWAY_UPSTREAM__API_KEY, encrypted, as the migration's does."""
        from gateway import cli
        from gateway.models import Provider
        from gateway.secrets import SecretBox
        from pydantic import SecretStr

        box_key = "seed-test-secret-key"
        upstream = settings.upstream.model_copy(
            update={"api_key": SecretStr("sk-seed-upstream-key")}
        )
        seeded = settings.model_copy(
            update={"upstream": upstream, "secret_key": SecretStr(box_key)}
        )
        monkeypatch.setattr(cli, "get_settings", lambda: seeded)
        await cli._seed(
            group_name="demo",
            model_name="demo-model",
            upstream_model="up",
            input_price=Decimal("1"),
            output_price=Decimal("1"),
            email="seed@example.org",
        )
        provider = (
            await session.execute(select(Provider).where(Provider.name == "default"))
        ).scalar_one()
        assert provider.api_key_encrypted.startswith("v1:")
        assert "sk-seed-upstream-key" not in provider.api_key_encrypted
        assert provider.api_key_hint
        assert SecretBox([box_key]).decrypt(provider.api_key_encrypted) == "sk-seed-upstream-key"


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

    async def test_the_import_alias_maps_the_same_name(
        self,
        bearer_app: FastAPI,
        client: Any,
        seeded: Seeded,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        """The old import route is a deprecated alias of map's
        ``new_same_name`` target, kept one release so an old console keeps
        working: same group, and now also the self-mapping the rule needs."""
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
                    select(IdentityEvent).where(IdentityEvent.action == "group.map")
                )
            ).scalar_one()
            assert event.detail == {"target": "new_same_name", "group": "gitlab/eng"}
            assert event.issuer == BEARER_ISSUER

        policy = (await client.get("/api/admin/oidc/policy")).json()
        assert {"idp": "gitlab/eng", "local": "gitlab/eng"} in policy["group_mappings"]

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

    async def test_a_name_equal_to_an_existing_group_is_listed(
        self,
        bearer_app: FastAPI,
        client: Any,
        seeded: Seeded,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        """Under the mapping-only rule an unmapped claimed name resolves to
        nothing even when a local group carries the name — so it must be
        listed, or the console could never map it. This is the flip of the
        old rule, which hid same-name rows and resolved them silently."""
        as_user(bearer_app, await make_admin(session_factory, seeded))
        await self._sign_in_people(session_factory)
        await client.post("/api/admin/groups", json={"name": "gitlab/ops"})
        listing = (await client.get("/api/admin/groups/seen")).json()
        assert [i["name"] for i in listing["items"]] == ["gitlab/eng", "gitlab/ops"]

        # And a mapped name stops being listed: decided, not waiting.
        ops = next(i for i in listing["items"] if i["name"] == "gitlab/ops")
        ops_group_id = next(
            i["id"]
            for i in (await client.get("/api/admin/groups")).json()["items"]
            if i["name"] == "gitlab/ops"
        )
        mapped = await client.post(
            f"/api/admin/groups/seen/{ops['id']}/map",
            json={"target": "existing", "group_id": ops_group_id},
        )
        assert mapped.status_code == 200, mapped.text
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

    async def test_map_to_existing_grants_now_and_removes_the_seen(
        self,
        bearer_app: FastAPI,
        client: Any,
        seeded: Seeded,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        """Map to existing (ADR 0095 §4): the IdP name now grants the local
        group, carriers are granted now under every_login, the seen row goes,
        and the mapping is a new policy row on the append-only history."""
        as_user(bearer_app, await make_admin(session_factory, seeded))
        await self._sign_in_people(session_factory)
        async with session_factory() as session:
            session.add(Group(name="engineering"))
            await session.commit()
            target_id = (
                await session.execute(select(Group.id).where(Group.name == "engineering"))
            ).scalar_one()
        eng = next(
            i
            for i in (await client.get("/api/admin/groups/seen")).json()["items"]
            if i["name"] == "gitlab/eng"
        )

        response = await client.post(
            f"/api/admin/groups/seen/{eng['id']}/map",
            json={
                "target": "existing",
                "group_id": str(target_id),
                "reason": "eng is engineering here",
            },
        )
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["applied"] == "now"
        assert body["members_added"] == 2
        assert body["group"]["name"] == "engineering"

        async with session_factory() as session:
            people = (
                (
                    await session.execute(
                        select(User.subject)
                        .join(Membership, Membership.user_id == User.id)
                        .join(Group, Group.id == Membership.group_id)
                        .where(Group.name == "engineering")
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
                    select(IdentityEvent).where(IdentityEvent.action == "group.map")
                )
            ).scalar_one()
            assert event.detail == {"target": "existing", "group": "engineering"}
            assert event.target_label == "gitlab/eng"

        policy = (await client.get("/api/admin/oidc/policy")).json()
        assert {"idp": "gitlab/eng", "local": "engineering"} in policy["group_mappings"]
        assert policy["configured"]["reason"] == "eng is engineering here"

    async def test_map_waits_for_the_next_sign_in_elsewhere(
        self,
        bearer_app: FastAPI,
        client: Any,
        seeded: Seeded,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        as_user(bearer_app, await make_admin(session_factory, seeded))
        async with session_factory() as session:
            row = (await session.execute(select(IdentityProvider))).scalar_one()
            row.group_sync = GroupSync.FIRST_LOGIN
            session.add(Group(name="engineering"))
            await session.commit()
            target_id = (
                await session.execute(select(Group.id).where(Group.name == "engineering"))
            ).scalar_one()
        await self._sign_in_people(session_factory)
        ops = next(
            i
            for i in (await client.get("/api/admin/groups/seen")).json()["items"]
            if i["name"] == "gitlab/ops"
        )
        body = (
            await client.post(
                f"/api/admin/groups/seen/{ops['id']}/map",
                json={"target": "existing", "group_id": str(target_id)},
            )
        ).json()
        assert body["applied"] == "next_login" and body["members_added"] == 0

    async def test_map_skips_carriers_already_holding_the_group(
        self,
        bearer_app: FastAPI,
        client: Any,
        seeded: Seeded,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        """Map-to-existing onto a group a carrier already holds (the normal
        case: console-added, another mapped name, directory sync) must not
        500 on the membership primary key — and must not convert their manual
        membership into the directory's. Runs through the endpoint's commit,
        so the constraint is really exercised."""
        as_user(bearer_app, await make_admin(session_factory, seeded))
        await self._sign_in_people(session_factory)
        async with session_factory() as session:
            session.add(Group(name="engineering"))
            await session.flush()
            target_id = (
                await session.execute(select(Group.id).where(Group.name == "engineering"))
            ).scalar_one()
            p1 = (await session.execute(select(User).where(User.subject == "p1"))).scalar_one()
            session.add(
                Membership(user_id=p1.id, group_id=target_id, source=MembershipSource.MANUAL)
            )
            await session.commit()
        eng = next(
            i
            for i in (await client.get("/api/admin/groups/seen")).json()["items"]
            if i["name"] == "gitlab/eng"
        )
        response = await client.post(
            f"/api/admin/groups/seen/{eng['id']}/map",
            json={"target": "existing", "group_id": str(target_id)},
        )
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["members_added"] == 1
        assert body["group"]["member_count"] == 2
        async with session_factory() as session:
            memberships = (
                (
                    await session.execute(
                        select(Membership)
                        .join(Group, Group.id == Membership.group_id)
                        .where(Group.name == "engineering")
                    )
                )
                .scalars()
                .all()
            )
            by_user = {m.user_id: m.source for m in memberships}
            assert len(by_user) == 2
            p1 = (await session.execute(select(User).where(User.subject == "p1"))).scalar_one()
            assert by_user[p1.id] == MembershipSource.MANUAL

    async def test_map_keeps_mappings_saved_behind_the_worker(
        self,
        bearer_app: FastAPI,
        client: Any,
        seeded: Seeded,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        """The pairs come from the stored row, not the worker's up-to-10s-old
        policy: a mapping saved elsewhere in that window must survive."""
        from gateway.models import OIDCPolicyConfig

        as_user(bearer_app, await make_admin(session_factory, seeded))
        await self._sign_in_people(session_factory)
        async with session_factory() as session:
            session.add(Group(name="engineering"))
            session.add(OIDCPolicyConfig(group_mappings=[["gitlab/old", "engineering"]]))
            await session.commit()
            target_id = (
                await session.execute(select(Group.id).where(Group.name == "engineering"))
            ).scalar_one()
        eng = next(
            i
            for i in (await client.get("/api/admin/groups/seen")).json()["items"]
            if i["name"] == "gitlab/eng"
        )
        response = await client.post(
            f"/api/admin/groups/seen/{eng['id']}/map",
            json={"target": "existing", "group_id": str(target_id)},
        )
        assert response.status_code == 200, response.text
        policy = (await client.get("/api/admin/oidc/policy")).json()
        assert {"idp": "gitlab/old", "local": "engineering"} in policy["group_mappings"]
        assert {"idp": "gitlab/eng", "local": "engineering"} in policy["group_mappings"]

    async def test_map_refuses_an_already_mapped_name(
        self,
        bearer_app: FastAPI,
        client: Any,
        seeded: Seeded,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        as_user(bearer_app, await make_admin(session_factory, seeded))
        await self._sign_in_people(session_factory)
        async with session_factory() as session:
            session.add(Group(name="engineering"))
            await session.commit()
            target_id = (
                await session.execute(select(Group.id).where(Group.name == "engineering"))
            ).scalar_one()
        eng = next(
            i
            for i in (await client.get("/api/admin/groups/seen")).json()["items"]
            if i["name"] == "gitlab/eng"
        )
        first = await client.post(
            f"/api/admin/groups/seen/{eng['id']}/map",
            json={"target": "existing", "group_id": str(target_id)},
        )
        assert first.status_code == 200, first.text
        # The seen row is gone with the mapping, so a second map 404s...
        again = await client.post(
            f"/api/admin/groups/seen/{eng['id']}/map",
            json={"target": "existing", "group_id": str(target_id)},
        )
        assert again.status_code == 404
        # ...and a mapped name is never seen again: the next sign-in resolves
        # through it, so nothing waits.
        async with session_factory() as session:
            user = await provision_user(
                session,
                issuer=BEARER_ISSUER,
                subject="p2",
                email="p2@example.org",
                display_name=None,
                group_names=["engineering"],
                claimed_names=["gitlab/eng"],
                settings=OIDCSettings(),
                policy=OIDCPolicy(join_mode="open", group_mappings={"gitlab/eng": "engineering"}),
                group_mappings={"gitlab/eng": "engineering"},
            )
            await session.commit()
            assert {m.group.name for m in user.memberships} == {"engineering", "users"}
        listing = (await client.get("/api/admin/groups/seen")).json()["items"]
        assert [i["name"] for i in listing] == ["gitlab/ops"]
        # The refusal for an already-mapped name survives for the race it
        # guards (two workers mapping while one commit is in flight): a seen
        # row for a name the stored policy already maps is refused, not
        # double-written.
        async with session_factory() as session:
            session.add(SeenGroup(issuer=BEARER_ISSUER, name="gitlab/eng"))
            await session.commit()
            stale = (
                await session.execute(select(SeenGroup).where(SeenGroup.name == "gitlab/eng"))
            ).scalar_one()
        duplicate = await client.post(
            f"/api/admin/groups/seen/{stale.id}/map",
            json={"target": "existing", "group_id": str(target_id)},
        )
        assert duplicate.status_code == 400, duplicate.text

    async def test_map_new_named_creates_the_group_and_the_mapping(
        self,
        bearer_app: FastAPI,
        client: Any,
        seeded: Seeded,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        """``new_named``: the IdP name becomes a group under another name —
        the third way to answer "what does this IdP group mean here"."""
        as_user(bearer_app, await make_admin(session_factory, seeded))
        await self._sign_in_people(session_factory)
        eng = next(
            i
            for i in (await client.get("/api/admin/groups/seen")).json()["items"]
            if i["name"] == "gitlab/eng"
        )
        response = await client.post(
            f"/api/admin/groups/seen/{eng['id']}/map",
            json={"target": "new_named", "name": "engineering"},
        )
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["group"]["name"] == "engineering"
        assert body["group"]["source"] == "oidc"
        assert body["members_added"] == 2

        async with session_factory() as session:
            group = (
                await session.execute(select(Group).where(Group.name == "engineering"))
            ).scalar_one()
            assert group.source is GroupSource.OIDC
            assert (
                await session.execute(select(SeenGroup).where(SeenGroup.name == "gitlab/eng"))
            ).scalar_one_or_none() is None
        policy = (await client.get("/api/admin/oidc/policy")).json()
        assert {"idp": "gitlab/eng", "local": "engineering"} in policy["group_mappings"]

    @pytest.mark.parametrize(
        ("payload", "clash"),
        [
            ({"target": "new_same_name"}, "gitlab/eng"),
            ({"target": "new_named", "name": "taken"}, "taken"),
        ],
    )
    async def test_map_new_targets_refuse_a_taken_name(
        self,
        bearer_app: FastAPI,
        client: Any,
        seeded: Seeded,
        session_factory: async_sessionmaker[AsyncSession],
        payload: dict[str, str],
        clash: str,
    ) -> None:
        """A new_* target whose name is already a group answers 409
        ``group_exists`` — the admin picks ``existing`` then, which is the
        same decision with the group they meant."""
        as_user(bearer_app, await make_admin(session_factory, seeded))
        await self._sign_in_people(session_factory)
        await client.post("/api/admin/groups", json={"name": clash})
        seen = (await client.get("/api/admin/groups/seen")).json()["items"]
        if payload["target"] == "new_same_name":
            row = next(i for i in seen if i["name"] == "gitlab/eng")
        else:
            row = next(i for i in seen if i["name"] == "gitlab/ops")
        response = await client.post(f"/api/admin/groups/seen/{row['id']}/map", json=payload)
        assert response.status_code == 409, response.text
        assert response.json()["error"]["code"] == "group_exists"
        # Nothing was decided: the seen row and the mapping are untouched.
        listing = (await client.get("/api/admin/groups/seen")).json()["items"]
        assert row["id"] in [i["id"] for i in listing]
        policy = (await client.get("/api/admin/oidc/policy")).json()
        claimed_name = row["name"]
        assert all(rule["idp"] != claimed_name for rule in policy["group_mappings"])

    async def test_unmap_appends_a_row_without_the_pair(
        self,
        bearer_app: FastAPI,
        client: Any,
        seeded: Seeded,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        """Unmap needs no endpoint: PUT the mappings list without the pair,
        and the append-only history records the removal. (The seeded
        deployment carries the migration-0058 self-mapping; the carry-forward
        keeps it, which is why the assertions read the pair, not the list.)"""
        as_user(bearer_app, await make_admin(session_factory, seeded))
        response = await client.put(
            "/api/admin/oidc/policy",
            json={
                "group_mappings": [{"idp": "gitlab/eng", "local": "engineering"}],
                "reason": "map",
            },
        )
        assert response.status_code == 200, response.text
        assert response.json()["group_mappings"] == [{"idp": "gitlab/eng", "local": "engineering"}]
        response = await client.put(
            "/api/admin/oidc/policy", json={"group_mappings": [], "reason": "unmap"}
        )
        assert response.status_code == 200, response.text
        assert response.json()["group_mappings"] == []
        async with session_factory() as session:
            rows = (
                (
                    await session.execute(
                        select(OIDCPolicyConfig).order_by(OIDCPolicyConfig.created_at)
                    )
                )
                .scalars()
                .all()
            )
            assert rows[-1].group_mappings == []
            assert ["gitlab/eng", "engineering"] in rows[-2].group_mappings


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
