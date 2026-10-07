"""ADR 0093 §6.2: linking an unknown identity to an existing account by
verified email, and the doors/settings that gate it.

`link_by_email` is tested directly, condition by condition, because each of
the eight is its own refusal and a single end-to-end test would only ever
prove the conjunction, not which clause did the refusing. `sign_in` is then
tested for the two properties that are its own, not `link_by_email`'s: that
it never links an admin (belt-and-braces on top of `link_by_email`'s own
condition 6, since `sign_in` is the only caller that could get the ordering
wrong), and that it wires the switch and the bind-before-link order together
correctly. `sync_user_from_claims` — the plain `/v1` path — is tested for the
one property review R2 and §6 both care about: it never runs the rule at
all, whatever the switch says.
"""

from __future__ import annotations

import uuid

from gateway.config import OIDCSettings, Settings
from gateway.identity_registry import record_from_env
from gateway.models import IdentityEvent, IdentityEventAction, User, UserIdentity
from gateway.oidc import link_by_email, sign_in, sync_user_from_claims
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

TARGET_EMAIL = "person@example.org"


async def make_target(
    session: AsyncSession,
    *,
    email: str = TARGET_EMAIL,
    email_verified: bool | None = True,
    admin_edited_fields: list[str] | None = None,
    is_active: bool = True,
    is_admin: bool = False,
    issuer: str = "https://old-idp.test",
) -> User:
    user = User(
        issuer=issuer,
        subject="old-subject",
        email=email,
        email_normalized=email.casefold(),
        email_verified=email_verified,
        admin_edited_fields=admin_edited_fields or [],
        is_active=is_active,
        is_admin=is_admin,
    )
    session.add(user)
    await session.commit()
    return user


class TestLinkByEmail:
    async def test_declines_when_email_verified_is_not_the_literal_true(
        self, session: AsyncSession
    ) -> None:
        await make_target(session)
        for value in (False, None, "true", 1):
            result = await link_by_email(
                session,
                issuer="https://new-idp.test",
                subject="new-subject",
                email=TARGET_EMAIL,
                email_verified=value,
            )
            assert result is None

    async def test_declines_a_non_ascii_or_unparseable_address(self, session: AsyncSession) -> None:
        await make_target(session, email="Persön@example.org", email_verified=True)
        result = await link_by_email(
            session,
            issuer="https://new-idp.test",
            subject="new-subject",
            email="Persön@example.org",
            email_verified=True,
        )
        assert result is None

    async def test_declines_when_no_account_holds_the_address(self, session: AsyncSession) -> None:
        result = await link_by_email(
            session,
            issuer="https://new-idp.test",
            subject="new-subject",
            email="nobody@example.org",
            email_verified=True,
        )
        assert result is None

    async def test_declines_when_two_accounts_share_the_address(
        self, session: AsyncSession
    ) -> None:
        await make_target(session, issuer="https://idp-a.test")
        await make_target(session, issuer="https://idp-b.test")
        result = await link_by_email(
            session,
            issuer="https://new-idp.test",
            subject="new-subject",
            email=TARGET_EMAIL,
            email_verified=True,
        )
        assert result is None

    async def test_declines_an_unverified_stored_address(self, session: AsyncSession) -> None:
        await make_target(session, email_verified=False)
        result = await link_by_email(
            session,
            issuer="https://new-idp.test",
            subject="new-subject",
            email=TARGET_EMAIL,
            email_verified=True,
        )
        assert result is None

    async def test_an_admin_edited_address_qualifies_even_if_never_verified(
        self, session: AsyncSession
    ) -> None:
        """§6.2 condition 4's OR clause: an administrator vouching for the
        address is as good as the directory verifying it."""
        target = await make_target(session, email_verified=None, admin_edited_fields=["email"])
        result = await link_by_email(
            session,
            issuer="https://new-idp.test",
            subject="new-subject",
            email=TARGET_EMAIL,
            email_verified=True,
        )
        assert result is not None
        assert result.id == target.id

    async def test_declines_an_inactive_target(self, session: AsyncSession) -> None:
        await make_target(session, is_active=False)
        result = await link_by_email(
            session,
            issuer="https://new-idp.test",
            subject="new-subject",
            email=TARGET_EMAIL,
            email_verified=True,
        )
        assert result is None

    async def test_declines_an_admin_target(self, session: AsyncSession) -> None:
        await make_target(session, is_admin=True)
        result = await link_by_email(
            session,
            issuer="https://new-idp.test",
            subject="new-subject",
            email=TARGET_EMAIL,
            email_verified=True,
        )
        assert result is None

    async def test_declines_a_pending_target(self, session: AsyncSession) -> None:
        await make_target(session, issuer="pystino:pending")
        result = await link_by_email(
            session,
            issuer="https://new-idp.test",
            subject="new-subject",
            email=TARGET_EMAIL,
            email_verified=True,
        )
        assert result is None

    async def test_declines_when_the_target_already_holds_this_issuer_as_primary(
        self, session: AsyncSession
    ) -> None:
        await make_target(session, issuer="https://new-idp.test")
        result = await link_by_email(
            session,
            issuer="https://new-idp.test",
            subject="a-different-subject",
            email=TARGET_EMAIL,
            email_verified=True,
        )
        assert result is None

    async def test_declines_when_the_target_already_holds_a_linked_identity_there(
        self, session: AsyncSession
    ) -> None:
        target = await make_target(session)
        session.add(
            UserIdentity(
                user_id=target.id,
                issuer="https://new-idp.test",
                subject="already-linked-subject",
                matched_email=TARGET_EMAIL,
            )
        )
        await session.commit()

        result = await link_by_email(
            session,
            issuer="https://new-idp.test",
            subject="a-different-subject",
            email=TARGET_EMAIL,
            email_verified=True,
        )
        assert result is None

    async def test_success_adds_an_identity_and_audits_the_link(
        self, session: AsyncSession
    ) -> None:
        target = await make_target(session)

        result = await link_by_email(
            session,
            issuer="https://new-idp.test",
            subject="new-subject",
            email=TARGET_EMAIL.upper(),  # case must not matter (§6.1)
            email_verified=True,
        )
        await session.commit()

        assert result is not None
        assert result.id == target.id

        row = (
            await session.execute(
                select(UserIdentity).where(
                    UserIdentity.user_id == target.id,
                    UserIdentity.issuer == "https://new-idp.test",
                )
            )
        ).scalar_one()
        assert row.subject == "new-subject"
        assert row.matched_email == TARGET_EMAIL

        event = (
            await session.execute(
                select(IdentityEvent).where(
                    IdentityEvent.action == IdentityEventAction.IDENTITY_LINK
                )
            )
        ).scalar_one()
        assert event.target_user_id == target.id
        assert event.detail == {"matched_email": TARGET_EMAIL, "issuer": "https://new-idp.test"}


