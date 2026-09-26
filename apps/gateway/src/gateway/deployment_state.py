"""The one-row `deployment_state` table (ADR 0093 §3.1, §5.3).

Everything here that is not about bootstrap is not written yet: the table
holds only `bootstrap_admin_consumed_at` for now. Read and write it through
`get_or_create_deployment_state` / `mark_bootstrap_consumed`, not by querying
`DeploymentState` directly — the row does not exist until something asks for
it (the unit suite's schema comes from `Base.metadata.create_all()`, which
creates no rows; a real deployment gets it from the migration backfill).
"""

from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession

from gateway.models import DeploymentState
from gateway.types import utcnow

_ROW_ID = 1


async def get_or_create_deployment_state(session: AsyncSession) -> DeploymentState:
    row = await session.get(DeploymentState, _ROW_ID)
    if row is None:
        row = DeploymentState(id=_ROW_ID)
        session.add(row)
        await session.flush()
    return row


async def mark_bootstrap_consumed(session: AsyncSession) -> None:
    """Retire the bootstrap door for good, if it is not retired already.

    Idempotent. Call this from every path that can produce the deployment's
    first administrator — `promote_bootstrap_admin`, the console's PATCH, the
    CLI's grant, and (once it lands) the env admin rules — so that once any of
    them has run, deactivating every administrator afterwards can never make
    the bootstrap door fire again.
    """
    row = await get_or_create_deployment_state(session)
    if row.bootstrap_admin_consumed_at is None:
        row.bootstrap_admin_consumed_at = utcnow()
