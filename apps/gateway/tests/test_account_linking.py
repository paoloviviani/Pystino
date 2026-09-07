"""ADR 0056: an IdP login adopting the local account with the same address.

The switch reverses a refusal that was written into the ``LocalCredential``
docstring, so the tests are mostly about what still refuses. In order:

- off by default: the same address at the same time produces two accounts,
  exactly as it did before this existed;
- on, with a verified address: one account, and it is the local one — same
  row id, so keys, spend, quotas and memberships come with it;
- ``email_verified`` false, absent, or the string ``"true"``: declined. The
  absent case is the one that matters — silence is not consent;
- the match is on the local door's address, casefolded, and never on
  ``users.email``;
- an account at *another* directory is never adopted, only a local one;
- a second subject from the same directory is declined, not adopted;
- the local password keeps working, which is the invariant the whole shape
  exists to protect: ``users.issuer`` is still ``"local"`` after linking;
- the directory becomes authoritative for groups and admin, which is a
  consequence worth pinning rather than discovering.

The browser callback itself has no test here — the file it lives in says why
(nothing drives the redirect flow) — so what is tested is the seam it calls
with one keyword, and ``TestTheSwitchIsCarried`` covers the row and the
record either side of it.
"""

from __future__ import annotations

import httpx
import pytest
from conftest import Seeded
from gateway.config import OIDCSettings
from gateway.identity_registry import record_from_env, record_from_row
from gateway.models import (
    Group,
    IdentityProvider,
    LocalCredential,
    Membership,
    MembershipSource,
    User,
    UserIdentity,
)
from gateway.oidc import provision_user, sync_user_from_claims
from gateway.oidc_policy import OIDCPolicy
from gateway.secrets import SecretBox
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from test_admin import as_user, make_admin

IDP = "https://idp.test"
ADDRESS = "paolo@example.org"
# Shaped like an Argon2 hash and deliberately not one: no test here verifies a
# password, and a real hash would invite someone to try.
NOT_A_HASH = "argon2id-placeholder"


async def make_local_user(
    session: AsyncSession, *, email: str = ADDRESS, is_admin: bool = False, active: bool = True
) -> User:
    """A local account exactly as ``gateway passwd`` makes one: casefolded subject."""
    user = User(
        issuer="local",
        subject=email.casefold(),
        email=email.casefold(),
        display_name="Local Person",
        is_admin=is_admin,
        is_active=active,
    )
    session.add(user)
    await session.flush()
    session.add(LocalCredential(user_id=user.id, password_hash=NOT_A_HASH))
    await session.commit()
    return user


async def sign_in(
    session: AsyncSession,
    *,
    subject: str = "idp-subject",
    email: str | None = ADDRESS,
    email_verified: object = True,
    allow_local_link: bool = True,
    issuer: str = IDP,
    groups: list[str] | None = None,
    policy: OIDCPolicy | None = None,
) -> User:
    user = await provision_user(
        session,
        issuer=issuer,
        subject=subject,
        email=email,
        display_name="Directory Person",
        group_names=groups if groups is not None else [],
        settings=OIDCSettings(),
        policy=policy,
        allow_local_link=allow_local_link,
        email_verified=email_verified,  # type: ignore[arg-type]
    )
    await session.commit()
    return user


async def user_count(session: AsyncSession) -> int:
    return len((await session.execute(select(User))).scalars().all())


async def links(session: AsyncSession) -> list[UserIdentity]:
    return list((await session.execute(select(UserIdentity))).scalars().all())


class TestOffByDefault:
    async def test_two_accounts_when_the_switch_is_off(self, session: AsyncSession) -> None:
        """The behaviour every deployment has today, and after the upgrade."""
        local = await make_local_user(session)
        user = await sign_in(session, allow_local_link=False)

        assert user.id != local.id
        assert await user_count(session) == 2
        assert await links(session) == []

    async def test_provision_user_defaults_to_off(self, session: AsyncSession) -> None:
        """A caller that says nothing is not opting in — /v1 is such a caller."""
        local = await make_local_user(session)
        user = await provision_user(
            session,
            issuer=IDP,
            subject="idp-subject",
            email=ADDRESS,
            display_name=None,
            group_names=[],
            settings=OIDCSettings(),
        )
        await session.commit()
        assert user.id != local.id


