"""The first administrator of an OIDC-only deployment (ADR 0088 draft, ADR 0093 §5.3).

With no local-password door, `gateway passwd --admin` has nothing to attach
to; the configured bootstrap address, verified by the IdP, is the way in. The
tests pin the four conditions, that it is honoured only with the bundled
Authelia, and — above all — that it fires at most once per deployment and
never re-arms, even once its only administrator is gone.
"""

from __future__ import annotations

from gateway.config import OIDCSettings
from gateway.models import User
from gateway.oidc import promote_bootstrap_admin, provision_user
from sqlalchemy.ext.asyncio import AsyncSession

BOOT = "Ops@Example.org"


async def _login(session: AsyncSession, subject: str, email: str) -> User:
    user = await provision_user(
        session,
        issuer="https://llm.example.org/authelia",
        subject=subject,
        email=email,
        display_name=None,
        group_names=[],
        settings=OIDCSettings(),
    )
    await session.flush()
    return user


async def test_the_verified_bootstrap_address_becomes_the_first_admin(
    session: AsyncSession,
) -> None:
    user = await _login(session, "s1", "ops@example.org")
    promoted = await promote_bootstrap_admin(
        session,
        user,
        bootstrap_email=BOOT,
        email="ops@example.org",
        email_verified=True,
        kind="authelia",
    )
    assert promoted and user.is_admin


async def test_it_is_inert_once_the_bootstrap_has_fired(session: AsyncSession) -> None:
    first = await _login(session, "s1", "ops@example.org")
    assert await promote_bootstrap_admin(
        session,
        first,
        bootstrap_email=BOOT,
        email="ops@example.org",
        email_verified=True,
        kind="authelia",
    )
    # A second, distinct account — e.g. the same address at a new issuer after
    # a switch — must not be promoted a second time.
    user = await _login(session, "s2", "ops@example.org")
    promoted = await promote_bootstrap_admin(
        session,
        user,
        bootstrap_email=BOOT,
        email="ops@example.org",
        email_verified=True,
        kind="authelia",
    )
    assert not promoted and not user.is_admin


async def test_deactivating_the_only_admin_does_not_rearm_it(session: AsyncSession) -> None:
    # This is the bug the redesign closes (review correction 2 / R1): the old
    # gate was "no *active* administrator", so disabling the only admin quietly
    # reopened the door to whoever next signed in with the bootstrap address.
    gone = await _login(session, "s1", "ops@example.org")
    assert await promote_bootstrap_admin(
        session,
        gone,
        bootstrap_email=BOOT,
        email="ops@example.org",
        email_verified=True,
        kind="authelia",
    )
    gone.is_active = False
    await session.flush()
    user = await _login(session, "s2", "ops@example.org")
    assert not await promote_bootstrap_admin(
        session,
        user,
        bootstrap_email=BOOT,
        email="ops@example.org",
        email_verified=True,
        kind="authelia",
    )
    assert not user.is_admin


async def test_ignored_with_an_external_idp(session: AsyncSession) -> None:
    user = await _login(session, "s1", "ops@example.org")
    assert not await promote_bootstrap_admin(
        session,
        user,
        bootstrap_email=BOOT,
        email="ops@example.org",
        email_verified=True,
        kind="generic",
    )
    assert not user.is_admin


async def test_unverified_or_string_verified_emails_are_refused(session: AsyncSession) -> None:
    user = await _login(session, "s1", "ops@example.org")
    for verified in (None, False, "true", 1):
        assert not await promote_bootstrap_admin(
            session,
            user,
            bootstrap_email=BOOT,
            email="ops@example.org",
            email_verified=verified,
            kind="authelia",
        )
    assert not user.is_admin


async def test_other_addresses_and_an_unset_bootstrap_do_nothing(session: AsyncSession) -> None:
    user = await _login(session, "s1", "else@example.org")
    assert not await promote_bootstrap_admin(
        session,
        user,
        bootstrap_email=BOOT,
        email="else@example.org",
        email_verified=True,
        kind="authelia",
    )
    assert not await promote_bootstrap_admin(
        session,
        user,
        bootstrap_email="",
        email="else@example.org",
        email_verified=True,
        kind="authelia",
    )
    assert not user.is_admin
