"""The first administrator of an OIDC-only deployment (ADR 0088 draft).

With no local-password door, `gateway passwd --admin` has nothing to attach
to; the configured bootstrap address, verified by the IdP, is the way in. The
tests pin the four conditions and, above all, that it can seed an empty
console but never add a second administrator.
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
        session, user, bootstrap_email=BOOT, email="ops@example.org", email_verified=True
    )
    assert promoted and user.is_admin


async def test_it_is_inert_once_any_admin_exists(session: AsyncSession) -> None:
    first = await _login(session, "s1", "someone@example.org")
    first.is_admin = True
    await session.flush()
    user = await _login(session, "s2", "ops@example.org")
    promoted = await promote_bootstrap_admin(
        session, user, bootstrap_email=BOOT, email="ops@example.org", email_verified=True
    )
    assert not promoted and not user.is_admin


async def test_an_inactive_admin_does_not_count(session: AsyncSession) -> None:
    # A deployment whose only administrator was disabled must still be
    # recoverable by the bootstrap address.
    gone = await _login(session, "s1", "gone@example.org")
    gone.is_admin, gone.is_active = True, False
    await session.flush()
    user = await _login(session, "s2", "ops@example.org")
    assert await promote_bootstrap_admin(
        session, user, bootstrap_email=BOOT, email="ops@example.org", email_verified=True
    )


async def test_unverified_or_string_verified_emails_are_refused(session: AsyncSession) -> None:
    user = await _login(session, "s1", "ops@example.org")
    for verified in (None, False, "true", 1):
        assert not await promote_bootstrap_admin(
            session, user, bootstrap_email=BOOT, email="ops@example.org", email_verified=verified
        )
    assert not user.is_admin


async def test_other_addresses_and_an_unset_bootstrap_do_nothing(session: AsyncSession) -> None:
    user = await _login(session, "s1", "else@example.org")
    assert not await promote_bootstrap_admin(
        session, user, bootstrap_email=BOOT, email="else@example.org", email_verified=True
    )
    assert not await promote_bootstrap_admin(
        session, user, bootstrap_email="", email="else@example.org", email_verified=True
    )
    assert not user.is_admin