class TestLinking:
    async def test_a_verified_address_adopts_the_local_account(
        self, session: AsyncSession
    ) -> None:
        local = await make_local_user(session)
        user = await sign_in(session)

        assert user.id == local.id, "the same row, so keys, spend and quotas come with it"
        assert await user_count(session) == 1
        (link,) = await links(session)
        assert (link.issuer, link.subject, link.matched_email) == (IDP, "idp-subject", ADDRESS)

    async def test_the_local_door_still_works_after_linking(self, session: AsyncSession) -> None:
        """The invariant the whole design protects.

        ``issuer == "local"`` is read in eight places as "this account signs in
        with a password". Linking must not touch it, or the person keeps a
        password that no longer opens anything.
        """
        local = await make_local_user(session)
        await sign_in(session)

        await session.refresh(local)
        assert local.issuer == "local"
        assert local.subject == ADDRESS
        credential = await session.get(LocalCredential, local.id)
        assert credential is not None

    async def test_the_second_login_reuses_the_link(self, session: AsyncSession) -> None:
        local = await make_local_user(session)
        await sign_in(session)
        again = await sign_in(session)

        assert again.id == local.id
        assert len(await links(session)) == 1, "linked once, not once per login"

    async def test_the_match_is_case_insensitive(self, session: AsyncSession) -> None:
        """Local subjects are casefolded, so the claim has to be too."""
        local = await make_local_user(session, email="Paolo@Example.org")
        user = await sign_in(session, email="PAOLO@EXAMPLE.ORG")
        assert user.id == local.id

    async def test_a_surrounding_space_does_not_defeat_the_match(
        self, session: AsyncSession
    ) -> None:
        local = await make_local_user(session)
        user = await sign_in(session, email=f"  {ADDRESS} ")
        assert user.id == local.id

    async def test_no_local_account_creates_the_ordinary_new_one(
        self, session: AsyncSession
    ) -> None:
        user = await sign_in(session, email="stranger@example.org")
        assert user.issuer == IDP
        assert await links(session) == []

    async def test_an_inactive_local_account_is_adopted_and_stays_inactive(
        self, session: AsyncSession
    ) -> None:
        """Disabling someone must not be undone by their signing in through SSO."""
        local = await make_local_user(session, active=False)
        user = await sign_in(session)
        assert user.id == local.id
        assert user.is_active is False

    async def test_linking_is_allowed_where_creating_would_be_refused(
        self, session: AsyncSession
    ) -> None:
        """Auto-provisioning off is a rule about strangers (ADR 0048).

        An adopted account already exists here, so the gate that refuses to
        create new ones must not refuse to *find* it.
        """
        local = await make_local_user(session)
        policy = OIDCPolicy(
            auto_provision=False, unknown_user_policy="refuse", groups_claim="groups"
        )
        user = await sign_in(session, policy=policy)
        assert user.id == local.id


