"""`pystino admin grant|revoke`: the break-glass of an OIDC-only deployment (D3)."""

from __future__ import annotations

import pytest
from gateway.deploy.admin import AdminCommandError, set_admin
from gateway.deployment_state import get_or_create_deployment_state
from gateway.models import IdentityEvent, IdentityEventAction, User
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

ISS = "https://llm.example.org/authelia"


async def test_grant_is_manual_and_reactivates(session: AsyncSession) -> None:
    user = User(
        issuer=ISS,
        subject="s",
        email="Ops@Example.org",
        is_active=False,
        deactivated_by="directory",
        admin_source="oidc",
    )
    session.add(user)
    await session.commit()
    granted = await set_admin(session, "ops@example.org", grant=True)
    assert granted.is_admin and granted.admin_source == "manual" and granted.is_active


async def test_grant_marks_the_bootstrap_consumed_and_is_audited(session: AsyncSession) -> None:
    user = User(issuer=ISS, subject="s", email="ops@example.org")
    session.add(user)
    await session.commit()
    await set_admin(session, "ops@example.org", grant=True)

    state = await get_or_create_deployment_state(session)
    assert state.bootstrap_admin_consumed_at is not None

    row = (
        await session.execute(select(IdentityEvent).order_by(IdentityEvent.at.desc()))
    ).scalars().first()
    assert row is not None
    assert row.action == IdentityEventAction.ADMIN_GRANT
    assert row.target_user_id == user.id


async def test_revoke_refuses_the_last_admin(session: AsyncSession) -> None:
    session.add(User(issuer=ISS, subject="s", email="a@example.org", is_admin=True))
    await session.commit()
    with pytest.raises(AdminCommandError, match="last active"):
        await set_admin(session, "a@example.org", grant=False)


async def test_ambiguity_and_absence_are_named(session: AsyncSession) -> None:
    session.add_all(
        [
            User(issuer=ISS, subject="1", email="dup@example.org"),
            User(issuer="https://other.example.org", subject="2", email="dup@example.org"),
        ]
    )
    await session.commit()
    with pytest.raises(AdminCommandError, match="several issuers"):
        await set_admin(session, "dup@example.org", grant=True)
    assert (await set_admin(session, "dup@example.org", grant=True, issuer=ISS)).is_admin
    with pytest.raises(AdminCommandError, match="sign in once"):
        await set_admin(session, "nobody@example.org", grant=True)
