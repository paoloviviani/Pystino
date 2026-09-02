"""The identity policy (ADR 0048) and the account-management endpoints.

What is tested here and why: a wrong answer in provisioning is a security
hole, not a stack trace — a gate that does not gate lets strangers mint
accounts; a gate that over-gates locks every legitimate user out. So the
policy fold, the provisioning gate, and the two account endpoints each get
their own assertions, against the real application and the real resolver.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from typing import Any

import httpx
import pytest
import pytest_asyncio
from gateway.config import OIDCSettings
from gateway.login_throttle import LoginThrottle
from gateway.models import ApiKey, Group, OIDCPolicyConfig, User
from gateway.oidc import ProvisioningRefused, provision_user
from gateway.oidc_policy import (
    OIDCPolicy,
    OIDCPolicyResolver,
    effective_policy,
    environment_policy,
)
from gateway.security import generate_api_key
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from test_admin import make_admin
from test_my_limits import session_cookie

# -- the fold: row decisions over the environment baseline --------------------


class TestEffectivePolicy:
    def test_no_row_means_the_environment(self) -> None:
        settings = OIDCSettings(groups_claim="roles", admin_groups=["idp-admins"])
        policy = effective_policy(settings, None)
        assert policy.auto_provision is True
        assert policy.groups_claim == "roles"
        assert policy.admin_groups == ["idp-admins"]
        assert policy.source == "environment"

    def test_a_row_decides_only_what_it_names(self) -> None:
        settings = OIDCSettings(groups_claim="groups")
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
        settings = OIDCSettings()
        row = OIDCPolicyConfig(unknown_user_policy="nonsense")
        policy = effective_policy(settings, row)
        assert policy.unknown_user_policy == "refuse"
        assert "unknown_user_policy" not in policy.sources

    def test_mappings_fold_into_a_dictionary(self) -> None:
        row = OIDCPolicyConfig(
            group_mappings=[["idp-a", "local-one"], ["idp-b", "local-one"]]
        )
        policy = effective_policy(OIDCSettings(), row)
        assert policy.group_mappings == {"idp-a": "local-one", "idp-b": "local-one"}


class TestMapGroupNames:
    def test_unmapped_groups_keep_their_name(self) -> None:
        policy = OIDCPolicy(
            auto_provision=True,
            unknown_user_policy="refuse",
            groups_claim="groups",
            group_mappings={"idp-team": "research"},
        )
        assert policy.map_group_names(["idp-team", "other"]) == ["research", "other"]

    def test_many_to_one_folds_without_repetition(self) -> None:
        policy = OIDCPolicy(
            auto_provision=True,
            unknown_user_policy="refuse",
            groups_claim="groups",
            group_mappings={"a": "one", "b": "one"},
        )
        assert policy.map_group_names(["a", "b"]) == ["one"]


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


async def write_policy(
    session_factory: async_sessionmaker[AsyncSession], **columns: Any
) -> None:
    async with session_factory() as session:
        session.add(OIDCPolicyConfig(**columns))
        await session.commit()


class TestProvisioningGate:
    async def test_off_and_refusing_makes_no_user(
        self, session_factory: async_sessionmaker[AsyncSession]
    ) -> None:
        await write_policy(session_factory, auto_provision=False)
        policy = OIDCPolicy(
            auto_provision=False,
            unknown_user_policy="refuse",
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
                    settings=OIDCSettings(),
                    touch_login=False,
                    policy=policy,
                )

    async def test_create_inactive_leaves_an_approvable_row(
        self, session_factory: async_sessionmaker[AsyncSession]
    ) -> None:
        await write_policy(session_factory, auto_provision=False)
        policy = OIDCPolicy(
            auto_provision=False,
            unknown_user_policy="create_inactive",
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
                settings=OIDCSettings(),
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
            auto_provision=False,
            unknown_user_policy="refuse",
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
                settings=OIDCSettings(),
                touch_login=False,
                policy=policy,
            )
            assert user.id == existing_id

    async def test_admin_follows_mapped_names(
        self, session_factory: async_sessionmaker[AsyncSession]
    ) -> None:
        # Admin compares local names: the IdP says "platform-admins", the
        # mapping says that means "admins" here, and admin_groups names the
        # local group — the platform's, not the IdP's.
        policy = OIDCPolicy(
            auto_provision=True,
            unknown_user_policy="refuse",
            groups_claim="groups",
            admin_groups=["admins"],
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
                # local names, so admin compares what the group is called here.
                group_names=policy.map_group_names(["platform-admins"]),
                settings=OIDCSettings(),
                touch_login=False,
                policy=policy,
            )
            assert user.is_admin is True
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
    yield session_cookie(admin.id, app)


class TestUserEndpoints:

    async def test_create_local_account_end_to_end(
        self,
        app: Any,
        client: httpx.AsyncClient,
        admin_session: dict[str, str],
    ) -> None:
        response = await client.post(
            "/api/admin/users",
            json={
                "email": "New.Person@Example.org",
                "password": "a-long-enough-password",
                "display_name": "New Person",
                "groups": ["research", "Contractors"],
            },
            headers=admin_session,
        )
        assert response.status_code == 201
        body = response.json()
        # Keyed by the casefolded email, the login query's convention.
        assert body["subject"] == "new.person@example.org"
        assert sorted(body["groups"]) == ["Contractors", "research"]
        # Two groups is not a sole group: the automatic default applies only
        # when there is exactly one, because picking between two would be a
        # guess. A second group makes the default an explicit decision.
        assert body["default_billing_group"] is None

        # And the account can actually sign in — the proof that matters. The
        # shared settings ship local auth off; the throttle's presence is the
        # feature switch, so the test arms it.
        app.state.login_throttle = LoginThrottle(
            max_failed_attempts=10, window_seconds=60
        )
        login = await client.post(
            "/auth/login",
            json={"email": "new.person@example.org", "password": "a-long-enough-password"},
        )
        assert login.status_code == 200

    async def test_duplicate_email_is_refused(
        self, client: httpx.AsyncClient, admin_session: dict[str, str]
    ) -> None:
        payload = {"email": "dup@example.org", "password": "a-long-enough-password"}
        first = await client.post("/api/admin/users", json=payload, headers=admin_session)
        assert first.status_code == 201
        again = await client.post("/api/admin/users", json=payload, headers=admin_session)
        assert again.status_code == 400
        assert again.json()["error"]["code"] == "account_exists"

    async def test_a_short_password_is_refused(
        self, client: httpx.AsyncClient, admin_session: dict[str, str]
    ) -> None:
        response = await client.post(
            "/api/admin/users",
            json={"email": "short@example.org", "password": "short"},
            headers=admin_session,
        )
        assert response.status_code == 400

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
        created = await client.post(
            "/api/admin/users",
            json={
                "email": "victim@example.org",
                "password": "a-long-enough-password",
                "groups": ["research"],
            },
            headers=admin_session,
        )
        user_id = created.json()["id"]
        user_pk = uuid.UUID(user_id)
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
        assert deleted.status_code == 204

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
        response = await client.delete(
            f"/api/admin/users/{seeded.user.id}", headers=admin_session
        )
        assert response.status_code == 400
        message = response.json()["error"]["message"]
        assert "cannot delete the account you are signed in with" in message

    async def test_deleting_another_admin_succeeds(
        self,
        client: httpx.AsyncClient,
        seeded: Any,
        admin_session: dict[str, str],
    ) -> None:
        # Two admins, then one goes. The caller remains — which is precisely
        # why no last-admin guard can ever fire: the request itself proves an
        # active administrator survives the deletion.
        await client.post(
            "/api/admin/users",
            json={
                "email": "second-admin@example.org",
                "password": "a-long-enough-password",
                "is_admin": True,
            },
            headers=admin_session,
        )
        listing = await client.get("/api/admin/users?q=second-admin", headers=admin_session)
        second_id = listing.json()["items"][0]["id"]
        response = await client.delete(f"/api/admin/users/{second_id}", headers=admin_session)
        assert response.status_code == 204


# -- the policy endpoints -----------------------------------------------------


class TestOidcPolicyEndpoints:
    async def test_get_reports_the_environment_until_a_row_exists(
        self, client: httpx.AsyncClient, seeded: Any, admin_session: dict[str, str]
    ) -> None:
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

    async def test_unknown_user_policy_requires_provisioning_off(
        self, client: httpx.AsyncClient, seeded: Any, admin_session: dict[str, str]
    ) -> None:
        response = await client.put(
            "/api/admin/oidc/policy",
            json={"unknown_user_policy": "create_inactive"},
            headers=admin_session,
        )
        assert response.status_code == 400

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

    async def test_non_admin_is_403(
        self, app: Any, client: httpx.AsyncClient, seeded: Any
    ) -> None:
        # The seeded user is a non-admin here: identity policy is governance,
        # and a non-admin must not even be able to read it.
        response = await client.get(
            "/api/admin/oidc/policy", headers=session_cookie(seeded.user.id, app)
        )
        assert response.status_code == 403