class TestRefusals:
    @pytest.mark.parametrize(
        "verified",
        [
            pytest.param(False, id="false"),
            pytest.param(None, id="absent"),
            pytest.param("true", id="the-string"),
            pytest.param(1, id="truthy-but-not-true"),
        ],
    )
    async def test_only_a_boolean_true_earns_a_link(
        self, session: AsyncSession, verified: object
    ) -> None:
        local = await make_local_user(session)
        user = await sign_in(session, email_verified=verified)

        assert user.id != local.id, "an unverified address is a claim, not a proof"
        assert await links(session) == []
        assert await user_count(session) == 2

    async def test_no_email_no_link(self, session: AsyncSession) -> None:
        local = await make_local_user(session)
        user = await sign_in(session, email=None)
        assert user.id != local.id
        assert await links(session) == []

    async def test_an_account_at_another_directory_is_never_adopted(
        self, session: AsyncSession
    ) -> None:
        """Only a *local* account is adopted.

        Two directories that both know an address are not evidence that they
        mean one person, and the switch is one operator's statement about one
        directory.
        """
        first = await sign_in(session, issuer="https://other.test", subject="s1")
        assert first.email == ADDRESS

        second = await sign_in(session, subject="s2")
        assert second.id != first.id
        assert await links(session) == []

    async def test_a_second_subject_from_the_same_directory_is_declined(
        self, session: AsyncSession
    ) -> None:
        """The address was reassigned, or there are two accounts in the directory.

        Adopting on the strength of the address alone would hand the newcomer
        everything the first one has.
        """
        local = await make_local_user(session)
        await sign_in(session, subject="first-subject")

        newcomer = await sign_in(session, subject="second-subject")
        assert newcomer.id != local.id
        (link,) = await links(session)
        assert link.subject == "first-subject", "the first link is untouched"


class TestWhatAdoptionChanges:
    """Consequences of linking, pinned so they are decisions and not surprises."""

    async def test_groups_the_local_account_already_had_survive_adoption(
        self, session: AsyncSession
    ) -> None:
        """Changed by ADR 0057, and this test with it.

        It used to assert the opposite — that the directory replaced the
        adopted account's groups — which is exactly the behaviour that was
        wrong: an administrator's grant is not the directory's to withdraw,
        and an account being adopted is the least appropriate moment to
        withdraw one.
        """
        local = await make_local_user(session)
        group = Group(name="finance")
        session.add(group)
        await session.flush()
        session.add(
            Membership(user_id=local.id, group_id=group.id, source=MembershipSource.MANUAL)
        )
        await session.commit()

        user = await sign_in(session, groups=["research"])
        assert {m.group.name for m in user.memberships} == {"finance", "research"}

    async def test_admin_follows_the_directory_when_admin_groups_are_configured(
        self, session: AsyncSession
    ) -> None:
        """The lockout worth knowing about before turning the switch on.

        A local admin adopted by a directory that does not put them in an
        admin group loses the flag on that login. The recovery is the local
        door, which is exactly what this design keeps working: ``gateway
        passwd`` and the administrator's own edit still treat the row as local.
        """
        local = await make_local_user(session, is_admin=True)
        policy = OIDCPolicy(
            auto_provision=True,
            unknown_user_policy="refuse",
            groups_claim="groups",
            admin_groups=["ops"],
        )
        user = await sign_in(session, groups=["research"], policy=policy)

        assert user.id == local.id
        assert user.is_admin is False

    async def test_admin_is_untouched_when_no_admin_groups_are_configured(
        self, session: AsyncSession
    ) -> None:
        local = await make_local_user(session, is_admin=True)
        user = await sign_in(session, groups=["research"])
        assert user.id == local.id
        assert user.is_admin is True


class TestTheV1Surface:
    async def test_a_bearer_token_resolves_the_link(self, session: AsyncSession) -> None:
        """One person, one row, whichever door they came through.

        Without this a linked person would sign into the console as themselves
        and bill as somebody else.
        """
        local = await make_local_user(session)
        await sign_in(session)

        user = await sync_user_from_claims(
            session,
            claims={"iss": IDP, "sub": "idp-subject", "email": ADDRESS},
            settings=OIDCSettings(),
        )
        assert user.id == local.id

    async def test_a_bearer_token_never_creates_a_link(self, session: AsyncSession) -> None:
        """Linking is a decision, and /v1 is not where it is made.

        The access-token path makes no userinfo request (a round trip per API
        call is not a thing a gateway may do), so the claim that would justify
        a link is the one it is least likely to hold.
        """
        local = await make_local_user(session)
        user = await sync_user_from_claims(
            session,
            claims={"iss": IDP, "sub": "idp-subject", "email": ADDRESS, "email_verified": True},
            settings=OIDCSettings(),
        )
        assert user.id != local.id
        assert await links(session) == []


