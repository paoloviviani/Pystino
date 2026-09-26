"""`pystino admin grant|revoke <email>`: the break-glass for an OIDC-only deployment.

With no local-password door (decision D3), the ways to become an administrator
are the console (another administrator), a provider that decides admin by
claim, the bootstrap address on an empty deployment — and this, for when all
three are unavailable. It runs inside the gateway container
(`docker compose exec gateway pystino admin grant you@example.org`) against
the database directly, so it needs shell access to the host, which already
implies the database. A grant is recorded as `manual`: no directory can undo it.
"""

from __future__ import annotations

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from gateway.deployment_state import mark_bootstrap_consumed
from gateway.identity_events import record_event
from gateway.models import IdentityEventAction, IdentityEventActor, User
from gateway.oidc import other_active_admin_exists


class AdminCommandError(RuntimeError):
    pass


async def set_admin(session: AsyncSession, email: str, *, grant: bool, issuer: str = "") -> User:
    query = select(User).where(func.lower(User.email) == email.strip().casefold())
    if issuer:
        query = query.where(User.issuer == issuer)
    users = list((await session.execute(query)).scalars())
    if not users:
        raise AdminCommandError(
            f"no account has the email {email}; sign in once through the identity provider first"
        )
    if len(users) > 1:
        issuers = ", ".join(sorted(u.issuer for u in users))
        raise AdminCommandError(
            f"{email} names accounts at several issuers ({issuers}); pass --issuer"
        )
    user = users[0]
    would_revoke = not grant and user.is_admin
    if would_revoke and not await other_active_admin_exists(session, excluding=user.id):
        await record_event(
            session,
            actor_type=IdentityEventActor.CLI,
            actor_label="cli:admin-revoke",
            action=IdentityEventAction.ADMIN_REFUSED_LAST,
            target_user_id=user.id,
            target_label=user.email or email,
            reason="pystino admin revoke would leave no active administrator",
        )
        await session.commit()
        raise AdminCommandError("refusing to revoke the last active administrator")
    user.is_admin = grant
    user.admin_source = "manual"
    if grant and not user.is_active:
        user.is_active = True
        user.deactivated_by = None
    await record_event(
        session,
        actor_type=IdentityEventActor.CLI,
        actor_label="cli:admin-grant" if grant else "cli:admin-revoke",
        action=IdentityEventAction.ADMIN_GRANT if grant else IdentityEventAction.ADMIN_REVOKE,
        target_user_id=user.id,
        target_label=user.email or email,
    )
    if grant:
        await mark_bootstrap_consumed(session)
    await session.commit()
    return user
