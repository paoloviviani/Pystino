"""Merge-chain resolution (ADR 0093 §3.1, §7.1).

``user_merges`` records only the immediate edge of each merge: who was merged
into whom. Reading "who was ever merged into this person" back out means
following the chain — A into B, then B into C — and :func:`resolve_merged_from`
is the one place that does it, with a single recursive query rather than an
application-side loop that would cost one round trip per link.
"""

from __future__ import annotations

import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from gateway.models import UserMerge


async def resolve_merged_from(session: AsyncSession, user_id: uuid.UUID) -> list[uuid.UUID]:
    """Every id ever merged into ``user_id``, chains resolved: if A was merged
    into B and B was later merged into C, ``resolve_merged_from(C)`` returns
    both A and B, even though A's own row names only B.
    """
    anchor = select(UserMerge.source_user_id.label("id")).where(UserMerge.target_user_id == user_id)
    chain = anchor.cte("merge_chain", recursive=True)
    chain = chain.union_all(
        select(UserMerge.source_user_id).where(UserMerge.target_user_id == chain.c.id)
    )
    return list((await session.execute(select(chain.c.id))).scalars().all())