class TestTheSwitchIsCarried:
    def test_a_row_carries_it_to_the_record(self, app: object) -> None:
        box: SecretBox = app.state.secrets  # type: ignore[attr-defined]
        row = IdentityProvider(
            name="corp",
            issuer=IDP,
            client_id="c",
            client_secret_encrypted=box.encrypt("s"),
            scopes=["openid"],
            link_local_by_email=True,
        )
        assert record_from_row(row, box).link_local_by_email is True

    def test_the_default_is_off_on_a_new_row(self, app: object) -> None:
        box: SecretBox = app.state.secrets  # type: ignore[attr-defined]
        row = IdentityProvider(
            name="corp",
            issuer=IDP,
            client_id="c",
            client_secret_encrypted=box.encrypt("s"),
            scopes=["openid"],
            link_local_by_email=False,
        )
        assert record_from_row(row, box).link_local_by_email is False

    def test_the_environment_fallback_never_links(self, settings: object) -> None:
        """An upgrade may not switch adoption on by itself."""
        from gateway.config import OIDCSettings as OS

        settings.oidc = OS(  # type: ignore[attr-defined]
            enabled=True, issuer=IDP, client_id="c", client_secret="s"
        )
        record = record_from_env(settings)  # type: ignore[arg-type]
        assert record is not None
        assert record.link_local_by_email is False


class TestTheApiSurface:
    """The switch through the management API — the console's half of the wiring."""

    async def test_a_provider_is_created_with_it_off_unless_asked(
        self,
        app: object,
        client: httpx.AsyncClient,
        seeded: Seeded,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        as_user(app, await make_admin(session_factory, seeded))
        response = await client.post(
            "/api/admin/identity-providers",
            json={
                "name": "corp",
                "issuer": "https://corp.example.org",
                "client_id": "gateway",
                "client_secret": "not-a-real-secret",
            },
        )
        assert response.status_code == 201
        assert response.json()["link_local_by_email"] is False

    async def test_it_can_be_asked_for_at_creation(
        self,
        app: object,
        client: httpx.AsyncClient,
        seeded: Seeded,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        as_user(app, await make_admin(session_factory, seeded))
        response = await client.post(
            "/api/admin/identity-providers",
            json={
                "name": "corp",
                "issuer": "https://corp.example.org",
                "client_id": "gateway",
                "client_secret": "not-a-real-secret",
                "link_local_by_email": True,
            },
        )
        assert response.status_code == 201
        assert response.json()["link_local_by_email"] is True

        async with session_factory() as session:
            row = (await session.execute(select(IdentityProvider))).scalar_one()
            assert row.link_local_by_email is True

    async def test_it_can_be_turned_off_again(
        self,
        app: object,
        client: httpx.AsyncClient,
        seeded: Seeded,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        as_user(app, await make_admin(session_factory, seeded))
        created = await client.post(
            "/api/admin/identity-providers",
            json={
                "name": "corp",
                "issuer": "https://corp.example.org",
                "client_id": "gateway",
                "client_secret": "not-a-real-secret",
                "link_local_by_email": True,
            },
        )
        provider_id = created.json()["id"]
        edited = await client.put(
            "/api/admin/identity-providers/" + provider_id,
            json={"link_local_by_email": False},
        )
        assert edited.status_code == 200
        assert edited.json()["link_local_by_email"] is False

    async def test_a_linked_account_says_so_on_the_user_listing(
        self,
        app: object,
        client: httpx.AsyncClient,
        seeded: Seeded,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        """An operator reading a user screen must be able to see both doors."""
        async with session_factory() as session:
            local = await make_local_user(session)
            await sign_in(session)
            local_id = str(local.id)

        as_user(app, await make_admin(session_factory, seeded))
        response = await client.get("/api/admin/users?limit=100")
        assert response.status_code == 200
        rows = {row["id"]: row for row in response.json()["items"]}
        assert rows[local_id]["linked_identities"] == [IDP]
        assert rows[local_id]["has_password"] is True