def _env_record(*, link_by_email: bool) -> tuple[Settings, object]:
    settings = Settings(
        oidc=OIDCSettings(
            enabled=True,
            issuer="https://new-idp.test",
            client_id="c",
            groups_claim="groups",
            link_by_email=link_by_email,
        )
    )
    record = record_from_env(settings)
    assert record is not None
    return settings, record


class TestSignInWiring:
    async def test_sign_in_links_by_email_when_the_switch_is_on(
        self, session: AsyncSession
    ) -> None:
        target = await make_target(session)
        settings, record = _env_record(link_by_email=True)

        result = await sign_in(
            session,
            record,
            issuer="https://new-idp.test",
            subject="new-subject",
            claims={
                "iss": "https://new-idp.test",
                "sub": "new-subject",
                "email": TARGET_EMAIL,
                "email_verified": True,
            },
            settings=settings,
        )
        await session.commit()

        assert result.user.id == target.id
        assert (await session.execute(select(User))).scalars().all() == [target]

    async def test_sign_in_does_not_link_when_the_switch_is_off(
        self, session: AsyncSession
    ) -> None:
        await make_target(session)
        settings, record = _env_record(link_by_email=False)

        result = await sign_in(
            session,
            record,
            issuer="https://new-idp.test",
            subject="new-subject",
            claims={
                "iss": "https://new-idp.test",
                "sub": "new-subject",
                "email": TARGET_EMAIL,
                "email_verified": True,
            },
            settings=settings,
        )
        await session.commit()

        all_users = (await session.execute(select(User))).scalars().all()
        assert len(all_users) == 2, "a second, unlinked account, not the existing one"
        assert result.user.id not in {u.id for u in all_users if u.issuer != "https://new-idp.test"}

    async def test_sign_in_never_links_an_admin(self, session: AsyncSession) -> None:
        admin = await make_target(session, is_admin=True)
        settings, record = _env_record(link_by_email=True)

        result = await sign_in(
            session,
            record,
            issuer="https://new-idp.test",
            subject="new-subject",
            claims={
                "iss": "https://new-idp.test",
                "sub": "new-subject",
                "email": TARGET_EMAIL,
                "email_verified": True,
            },
            settings=settings,
        )
        await session.commit()

        assert result.user.id != admin.id
        assert len((await session.execute(select(User))).scalars().all()) == 2


class TestBearerPathNeverLinks:
    """The successor of the removed `test_a_bearer_token_never_creates_a_link`
    (ADR 0093 §3.2 dropped the old, unsound matcher this once named; §6.2
    replaces it, gated exactly the same way: never on a plain `/v1` call).
    """

    async def test_a_bearer_token_with_a_matching_email_creates_a_new_user_not_a_link(
        self, session: AsyncSession
    ) -> None:
        target = await make_target(session)
        settings = OIDCSettings(
            enabled=True,
            issuer="https://new-idp.test",
            client_id="c",
            groups_claim="groups",
            link_by_email=True,
        )

        user = await sync_user_from_claims(
            session,
            claims={
                "iss": "https://new-idp.test",
                "sub": str(uuid.uuid4()),
                "email": TARGET_EMAIL,
                "email_verified": True,
            },
            settings=settings,
        )
        await session.commit()

        assert user.id != target.id
        assert (
            await session.execute(select(UserIdentity).where(UserIdentity.user_id == target.id))
        ).scalar_one_or_none() is None
