"""The join gate (ADR 0095): who may sign in, and who may call `/v1`.

A wrong answer here is a security hole, not a stack trace — a gate that does
not gate lets strangers mint accounts, and one that over-gates locks every
legitimate user out. So each mode's stranger answer, the exemption, and the
group check on all three paths (sign-in, bearer, console session) get their
own assertions.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

import httpx
import pytest
import pytest_asyncio
from conftest import LEGACY_GROUPS, Seeded
from gateway.config import OIDCSettings, Settings
from gateway.errors import OutsideAccessGroupsError
from gateway.identity_registry import record_from_env
from gateway.join_gate import (
    exempt,
    holds_required_group,
    provider_label,
    refusal_text,
    stranger_passes,
)
from gateway.models import (
    Group,
    IdentityEvent,
    IdentityEventAction,
    Membership,
    MembershipSource,
    OIDCPolicyConfig,
    User,
)
from gateway.oidc import ProvisioningRefused, sign_in, sync_user_from_claims
from gateway.oidc_policy import OIDCPolicy
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from test_my_limits import session_cookie

ISS = "https://idp.test"


def _settings(**overrides: Any) -> Settings:
    return Settings(
        oidc=OIDCSettings(enabled=True, issuer=ISS, client_id="c", **LEGACY_GROUPS, **overrides)
    )


def _record(settings: Settings) -> Any:
    record = record_from_env(settings)
    assert record is not None
    return record


def _policy(mode: str, **overrides: Any) -> OIDCPolicy:
    return OIDCPolicy(join_mode=mode, **overrides)


def _claims(subject: str, groups: list[str], email: str | None = None) -> dict[str, Any]:
    claims: dict[str, Any] = {"iss": ISS, "sub": subject, "groups": groups}
    if email is not None:
        claims["email"] = email
        claims["email_verified"] = True
    return claims


async def _sign_in(
    session: AsyncSession,
    subject: str,
    groups: list[str],
    *,
    settings: Settings | None = None,
    policy: OIDCPolicy | None = None,
    email: str | None = None,
) -> Any:
    settings = settings or _settings()
    result = await sign_in(
        session,
        _record(settings),
        issuer=ISS,
        subject=subject,
        claims=_claims(subject, groups, email or f"{subject}@example.org"),
        settings=settings,
        policy=policy,
    )
    await session.commit()
    return result


async def _row(session: AsyncSession, subject: str) -> User | None:
    return (
        await session.execute(select(User).where(User.issuer == ISS, User.subject == subject))
    ).scalar_one_or_none()


# -- the pure gate ------------------------------------------------------------


class TestJoinGatePure:
    def test_holds_required_group_reads_effective_memberships(self) -> None:
        required = frozenset({"staff"})
        assert holds_required_group(_member("staff", active=True), required) is True
        # Any source counts: a console grant is a real membership.
        assert (
            holds_required_group(_member("staff", active=True, source="manual"), required) is True
        )
        assert holds_required_group(_member("other", active=True), required) is False
        # An inactive group confers nothing.
        assert holds_required_group(_member("staff", active=False), required) is False

    def test_stranger_passes_on_mapped_names(self) -> None:
        assert stranger_passes(["staff"], frozenset({"staff"})) is True
        assert stranger_passes(["other"], frozenset({"staff"})) is False

    def test_exempt_admins_always(self) -> None:
        admin = User(issuer=ISS, subject="a", is_admin=True)
        assert exempt(admin, None, OIDCSettings(**LEGACY_GROUPS)) is True

    def test_exempt_env_email_rule_needs_the_literal_true(self) -> None:
        settings = OIDCSettings(**LEGACY_GROUPS, admin_emails="boss@example.org")
        good = {"email": "boss@example.org", "email_verified": True}
        assert exempt(None, good, settings) is True
        assert exempt(None, {**good, "email_verified": "true"}, settings) is False
        # ... and the bearer path does not consult it at all.
        assert exempt(None, good, settings, email_rule=False) is False

    def test_exempt_claim_rule(self) -> None:
        settings = OIDCSettings(**LEGACY_GROUPS, admin_claim="groups", admin_claim_values="ops")
        assert exempt(None, {"groups": ["ops"]}, settings) is True
        assert exempt(None, {"groups": ["users"]}, settings) is False

    def test_refusal_texts_name_no_groups(self) -> None:
        assert "invitation-only" in refusal_text(
            "invite", email="a@example.org", provider="Keycloak"
        )
        assert "a@example.org" in refusal_text("invite", email="a@example.org", provider="Keycloak")
        text = refusal_text("group", email=None, provider="Keycloak")
        assert "Keycloak" in text and "staff" not in text
        assert "no longer" in refusal_text("group_lost", email=None, provider="x")
        assert "waiting" in refusal_text("approval", email=None, provider="x")

    def test_provider_label_defaults_to_identity_provider(self) -> None:
        assert provider_label("keycloak") == "Keycloak"
        assert provider_label("generic") == "identity provider"
        assert provider_label(None) == "identity provider"


def _member(group_name: str, *, active: bool, source: str = "oidc") -> User:
    group = Group(name=group_name, is_active=active)
    user = User(issuer=ISS, subject="s")
    user.memberships.append(
        Membership(
            group=group,
            source=MembershipSource.MANUAL if source == "manual" else MembershipSource.OIDC,
        )
    )
    return user


# -- each mode's stranger answer, through the sign-in door --------------------


class TestStrangerModes:
    async def test_open_creates_active(self, session: AsyncSession) -> None:
        result = await _sign_in(session, "new-1", [], policy=_policy("open"))
        assert result.user.is_active is True

    async def test_approval_creates_inactive_with_its_provenance(
        self, session: AsyncSession
    ) -> None:
        result = await _sign_in(
            session, "new-2", [], policy=_policy("approval"), email="new-2@example.org"
        )
        assert result.user.is_active is False
        assert result.user.deactivated_by == "approval"
        # Today's create_inactive branch forgets this; the approval queue
        # matches people by address, so the gate must not.
        assert result.user.email_normalized == "new-2@example.org"

    async def test_invite_refuses_and_writes_no_row(self, session: AsyncSession) -> None:
        with pytest.raises(ProvisioningRefused, match="invitation-only"):
            await _sign_in(session, "new-3", [], policy=_policy("invite"))
        assert await _row(session, "new-3") is None

    async def test_invite_refusal_is_audited(self, session: AsyncSession) -> None:
        with pytest.raises(ProvisioningRefused):
            await _sign_in(session, "new-4", [], policy=_policy("invite"))
        event = (
            await session.execute(
                select(IdentityEvent).where(
                    IdentityEvent.action == IdentityEventAction.ACCESS_REFUSED
                )
            )
        ).scalar_one_or_none()
        assert event is not None
        assert event.detail == {"reason": "invite"}

    async def test_group_passes_on_a_required_group(self, session: AsyncSession) -> None:
        result = await _sign_in(
            session, "new-5", ["staff"], policy=_policy("group", required_groups=("staff",))
        )
        assert result.user.is_active is True

    async def test_group_refuses_without_one_and_names_none(self, session: AsyncSession) -> None:
        with pytest.raises(ProvisioningRefused, match="isn't in a group"):
            await _sign_in(
                session, "new-6", ["other"], policy=_policy("group", required_groups=("staff",))
            )
        assert await _row(session, "new-6") is None

    async def test_group_fallback_approval_holds_for_approval(self, session: AsyncSession) -> None:
        result = await _sign_in(
            session,
            "new-7",
            ["other"],
            policy=_policy("group", required_groups=("staff",), group_fallback="approval"),
        )
        assert result.user.is_active is False
        assert result.user.deactivated_by == "approval"

    async def test_env_admin_email_exempts_a_stranger(self, session: AsyncSession) -> None:
        settings = _settings(admin_emails="boss@example.org")
        for mode in ("invite", "group"):
            result = await _sign_in(
                session,
                f"boss-{mode}",
                [],
                settings=settings,
                policy=_policy(mode, required_groups=("staff",)),
                email="boss@example.org",
            )
            assert result.user.is_admin is True
            assert result.user.is_active is True


# -- group mode for people already here ---------------------------------------


async def _member_user(session: AsyncSession, subject: str, group_names: list[str]) -> User:
    user = User(
        issuer=ISS,
        subject=subject,
        email=f"{subject}@example.org",
        email_normalized=f"{subject}@example.org",
    )
    session.add(user)
    await session.flush()
    for name in group_names:
        group = (
            await session.execute(select(Group).where(Group.name == name))
        ).scalar_one_or_none()
        if group is None:
            group = Group(name=name)
            session.add(group)
            await session.flush()
        session.add(Membership(user_id=user.id, group_id=group.id, source=MembershipSource.OIDC))
    await session.commit()
    return user


class TestGroupModeExistingUsers:
    async def test_sign_in_refused_after_losing_the_group(self, session: AsyncSession) -> None:
        policy = _policy("group", required_groups=("staff",))
        await _sign_in(session, "s-1", ["staff"], policy=policy)
        with pytest.raises(ProvisioningRefused, match="no longer"):
            await _sign_in(session, "s-1", [], policy=policy)

    async def test_v1_refused_with_403(self, session: AsyncSession) -> None:
        policy = _policy("group", required_groups=("staff",))
        await _member_user(session, "s-2", ["staff"])
        settings = OIDCSettings(**LEGACY_GROUPS)
        with pytest.raises(OutsideAccessGroupsError, match="no longer"):
            await sync_user_from_claims(
                session,
                claims=_claims("s-2", []),
                settings=settings,
                policy=policy,
            )

    async def test_v1_early_return_refuses_without_divergence(self, session: AsyncSession) -> None:
        """No token change, so no reconciliation — the gate still fires."""
        policy = _policy("group", required_groups=("staff",))
        await _member_user(session, "s-3", ["other"])
        settings = OIDCSettings(**LEGACY_GROUPS)
        with pytest.raises(OutsideAccessGroupsError, match="no longer"):
            await sync_user_from_claims(
                session,
                claims=_claims("s-3", ["other"]),
                settings=settings,
                policy=policy,
            )

    async def test_regained_group_restores_access(self, session: AsyncSession) -> None:
        policy = _policy("group", required_groups=("staff",))
        await _sign_in(session, "s-4", ["staff"], policy=policy)
        with pytest.raises(ProvisioningRefused):
            await _sign_in(session, "s-4", [], policy=policy)
        result = await _sign_in(session, "s-4", ["staff"], policy=policy)
        assert result.user.is_active is True

    async def test_admin_exempt(self, session: AsyncSession) -> None:
        policy = _policy("group", required_groups=("staff",))
        user = await _member_user(session, "s-5", [])
        user.is_admin = True
        await session.commit()
        result = await _sign_in(session, "s-5", [], policy=policy)
        assert result.user.id == user.id

    async def test_console_granted_membership_passes(self, session: AsyncSession) -> None:
        policy = _policy("group", required_groups=("staff",))
        user = await _member_user(session, "s-6", [])
        group = (
            await session.execute(select(Group).where(Group.name == "staff"))
        ).scalar_one_or_none()
        if group is None:
            group = Group(name="staff")
            session.add(group)
            await session.flush()
        session.add(Membership(user_id=user.id, group_id=group.id, source=MembershipSource.MANUAL))
        await session.commit()
        result = await _sign_in(session, "s-6", [], policy=policy)
        assert result.user.id == user.id

    async def test_inactive_group_does_not_count(self, session: AsyncSession) -> None:
        policy = _policy("group", required_groups=("staff",))
        user = await _member_user(session, "s-7", [])
        group = Group(name="staff", is_active=False)
        session.add(group)
        await session.flush()
        session.add(Membership(user_id=user.id, group_id=group.id, source=MembershipSource.OIDC))
        await session.commit()
        with pytest.raises(ProvisioningRefused):
            await _sign_in(session, "s-7", [], policy=policy)


# -- the /v1 doors answer 403, never 401 ---------------------------------------


class TestV1DoorsAnswer403:
    async def test_bearer_outside_groups_is_403_with_code(
        self,
        bearer_app: Any,
        client: httpx.AsyncClient,
        seeded: Seeded,
        signing_key: Any,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        """The token verifies — the caller is authenticated — so a 401 would
        lie, and Cerea would revoke the machine for a loss that regaining the
        group restores."""
        from test_bearer_auth import auth, make_token

        async with session_factory() as session:
            session.add(Group(name="staff"))
            session.add(OIDCPolicyConfig(join_mode="group", required_groups=["staff"]))
            await session.commit()
        await bearer_app.state.oidc_policy.refresh_once()

        token = make_token(signing_key)
        response = await client.get("/v1/models", headers=auth(token))
        assert response.status_code == 403, response.text
        assert response.json()["error"]["code"] == "outside_access_groups"

    async def test_key_outside_groups_is_403_with_code(
        self,
        app: Any,
        client: httpx.AsyncClient,
        seeded: Seeded,
        session_factory: async_sessionmaker[AsyncSession],
        group_policy: None,
    ) -> None:
        response = await client.get("/v1/models", headers=seeded.auth)
        assert response.status_code == 403, response.text
        assert response.json()["error"]["code"] == "outside_access_groups"

    async def test_key_with_a_required_group_passes(
        self,
        app: Any,
        client: httpx.AsyncClient,
        seeded: Seeded,
        session_factory: async_sessionmaker[AsyncSession],
        group_policy: None,
    ) -> None:
        async with session_factory() as session:
            group = (await session.execute(select(Group).where(Group.name == "staff"))).scalar_one()
            session.add(
                Membership(
                    user_id=seeded.user.id,
                    group_id=group.id,
                    source=MembershipSource.MANUAL,
                )
            )
            await session.commit()
        response = await client.get("/v1/models", headers=seeded.auth)
        assert response.status_code == 200, response.text


# -- the console session door ---------------------------------------------------


@pytest_asyncio.fixture
async def group_policy(
    app: Any, session_factory: async_sessionmaker[AsyncSession]
) -> AsyncIterator[None]:
    async with session_factory() as session:
        session.add(Group(name="staff"))
        session.add(OIDCPolicyConfig(join_mode="group", required_groups=["staff"]))
        await session.commit()
    await app.state.oidc_policy.refresh_once()
    yield
    async with session_factory() as session:
        for row in (await session.execute(select(OIDCPolicyConfig))).scalars().all():
            await session.delete(row)
        await session.commit()
    await app.state.oidc_policy.refresh_once()


class TestConsoleSessionGate:
    async def test_session_without_a_required_group_is_refused(
        self,
        app: Any,
        client: httpx.AsyncClient,
        seeded: Seeded,
        session_factory: async_sessionmaker[AsyncSession],
        group_policy: None,
    ) -> None:
        cookie = session_cookie(seeded.user.id, app)
        response = await client.get("/api/me/limits", headers=cookie)
        assert response.status_code == 401, response.text

    async def test_session_with_a_required_group_passes(
        self,
        app: Any,
        client: httpx.AsyncClient,
        seeded: Seeded,
        session_factory: async_sessionmaker[AsyncSession],
        group_policy: None,
    ) -> None:
        async with session_factory() as session:
            group = (await session.execute(select(Group).where(Group.name == "staff"))).scalar_one()
            session.add(
                Membership(
                    user_id=seeded.user.id,
                    group_id=group.id,
                    source=MembershipSource.MANUAL,
                )
            )
            await session.commit()
        cookie = session_cookie(seeded.user.id, app)
        response = await client.get("/api/me/limits", headers=cookie)
        assert response.status_code == 200, response.text
