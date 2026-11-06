"""A caller's own usage by the label it put on its requests (``x-pystino-tag``).

Read side only, and **only ever under the caller's own ``user_id``**: a tag is a
label the caller chose, never an authorisation, so asking for someone else's
tag returns nothing rather than their figures. Bring-your-own-key rows and
requests still in flight are left out, exactly as `caller_usage` leaves them
out of the spend a quota counts — this is what the gateway metered and charged.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal

from sqlalchemy import ColumnElement, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from gateway.models import UsageRecord, UsageStatus
from gateway.schemas import TagRequestRow, TagUsageRow

#: Most labels one call may ask about, and most request rows one call returns.
MAX_TAGS = 100
MAX_REQUEST_ROWS = 1000


def _conditions(
    user_id: uuid.UUID, since: datetime | None, until: datetime | None
) -> list[ColumnElement[bool]]:
    conditions: list[ColumnElement[bool]] = [
        UsageRecord.user_id == user_id,
        UsageRecord.byok.is_(False),
        UsageRecord.status != UsageStatus.IN_PROGRESS,
    ]
    if since is not None:
        conditions.append(UsageRecord.created_at >= since)
    if until is not None:
        conditions.append(UsageRecord.created_at < until)
    return conditions


async def usage_by_tag(
    session: AsyncSession,
    *,
    user_id: uuid.UUID,
    tags: list[str],
    currency: str,
    since: datetime | None = None,
    until: datetime | None = None,
) -> list[TagUsageRow]:
    """One total per asked-for tag that has any rows, in the order asked."""
    stmt = (
        select(
            UsageRecord.tag,
            func.count(UsageRecord.id),
            func.coalesce(func.sum(UsageRecord.prompt_tokens), 0),
            func.coalesce(func.sum(UsageRecord.completion_tokens), 0),
            func.coalesce(func.sum(UsageRecord.cached_prompt_tokens), 0),
            func.coalesce(func.sum(UsageRecord.cost), 0),
            func.min(UsageRecord.created_at),
            func.max(UsageRecord.created_at),
        )
        .where(UsageRecord.tag.in_(tags), *_conditions(user_id, since, until))
        .group_by(UsageRecord.tag)
    )
    found = {row[0]: row for row in (await session.execute(stmt)).all()}
    out: list[TagUsageRow] = []
    for tag in dict.fromkeys(tags):
        row = found.get(tag)
        if row is None:
            continue
        out.append(
            TagUsageRow(
                tag=tag,
                requests=int(row[1]),
                prompt_tokens=int(row[2]),
                completion_tokens=int(row[3]),
                cached_prompt_tokens=int(row[4]),
                cost=Decimal(str(row[5])),
                currency=currency,
                first_at=row[6],
                last_at=row[7],
            )
        )
    return out


async def requests_by_tag(
    session: AsyncSession,
    *,
    user_id: uuid.UUID,
    tag: str,
    currency: str,
    since: datetime | None = None,
    until: datetime | None = None,
    limit: int = MAX_REQUEST_ROWS,
) -> list[TagRequestRow]:
    """The newest ``limit`` requests under one tag: when, how many tokens, what cost.

    Enough for a client to bucket a session into turns by its own timestamps.
    No prompt or completion text, no error message, no request id.
    """
    stmt = (
        select(
            UsageRecord.created_at,
            UsageRecord.model_name,
            UsageRecord.prompt_tokens,
            UsageRecord.completion_tokens,
            UsageRecord.cached_prompt_tokens,
            UsageRecord.cost,
        )
        .where(UsageRecord.tag == tag, *_conditions(user_id, since, until))
        .order_by(UsageRecord.created_at.desc(), UsageRecord.id.desc())
        .limit(min(limit, MAX_REQUEST_ROWS))
    )
    return [
        TagRequestRow(
            at=r[0],
            model=r[1],
            prompt_tokens=r[2],
            completion_tokens=r[3],
            cached_prompt_tokens=r[4],
            cost=Decimal(str(r[5])),
            currency=currency,
        )
        for r in (await session.execute(stmt)).all()
    ]
