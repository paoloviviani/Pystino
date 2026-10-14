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
        oidc=OIDCSettings(enabled=True, issuer=ISS, client_id="c", **{**LEGACY_GROUPS, **overrides})
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
        # Without vetting (the directory-source case) every membership counts.
        assert holds_required_group(_member("staff", active=True), required) is True
        assert holds_required_group(_member("other", active=True), required) is False
        # An inactive group confers nothing.
        assert holds_required_group(_member("staff", active=False), required) is False

    def test_stranger_passes_on_mapped_names(self) -> None:
        # The gate itself is name-against-name: the vouching happened at the
        # mapping step, which already dropped what the IdP may not resolve.
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

    async def test_the_target_name_of_a_mapping_is_not_its_own_vouch(
        self, session: AsyncSession
    ) -> None:
        """The hole the first vetting missed: with `acme/staff` mapped to
        `staff`, the mapping *target* claimed directly from the IdP is still
        user-writable — a GitLab top-level group named `staff` — and must not
        admit. The vouch is on the IdP name: a mapping key resolves to its
        target; the target's own name resolves to nothing."""
        policy = _policy(
            "group", required_groups=("staff",), group_mappings={"acme/staff": "staff"}
        )
        with pytest.raises(ProvisioningRefused, match="isn't in a group"):
            await _sign_in(session, "spoof-1", ["staff"], policy=policy)
        assert await _row(session, "spoof-1") is None

    async def test_the_mapped_name_is_admitted(self, session: AsyncSession) -> None:
        policy = _policy(
            "group", required_groups=("staff",), group_mappings={"acme/staff": "staff"}
        )
        result = await _sign_in(session, "mapped-1", ["acme/staff"], policy=policy)
        assert result.user.is_active is True
        assert "staff" in [m.group.name for m in result.user.memberships]

    async def test_an_allowlisted_name_confers_nothing_by_itself(
        self, session: AsyncSession
    ) -> None:
        """The allowlist is a pre-filter on which claimed names are considered
        at all (extract_groups); it vouches for nothing. Admission needs a
        mapping — an allowlisted-but-unmapped required name is refused like
        any other unmapped claim."""
        settings = _settings(group_allowlist=["staff"])
        policy = _policy("group", required_groups=("staff",))
        with pytest.raises(ProvisioningRefused, match="isn't in a group"):
            await _sign_in(
                session,
                "allowlisted-1",
                ["staff"],
                settings=settings,
                policy=policy,
            )
        assert await _row(session, "allowlisted-1") is None

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


async def _member_user(
    session: AsyncSession,
    subject: str,
    group_names: list[str],
    *,
    source: Any = MembershipSource.MANUAL,
) -> User:
    """A person whose memberships were granted here (console, or a directory
    push — both count toward the gate as before). The claim-sourced shape is
    the one test that needs it passes `source=MembershipSource.OIDC`."""
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
        session.add(Membership(user_id=user.id, group_id=group.id, source=source))
    await session.commit()
    return user


async def user_memberships(session: AsyncSession, subject: str) -> list[Membership]:
    user = (await session.execute(select(User).where(User.subject == subject))).scalar_one()
    return list(user.memberships)


class TestGroupModeExistingUsers:
    async def test_sign_in_refused_after_losing_the_group(self, session: AsyncSession) -> None:
        # The mapping is the operator's vouch for the IdP name (ADR 0095
        # review): without it the claim itself would not admit.
        policy = _policy("group", required_groups=("staff",), group_mappings={"staff": "staff"})
        await _sign_in(session, "s-1", ["staff"], policy=policy)
        with pytest.raises(ProvisioningRefused, match="no longer"):
            await _sign_in(session, "s-1", [], policy=policy)

    async def test_v1_refused_with_403(self, session: AsyncSession) -> None:
        """A vetted membership the directory then takes away: the claim drops
        `staff`, the reconcile removes it, and the gate refuses with 403."""
        policy = _policy("group", required_groups=("staff",), group_mappings={"staff": "staff"})
        await _member_user(session, "s-2", ["staff"], source=MembershipSource.OIDC)
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
        policy = _policy("group", required_groups=("staff",), group_mappings={"staff": "staff"})
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

    async def test_an_existing_user_gains_no_membership_from_the_spoofed_name(
        self, session: AsyncSession
    ) -> None:
        policy = _policy(
            "group", required_groups=("staff",), group_mappings={"acme/staff": "staff"}
        )
        await _member_user(session, "e-1", [])
        with pytest.raises(ProvisioningRefused, match="no longer"):
            await _sign_in(session, "e-1", ["staff"], policy=policy)
        names = [m.group.name for m in await user_memberships(session, "e-1")]
        assert "staff" not in names

    async def test_a_directory_sourced_membership_admits_without_a_mapping(
        self, session: AsyncSession
    ) -> None:
        """SCIM/directory memberships count as before: with
        `group_source="directory"` no membership was ever created from a
        claim, so there is nothing to vouch — the oidc membership counts."""
        policy = _policy("group", required_groups=("staff",))
        await _member_user(session, "s-dir", ["staff"], source=MembershipSource.OIDC)
        settings = OIDCSettings(**LEGACY_GROUPS)
        user = await sync_user_from_claims(
            session,
            claims=_claims("s-dir", ["staff"]),
            settings=settings,
            policy=policy,
            group_source="directory",
        )
        assert user.is_active is True

    async def test_a_non_required_unmapped_name_resolves_to_nothing(
        self, session: AsyncSession
    ) -> None:
        """The drop is the rule, not a required-names special case: an
        unmapped name resolves to nothing whatever the gate asks for, and is
        recorded as a seen name for the console to map. (Under `auto` the
        name would self-map instead — that mode creates the mapping itself.)"""
        policy = _policy("group", required_groups=("staff",))
        await _member_user(session, "s-8", ["staff"])  # manual: admitted
        result = await _sign_in(
            session,
            "s-8",
            ["research"],
            settings=_settings(group_import="manual"),
            policy=policy,
        )
        names = [m.group.name for m in result.user.memberships]
        assert "research" not in names and "staff" in names
        assert "research" in (result.user.unresolved_group_names or [])

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
            # ADR 0096 (amended): the capability is a flag now, so the key's
            # owner needs it to use the key at all — this test is about the
            # join gate, so the flag is granted outright.
            group.api_keys_allowed = True
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


