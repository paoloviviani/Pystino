"""The identity policy (ADR 0048) and the account-management endpoints.

What is tested here and why: a wrong answer in provisioning is a security
hole, not a stack trace — a gate that does not gate lets strangers mint
accounts; a gate that over-gates locks every legitimate user out. So the
policy fold, the provisioning gate, and the two account endpoints each get
their own assertions, against the real application and the real resolver.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

import httpx
import pytest
import pytest_asyncio
from conftest import LEGACY_GROUPS
from gateway.config import OIDCSettings
from gateway.models import ApiKey, Group, OIDCPolicyConfig, User
from gateway.oidc import ProvisioningRefused, provision_user
from gateway.oidc_policy import (
    OIDCPolicy,
    OIDCPolicyResolver,
    effective_policy,
    environment_policy,
)
from gateway.security import generate_api_key
from pydantic import SecretStr
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from test_admin import make_admin
from test_my_limits import session_cookie

# -- the fold: row decisions over the environment baseline --------------------


class TestEffectivePolicy:
    def test_no_row_means_the_environment(self) -> None:
        settings = OIDCSettings(**LEGACY_GROUPS, groups_claim="roles")
        policy = effective_policy(settings, None)
        assert policy.auto_provision is True
        assert policy.groups_claim == "roles"
        assert policy.source == "environment"

    def test_a_row_decides_only_what_it_names(self) -> None:
        settings = OIDCSettings(**LEGACY_GROUPS, groups_claim="groups")
        row = OIDCPolicyConfig(auto_provision=False)
        policy = effective_policy(settings, row)
        # auto_provision decided by the row; unknown-user policy and claim
        # still the environment's, and the source split says which is which.
        assert policy.auto_provision is False
        assert policy.unknown_user_policy == "refuse"
        assert policy.groups_claim == "groups"
        assert policy.sources == {"auto_provision": "console"}
        assert policy.source == "console"

    def test_a_hand_mangled_row_does_not_crash_every_login(self) -> None:
        # The check constraint holds for anything written through the API; a
        # row edited by hand does not get to take the deployment down.
        settings = OIDCSettings(**LEGACY_GROUPS)
        row = OIDCPolicyConfig(unknown_user_policy="nonsense")
        policy = effective_policy(settings, row)
        assert policy.unknown_user_policy == "refuse"
        assert "unknown_user_policy" not in policy.sources

    def test_mappings_fold_into_a_dictionary(self) -> None:
        row = OIDCPolicyConfig(group_mappings=[["idp-a", "local-one"], ["idp-b", "local-one"]])
        policy = effective_policy(OIDCSettings(**LEGACY_GROUPS), row)
        assert policy.group_mappings == {"idp-a": "local-one", "idp-b": "local-one"}


class TestJoinModeFold:
    """ADR 0095: one join mode folds from row, legacy columns, or env, in order."""

    def test_legacy_open_row_maps_to_open(self) -> None:
        policy = effective_policy(
            OIDCSettings(**LEGACY_GROUPS), OIDCPolicyConfig(auto_provision=True)
        )
        assert policy.join_mode == "open"
        assert policy.sources == {"auto_provision": "console"}
        assert policy.auto_provision is True
        assert policy.unknown_user_policy == "refuse"

    def test_legacy_create_inactive_maps_to_approval(self) -> None:
        policy = effective_policy(
            OIDCSettings(**LEGACY_GROUPS),
            OIDCPolicyConfig(auto_provision=False, unknown_user_policy="create_inactive"),
        )
        assert policy.join_mode == "approval"
        assert policy.auto_provision is False
        assert policy.unknown_user_policy == "create_inactive"

    def test_legacy_refuse_maps_to_invite(self) -> None:
        policy = effective_policy(
            OIDCSettings(**LEGACY_GROUPS),
            OIDCPolicyConfig(auto_provision=False, unknown_user_policy="refuse"),
        )
        assert policy.join_mode == "invite"
        assert policy.auto_provision is False
        assert policy.unknown_user_policy == "refuse"

    def test_legacy_silent_row_maps_to_invite(self) -> None:
        # auto_provision False with no unknown-user policy: the old code
        # refused (the env baseline), so the fold reads invite, honestly now
        # that pre-registration exists.
        policy = effective_policy(
            OIDCSettings(**LEGACY_GROUPS), OIDCPolicyConfig(auto_provision=False)
        )
        assert policy.join_mode == "invite"

    def test_join_mode_row_wins_over_legacy_columns(self) -> None:
        policy = effective_policy(
            OIDCSettings(**LEGACY_GROUPS),
            OIDCPolicyConfig(join_mode="open", auto_provision=False),
        )
        assert policy.join_mode == "open"
        assert policy.auto_provision is True

    def test_no_row_means_the_env_join_mode(self) -> None:
        policy = effective_policy(OIDCSettings(**LEGACY_GROUPS), None)
        assert policy.join_mode == "open"
        assert policy.group_fallback == "refuse"
        assert policy.required_groups == ()
        assert "join_mode" not in policy.sources

    def test_env_join_mode_and_groups(self) -> None:
        settings = OIDCSettings(
            **LEGACY_GROUPS, join_mode="group", required_groups=["staff"], group_fallback="approval"
        )
        policy = effective_policy(settings, None)
        assert policy.join_mode == "group"
        assert policy.required_groups == ("staff",)
        assert policy.group_fallback == "approval"

    def test_env_required_groups_accepts_a_comma_string(self) -> None:
        settings = OIDCSettings(**LEGACY_GROUPS, required_groups="staff, ops")
        assert effective_policy(settings, None).required_groups == ("staff", "ops")

    def test_a_hand_mangled_join_mode_defers_to_env(self) -> None:
        settings = OIDCSettings(**LEGACY_GROUPS, join_mode="invite")
        policy = effective_policy(settings, OIDCPolicyConfig(join_mode="nonsense"))
        assert policy.join_mode == "invite"
        assert "join_mode" not in policy.sources

    def test_group_mode_without_groups_refuses_startup(self) -> None:
        import pytest as _pytest

        with _pytest.raises(ValueError, match="REQUIRED_GROUPS"):
            OIDCSettings(**LEGACY_GROUPS, join_mode="group")

    def test_group_fallback_rejects_other_words(self) -> None:
        import pytest as _pytest

        with _pytest.raises(Exception, match="group_fallback"):
            OIDCSettings(**LEGACY_GROUPS, group_fallback="maybe")

    async def test_env_required_groups_warn_for_unknown_names(
        self, session_factory: async_sessionmaker[AsyncSession], caplog: Any
    ) -> None:
        import logging as _logging

        settings = OIDCSettings(**LEGACY_GROUPS, join_mode="group", required_groups=["ghost"])
        resolver = OIDCPolicyResolver(settings, session_factory)
        with caplog.at_level(_logging.WARNING, logger="gateway.oidc_policy"):
            await resolver.refresh_once()
        assert "ghost" in caplog.text
        # Warned once per distinct set: the ten-second poll must not spam.
        caplog.clear()
        with caplog.at_level(_logging.WARNING, logger="gateway.oidc_policy"):
            await resolver.refresh_once()
        assert "ghost" not in caplog.text


class TestMapGroupNames:
    def test_an_unmapped_name_is_dropped_even_when_it_names_a_group(self) -> None:
        policy = OIDCPolicy(
            join_mode="open",
            groups_claim="groups",
            group_mappings={"idp-team": "research"},
        )
        assert policy.map_claim_group_names(["idp-team", "other"]) == ["research"]

    def test_many_to_one_folds_without_repetition(self) -> None:
        policy = OIDCPolicy(
            join_mode="open",
            groups_claim="groups",
            group_mappings={"a": "one", "b": "one"},
        )
        assert policy.map_claim_group_names(["a", "b"]) == ["one"]

    def test_a_directory_source_provider_keeps_its_names_vouched(self) -> None:
        policy = OIDCPolicy(
            join_mode="open",
            groups_claim="groups",
            group_mappings={"a": "one"},
        )
        assert policy.map_claim_group_names(["a", "b"], claim_source=False) == ["one", "b"]


# -- the provisioning gate ----------------------------------------------------


@pytest_asyncio.fixture
async def policy_resolver(
    app: Any, session_factory: async_sessionmaker[AsyncSession]
) -> AsyncIterator[OIDCPolicyResolver]:
    """The resolver the app fixture wired, re-readable from tests."""
    resolver: OIDCPolicyResolver = app.state.oidc_policy
    yield resolver
    # A test that wrote a row must not leak it into the next test's polls:
    # every test gets a fresh in-memory database, so this is belt and braces.
    resolver._policy = environment_policy(app.state.settings.oidc)


async def write_policy(session_factory: async_sessionmaker[AsyncSession], **columns: Any) -> None:
    async with session_factory() as session:
        session.add(OIDCPolicyConfig(**columns))
        await session.commit()


class TestProvisioningGate:
    async def test_off_and_refusing_makes_no_user(
        self, session_factory: async_sessionmaker[AsyncSession]
    ) -> None:
        await write_policy(session_factory, auto_provision=False)
        policy = OIDCPolicy(
            join_mode="invite",
            groups_claim="groups",
        )
        with pytest.raises(ProvisioningRefused):
            async with session_factory() as session:
                await provision_user(
                    session,
                    issuer="https://idp.test",
                    subject="stranger-1",
                    email=None,
                    display_name=None,
                    group_names=[],
                    settings=OIDCSettings(**LEGACY_GROUPS),
                    touch_login=False,
                    policy=policy,
                )

    async def test_create_inactive_leaves_an_approvable_row(
        self, session_factory: async_sessionmaker[AsyncSession]
    ) -> None:
        await write_policy(session_factory, auto_provision=False)
        policy = OIDCPolicy(
            join_mode="approval",
            groups_claim="groups",
        )
        async with session_factory() as session:
            user = await provision_user(
                session,
                issuer="https://idp.test",
                subject="stranger-2",
                email="stranger@example.org",
                display_name=None,
                group_names=["research"],
                settings=OIDCSettings(**LEGACY_GROUPS),
                touch_login=False,
                policy=policy,
            )
            # Disabled, and the group followed them in: enabling the account
            # is the whole approval, not enabling plus re-doing their
            # membership by hand.
            assert user.is_active is False
            names = (
                (
                    await session.execute(
                        select(Group.name).join(User.memberships).where(User.id == user.id)
                    )
                )
                .scalars()
                .all()
            )
            assert list(names) == ["research"]

    async def test_known_users_update_normally_while_off(
        self, session_factory: async_sessionmaker[AsyncSession]
    ) -> None:
        await write_policy(session_factory, auto_provision=False)
        async with session_factory() as session:
            existing = User(issuer="https://idp.test", subject="known-1", email="known@example.org")
            session.add(existing)
            await session.commit()
            existing_id = existing.id

        policy = OIDCPolicy(
            join_mode="invite",
            groups_claim="groups",
        )
        async with session_factory() as session:
            user = await provision_user(
                session,
                issuer="https://idp.test",
                subject="known-1",
                email="known@example.org",
                display_name=None,
                group_names=[],
                settings=OIDCSettings(**LEGACY_GROUPS),
                touch_login=False,
                policy=policy,
            )
            assert user.id == existing_id

    async def test_admin_follows_no_claim_at_all(
        self, session_factory: async_sessionmaker[AsyncSession]
    ) -> None:
        """ADR 0069: the flag is not derived, from anything, any more.

        This test used to assert the opposite — that a mapped admin group
        conferred the flag, with the mapping applied first so the comparison
        spoke local names. The comparison itself is what was removed: an
        administrator is made in the console or by `gateway passwd --admin`,
        and a directory that names a group "admins" has said nothing about
        who administers this deployment.
        """
        policy = OIDCPolicy(
            join_mode="open",
            groups_claim="groups",
            group_mappings={"platform-admins": "admins"},
        )
        async with session_factory() as session:
            user = await provision_user(
                session,
                issuer="https://idp.test",
                subject="admin-1",
                email="admin@example.org",
                display_name=None,
                # Map first, exactly as the callers do: provision receives
                # local names, so a group the claim names lands under its
                # local name — and confers nothing.
                group_names=policy.map_claim_group_names(["platform-admins"]),
                settings=OIDCSettings(**LEGACY_GROUPS),
                touch_login=False,
                policy=policy,
            )
            assert user.is_admin is False
            assert [m.group.name for m in user.memberships] == ["admins"]


# -- the account endpoints ----------------------------------------------------


@pytest_asyncio.fixture
async def admin_session(
    app: Any,
    client: httpx.AsyncClient,
    seeded: Any,
    session_factory: async_sessionmaker[AsyncSession],
) -> AsyncIterator[dict[str, str]]:
    """A session cookie for an administrator: the seeded user, promoted."""
    admin = await make_admin(session_factory=session_factory, seeded=seeded)
    # A delete now refuses outright with no chat configured (ADR 0093 §9.3);
    # this class deletes accounts, so it needs one set, even an unreachable
    # one -- the inline post-commit call failing and queuing for retry is
    # not this test file's concern, only that delete itself is not refused.
    app.state.settings.chat.erasure_url = "http://127.0.0.1:1/internal/erasure"
    app.state.settings.chat.erasure_token = SecretStr("test-erasure-token")
    yield session_cookie(admin.id, app)


class TestUserEndpoints:
    async def test_delete_removes_keys_and_keeps_the_ledger(
        self,
        app: Any,
        client: httpx.AsyncClient,
        seeded: Any,
        admin_session: dict[str, str],
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        # A second account to be the victim: the session admin can never be
        # the target of a delete (self-delete is refused), so the fixture
        # promotes the seeded user and the test mints someone expendable.
        # Inserted directly: accounts arrive through an identity provider now
        # (ADR 0088), and the console no longer creates password accounts.
        async with session_factory() as session:
            victim = User(issuer="https://idp.test", subject="victim", email="victim@example.org")
            session.add(victim)
            await session.commit()
            user_pk = victim.id
        user_id = str(user_pk)
        async with session_factory() as session:
            generated = generate_api_key()
            session.add(
                ApiKey(
                    user_id=user_pk,
                    prefix=generated.prefix,
                    key_hash=generated.key_hash,
                    name="doomed",
                    billing_group_id=seeded.group.id,
                )
            )
            await session.commit()

        deleted = await client.delete(f"/api/admin/users/{user_id}", headers=admin_session)
        assert deleted.status_code == 200

        async with session_factory() as session:
            assert (
                await session.execute(select(User).where(User.id == user_pk))
            ).scalar_one_or_none() is None
            # CASCADE took the keys; the usage rows' user_id becomes null and
            # the rows themselves stay.
            assert (
                await session.execute(select(ApiKey).where(ApiKey.user_id == user_pk))
            ).scalars().all() == []

    async def test_nobody_deletes_their_own_session_account(
        self,
        app: Any,
        client: httpx.AsyncClient,
        seeded: Any,
        admin_session: dict[str, str],
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        # Two admins, so the last-admin guard does not fire first and mask the
        # rule under test: the account you are signed in with is not yours to
        # delete, whoever else exists.
        await client.post(
            "/api/admin/users",
            json={
                "email": "second-admin@example.org",
                "password": "a-long-enough-password",
                "is_admin": True,
            },
            headers=admin_session,
        )
        response = await client.delete(f"/api/admin/users/{seeded.user.id}", headers=admin_session)
        assert response.status_code == 400
        message = response.json()["error"]["message"]
        assert "cannot delete the account you are signed in with" in message

    async def test_deleting_another_admin_succeeds(
        self,
        client: httpx.AsyncClient,
        seeded: Any,
        admin_session: dict[str, str],
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        # Two admins, then one goes. The caller remains — which is precisely
        # why no last-admin guard can ever fire: the request itself proves an
        # active administrator survives the deletion.
        async with session_factory() as session:
            session.add(
                User(
                    issuer="https://idp.test",
                    subject="second-admin",
                    email="second-admin@example.org",
                    is_admin=True,
                )
            )
            await session.commit()
        listing = await client.get("/api/admin/users?q=second-admin", headers=admin_session)
        second_id = listing.json()["items"][0]["id"]
        response = await client.delete(f"/api/admin/users/{second_id}", headers=admin_session)
        assert response.status_code == 200


# -- the policy endpoints -----------------------------------------------------


class TestOidcPolicyEndpoints:
    async def test_get_reports_the_environment_until_a_row_exists(
        self,
        client: httpx.AsyncClient,
        session_factory: async_sessionmaker[AsyncSession],
        admin_session: dict[str, str],
    ) -> None:
        # The seeded deployment carries the migration-0058 self-mapping row;
        # this test's premise is "no row exists", so it clears the table first.
        async with session_factory() as session:
            for row in (await session.execute(select(OIDCPolicyConfig))).scalars().all():
                await session.delete(row)
            await session.commit()
        response = await client.get("/api/admin/oidc/policy", headers=admin_session)
        assert response.status_code == 200
        body = response.json()
        assert body["source"] == "environment"
        assert body["auto_provision"] is True
        assert body["configured"] is None

    async def test_put_records_a_decision_and_it_takes_effect(
        self,
        app: Any,
        client: httpx.AsyncClient,
        seeded: Any,
        admin_session: dict[str, str],
    ) -> None:
        response = await client.put(
            "/api/admin/oidc/policy",
            json={
                "auto_provision": False,
                "unknown_user_policy": "create_inactive",
                "group_mappings": [
                    {"idp": "platform-admins", "local": "admins"},
                    {"idp": "idp-team", "local": "admins"},
                ],
                "reason": "approval required before strangers become users",
            },
            headers=admin_session,
        )
        assert response.status_code == 200
        body = response.json()
        assert body["source"] == "console"
        assert body["unknown_user_policy"] == "create_inactive"
        assert {rule["idp"] for rule in body["group_mappings"]} == {"platform-admins", "idp-team"}
        assert body["configured"]["reason"].startswith("approval required")
        # The answering worker refreshed immediately: a save-then-read on the
        # same origin sees its own decision without waiting a poll.
        assert app.state.oidc_policy.policy.auto_provision is False

        fetched = await client.get("/api/admin/oidc/policy", headers=admin_session)
        assert fetched.json()["auto_provision"] is False

    async def test_put_needs_no_reason(
        self,
        client: httpx.AsyncClient,
        session_factory: async_sessionmaker[AsyncSession],
        seeded: Any,
        admin_session: dict[str, str],
    ) -> None:
        response = await client.put(
            "/api/admin/oidc/policy", json={"auto_provision": False}, headers=admin_session
        )
        assert response.status_code == 200, response.text
        assert response.json()["configured"]["reason"] == ""

        async with session_factory() as session:
            row = (
                (
                    await session.execute(
                        select(OIDCPolicyConfig).order_by(OIDCPolicyConfig.created_at.desc())
                    )
                )
                .scalars()
                .first()
            )
            assert row is not None and row.reason == ""

    async def test_unknown_user_policy_alone_means_approval(
        self, client: httpx.AsyncClient, seeded: Any, admin_session: dict[str, str]
    ) -> None:
        # A row naming only the unknown-user rule folds to approval on its
        # own — the save carries the "provisioning off" decision with it, so
        # there is no dead knob to refuse.
        response = await client.put(
            "/api/admin/oidc/policy",
            json={"unknown_user_policy": "create_inactive"},
            headers=admin_session,
        )
        assert response.status_code == 200, response.text
        assert response.json()["join_mode"] == "approval"

    async def test_unknown_user_policy_with_provisioning_on_is_refused(
        self, client: httpx.AsyncClient, seeded: Any, admin_session: dict[str, str]
    ) -> None:
        response = await client.put(
            "/api/admin/oidc/policy",
            json={"auto_provision": True, "unknown_user_policy": "create_inactive"},
            headers=admin_session,
        )
        assert response.status_code == 400

    async def test_put_unknown_group_name_is_refused(
        self, client: httpx.AsyncClient, seeded: Any, admin_session: dict[str, str]
    ) -> None:
        response = await client.put(
            "/api/admin/oidc/policy",
            json={"join_mode": "group", "required_groups": ["no-such-group"]},
            headers=admin_session,
        )
        assert response.status_code == 400
        assert "no-such-group" in response.json()["error"]["message"]

    async def test_explicit_nulls_clear_back_to_env(
        self,
        client: httpx.AsyncClient,
        seeded: Any,
        admin_session: dict[str, str],
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        """An explicitly sent null stores None — back to env — not []."""
        async with session_factory() as session:
            session.add(Group(name="staff"))
            await session.commit()
        response = await client.put(
            "/api/admin/oidc/policy",
            json={
                "join_mode": "group",
                "required_groups": ["staff"],
                "group_mappings": [{"idp": "idp-team", "local": "staff"}],
            },
            headers=admin_session,
        )
        assert response.status_code == 200, response.text
        # Leaving group mode needs the groups cleared in the same save;
        # clearing them alone would strand a gateless group mode.
        response = await client.put(
            "/api/admin/oidc/policy",
            json={"join_mode": "invite", "required_groups": None},
            headers=admin_session,
        )
        assert response.status_code == 200, response.text
        response = await client.put(
            "/api/admin/oidc/policy", json={"group_mappings": None}, headers=admin_session
        )
        assert response.status_code == 200, response.text
        async with session_factory() as session:
            row = (
                await session.execute(
                    select(OIDCPolicyConfig).order_by(OIDCPolicyConfig.created_at.desc()).limit(1)
                )
            ).scalar_one()
            assert row.join_mode == "invite"
            assert row.required_groups is None
            assert row.group_mappings is None

    async def test_deleting_a_required_group_is_refused(
        self,
        app: Any,
        client: httpx.AsyncClient,
        seeded: Any,
        admin_session: dict[str, str],
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        async with session_factory() as session:
            session.add(Group(name="staff"))
            session.add(OIDCPolicyConfig(join_mode="group", required_groups=["staff"]))
            await session.commit()
            group_id = (
                await session.execute(select(Group.id).where(Group.name == "staff"))
            ).scalar_one()
        await app.state.oidc_policy.refresh_once()
        response = await client.delete(f"/api/admin/groups/{group_id}", headers=admin_session)
        assert response.status_code == 400, response.text
        assert "required groups" in response.json()["error"]["message"]
        assert "staff" in response.json()["error"]["message"]

    async def test_deleting_an_unrequired_group_still_works(
        self,
        app: Any,
        client: httpx.AsyncClient,
        seeded: Any,
        admin_session: dict[str, str],
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        async with session_factory() as session:
            session.add(Group(name="staff"))
            session.add(Group(name="other"))
            session.add(OIDCPolicyConfig(join_mode="group", required_groups=["staff"]))
            await session.commit()
            group_id = (
                await session.execute(select(Group.id).where(Group.name == "other"))
            ).scalar_one()
        await app.state.oidc_policy.refresh_once()
        response = await client.delete(f"/api/admin/groups/{group_id}", headers=admin_session)
        assert response.status_code == 204, response.text

    async def test_put_join_mode_group_with_groups(
        self,
        client: httpx.AsyncClient,
        seeded: Any,
        admin_session: dict[str, str],
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        async with session_factory() as session:
            session.add(Group(name="staff"))
            session.add(Group(name="ops"))
            await session.commit()
        response = await client.put(
            "/api/admin/oidc/policy",
            json={
                "join_mode": "group",
                "required_groups": ["staff", " ops "],
                "group_fallback": "approval",
            },
            headers=admin_session,
        )
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["join_mode"] == "group"
        assert body["required_groups"] == ["staff", "ops"]
        assert body["group_fallback"] == "approval"
        assert body["sources"]["join_mode"] == "console"
        # The legacy view follows the mode: off, and refusing by default the
        # fallback names.
        assert body["auto_provision"] is False

    async def test_put_group_without_groups_is_refused(
        self, client: httpx.AsyncClient, seeded: Any, admin_session: dict[str, str]
    ) -> None:
        response = await client.put(
            "/api/admin/oidc/policy", json={"join_mode": "group"}, headers=admin_session
        )
        assert response.status_code == 400
        assert "required_groups" in response.json()["error"]["message"]

    async def test_put_join_mode_with_legacy_knobs_is_refused(
        self, client: httpx.AsyncClient, seeded: Any, admin_session: dict[str, str]
    ) -> None:
        response = await client.put(
            "/api/admin/oidc/policy",
            json={"join_mode": "invite", "auto_provision": False},
            headers=admin_session,
        )
        assert response.status_code == 400

    async def test_put_groups_outside_group_mode_is_refused(
        self, client: httpx.AsyncClient, seeded: Any, admin_session: dict[str, str]
    ) -> None:
        response = await client.put(
            "/api/admin/oidc/policy",
            json={"join_mode": "invite", "required_groups": ["staff"]},
            headers=admin_session,
        )
        assert response.status_code == 400

    async def test_single_field_saves_carry_the_join_mode(
        self,
        client: httpx.AsyncClient,
        seeded: Any,
        admin_session: dict[str, str],
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        """One row per save must not reset the answers the other saves gave.

        The console saves one field at a time, and each save stores a whole
        row of nulls-except-what-was-sent. Without carry-forward, the second
        save folds the join answer back to the environment and fails open.
        """
        async with session_factory() as session:
            session.add(Group(name="staff"))
            await session.commit()
        response = await client.put(
            "/api/admin/oidc/policy",
            json={"join_mode": "group", "required_groups": ["staff"]},
            headers=admin_session,
        )
        assert response.status_code == 200, response.text

        response = await client.put(
            "/api/admin/oidc/policy", json={"group_fallback": "approval"}, headers=admin_session
        )
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["join_mode"] == "group"
        assert body["required_groups"] == ["staff"]
        assert body["group_fallback"] == "approval"

        response = await client.put(
            "/api/admin/oidc/policy",
            json={"group_mappings": [{"idp": "idp-team", "local": "staff"}]},
            headers=admin_session,
        )
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["join_mode"] == "group"
        assert body["required_groups"] == ["staff"]

        response = await client.put(
            "/api/admin/oidc/policy", json={"groups_claim": "roles"}, headers=admin_session
        )
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["join_mode"] == "group"
        assert body["required_groups"] == ["staff"]
        assert body["groups_claim"] == "roles"

    async def test_legacy_pair_across_two_saves_gives_approval(
        self, client: httpx.AsyncClient, seeded: Any, admin_session: dict[str, str]
    ) -> None:
        response = await client.put(
            "/api/admin/oidc/policy", json={"auto_provision": False}, headers=admin_session
        )
        assert response.status_code == 200, response.text
        assert response.json()["join_mode"] == "invite"
        response = await client.put(
            "/api/admin/oidc/policy",
            json={"unknown_user_policy": "create_inactive"},
            headers=admin_session,
        )
        assert response.status_code == 200, response.text
        assert response.json()["join_mode"] == "approval"

    async def test_join_mode_save_clears_carried_legacy_columns(
        self,
        client: httpx.AsyncClient,
        seeded: Any,
        admin_session: dict[str, str],
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        """A row must never mix the two spellings: saving a join mode drops
        the carried legacy columns, so a later legacy-column edit cannot
        silently revive beside it."""
        response = await client.put(
            "/api/admin/oidc/policy", json={"auto_provision": False}, headers=admin_session
        )
        assert response.status_code == 200, response.text
        response = await client.put(
            "/api/admin/oidc/policy", json={"join_mode": "open"}, headers=admin_session
        )
        assert response.status_code == 200, response.text
        assert response.json()["join_mode"] == "open"
        async with session_factory() as session:
            row = (
                await session.execute(
                    select(OIDCPolicyConfig).order_by(OIDCPolicyConfig.created_at.desc()).limit(1)
                )
            ).scalar_one()
            assert row.join_mode == "open"
            assert row.auto_provision is None
            assert row.unknown_user_policy is None

    async def test_an_idp_group_cannot_be_mapped_twice(
        self, client: httpx.AsyncClient, seeded: Any, admin_session: dict[str, str]
    ) -> None:
        response = await client.put(
            "/api/admin/oidc/policy",
            json={
                "group_mappings": [
                    {"idp": "same", "local": "one"},
                    {"idp": "same", "local": "two"},
                ]
            },
            headers=admin_session,
        )
        assert response.status_code == 400

    async def test_non_admin_is_403(self, app: Any, client: httpx.AsyncClient, seeded: Any) -> None:
        # The seeded user is a non-admin here: identity policy is governance,
        # and a non-admin must not even be able to read it.
        response = await client.get(
            "/api/admin/oidc/policy", headers=session_cookie(seeded.user.id, app)
        )
        assert response.status_code == 403

    async def test_policy_preview_counts_refusals(
        self,
        client: httpx.AsyncClient,
        seeded: Any,
        admin_session: dict[str, str],
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        """The console confirms "N people lose access" before saving group mode."""
        async with session_factory() as session:
            session.add(
                User(
                    issuer="https://idp.test",
                    subject="plain-1",
                    email="plain@example.org",
                    email_normalized="plain@example.org",
                )
            )
            await session.commit()
        # The seeded administrator is exempt; the plain user holds nothing.
        # Repeated params, as the console sends them (list[str] Query).
        response = await client.get(
            "/api/admin/oidc/policy/preview",
            params=[("join_mode", "group"), ("required_groups", "research")],
            headers=admin_session,
        )
        assert response.status_code == 200, response.text
        assert response.json() == {
            "active_users": 2,
            "would_be_refused": 1,
            "admins_exempt": 1,
        }
        assert (
            await client.get(
                "/api/admin/oidc/policy/preview",
                params=[("join_mode", "invite")],
                headers=admin_session,
            )
        ).status_code == 400
        assert (
            await client.get(
                "/api/admin/oidc/policy/preview",
                params=[("join_mode", "group")],
                headers=admin_session,
            )
        ).status_code == 400