@pytest_asyncio.fixture
async def acme_staff_policy(
    app: Any, session_factory: async_sessionmaker[AsyncSession]
) -> AsyncIterator[None]:
    """Group mode with the operator's mapping `acme/staff -> staff`: the
    mapped IdP name vouches; the target's own name does not."""
    async with session_factory() as session:
        # ADR 0096 (amended): the capability is a flag now. This fixture is
        # about the join gate's refusal text, so the group carries the flag,
        # keeping the key usable once admission lands.
        session.add(Group(name="staff", api_keys_allowed=True))
        session.add(
            OIDCPolicyConfig(
                join_mode="group",
                required_groups=["staff"],
                group_mappings=[["acme/staff", "staff"]],
            )
        )
        await session.commit()
    await app.state.oidc_policy.refresh_once()
    yield
    async with session_factory() as session:
        for row in (await session.execute(select(OIDCPolicyConfig))).scalars().all():
            await session.delete(row)
        await session.commit()
    await app.state.oidc_policy.refresh_once()


def _mapped_policy() -> OIDCPolicy:
    return _policy("group", required_groups=("staff",), group_mappings={"acme/staff": "staff"})


class TestTheSpoofedNameNeverAdmits:
    """ADR 0095 review, round 2: with `acme/staff -> staff` mapped, a claim
    naming `staff` directly is the attacker's, not the operator's — it is
    dropped at the mapping step, so no door admits on it and no membership
    is created from it."""

    async def test_the_bearer_path_refuses_and_creates_no_membership(
        self,
        bearer_app: Any,
        client: httpx.AsyncClient,
        seeded: Seeded,
        signing_key: Any,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        from test_bearer_auth import auth, make_token

        async with session_factory() as session:
            # The local group the operator vouched for: an imported name
            # resolves into it, a fresh sighting does not smuggle it in.
            session.add(Group(name="staff"))
            session.add(
                OIDCPolicyConfig(
                    join_mode="group",
                    required_groups=["staff"],
                    group_mappings=[["acme/staff", "staff"]],
                )
            )
            await session.commit()
        await bearer_app.state.oidc_policy.refresh_once()

        # /v1/me, not /v1/models: the point is the gate, not billing (a
        # reconcile that drops the billed group is a product-correct refusal
        # of its own, and would mask the gate's answer). Spoofed first: the
        # refused request must mint no membership, so the vouched request
        # after it proves the same name admits once the IdP vouches it.
        spoofed = await client.get(
            "/v1/me", headers=auth(make_token(signing_key, groups=["staff"]))
        )
        assert spoofed.status_code == 403, spoofed.text
        assert spoofed.json()["error"]["code"] == "outside_access_groups"
        async with session_factory() as session:
            user = (
                await session.execute(select(User).where(User.email == "member@example.org"))
            ).scalar_one()
            assert "staff" not in [m.group.name for m in user.memberships]

        vouched = await client.get(
            "/v1/me", headers=auth(make_token(signing_key, groups=["acme/staff"]))
        )
        assert vouched.status_code == 200, vouched.text

    async def test_the_key_is_refused_until_the_vouched_name_admits(
        self,
        app: Any,
        client: httpx.AsyncClient,
        seeded: Seeded,
        session_factory: async_sessionmaker[AsyncSession],
        acme_staff_policy: None,
    ) -> None:
        # The owner's own sign-in naming the target directly: dropped, so no
        # membership is created from it.
        with pytest.raises(ProvisioningRefused, match="no longer"):
            async with session_factory() as session:
                await _sign_in(
                    session=session,
                    subject=seeded.user.subject,
                    groups=["staff"],
                    policy=_mapped_policy(),
                )
        response = await client.get("/v1/me", headers=seeded.auth)
        assert response.status_code == 403, response.text
        assert response.json()["error"]["code"] == "outside_access_groups"

        # The vouched IdP name admits, and the key follows the account.
        async with session_factory() as session:
            await _sign_in(
                session=session,
                subject=seeded.user.subject,
                groups=["acme/staff"],
                policy=_mapped_policy(),
            )
        response = await client.get("/v1/me", headers=seeded.auth)
        assert response.status_code == 200, response.text

    async def test_the_console_session_is_refused_until_the_vouched_name_admits(
        self,
        app: Any,
        client: httpx.AsyncClient,
        seeded: Seeded,
        session_factory: async_sessionmaker[AsyncSession],
        acme_staff_policy: None,
    ) -> None:
        with pytest.raises(ProvisioningRefused, match="no longer"):
            async with session_factory() as session:
                await _sign_in(
                    session=session,
                    subject=seeded.user.subject,
                    groups=["staff"],
                    policy=_mapped_policy(),
                )
        cookie = session_cookie(seeded.user.id, app)
        response = await client.get("/api/me/limits", headers=cookie)
        assert response.status_code == 401, response.text

        async with session_factory() as session:
            await _sign_in(
                session=session,
                subject=seeded.user.subject,
                groups=["acme/staff"],
                policy=_mapped_policy(),
            )
        response = await client.get("/api/me/limits", headers=cookie)
        assert response.status_code == 200, response.text


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
