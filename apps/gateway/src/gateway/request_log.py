"""The request log: one line per request, with no content.

``usage_records`` already holds everything a line needs. This module is the
read side only: a filtered, newest-first listing and a CSV of the same set.

**What it must never carry.** ``assistant_text`` and ``error_message`` are not
selected. The query names its columns instead of loading ``UsageRecord``, so a
column added to the ledger later is invisible here until someone adds it on
purpose, and there is no object on which a serializer could reach the text.

**Access is decided by the caller of this module**, not inside it: the admin
route passes whatever user filter the administrator asked for; the ``/api/me``
route passes ``user_id=<the caller>`` and offers no parameter that changes it.
"""

from __future__ import annotations

import csv
import io
import uuid
from dataclasses import dataclass
from typing import Any

from sqlalchemy import Select, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from gateway.errors import BadRequestError
from gateway.models import Group, UsageRecord, UsageStatus, User
from gateway.pagination import Page, PageParams, count_of
from gateway.periods import Period
from gateway.schemas import RequestLogRow

#: A CSV export loads its rows in memory, so it is bounded. A refusal, never a
#: silent truncation: a finance spreadsheet missing its last rows reads as a
#: complete one.
MAX_EXPORT_ROWS = 100_000

CSV_HEADER = [
    "time",
    "request_id",
    "user",
    "group",
    "requested_model",
    "model",
    "upstream_model",
    "streamed",
    "prompt_tokens",
    "completion_tokens",
    "cached_prompt_tokens",
    "cost",
    "currency",
    "latency_ms",
    "status",
    "source",
    "attempt",
]


@dataclass(frozen=True, slots=True)
class RequestLogFilter:
    period: Period
    user_id: uuid.UUID | None = None
    group_id: uuid.UUID | None = None
    #: Matches the alias the caller asked for *or* the model that served it.
    model: str | None = None
    status: UsageStatus | None = None


def _conditions(f: RequestLogFilter) -> list[Any]:
    # Half-open, as the reports and quotas are.
    conditions: list[Any] = [
        UsageRecord.created_at >= f.period.start,
        UsageRecord.created_at < f.period.end,
    ]
    if f.user_id is not None:
        conditions.append(UsageRecord.user_id == f.user_id)
    if f.group_id is not None:
        conditions.append(UsageRecord.group_id == f.group_id)
    if f.model:
        conditions.append(
            or_(UsageRecord.model_name == f.model, UsageRecord.requested_model == f.model)
        )
    if f.status is not None:
        conditions.append(UsageRecord.status == f.status)
    return conditions


def _count_query(f: RequestLogFilter) -> Select[Any]:
    return select(UsageRecord.id).where(*_conditions(f))


def _listing_query(f: RequestLogFilter) -> Select[Any]:
    # Order by created_at then id: the tiebreak keeps pages stable when several
    # rows share a timestamp, and the leading column is the one every
    # created_at index starts or ends with.
    return (
        select(
            UsageRecord.request_id,
            UsageRecord.created_at,
            UsageRecord.user_id,
            func.coalesce(User.email, User.subject).label("user_label"),
            UsageRecord.group_id,
            Group.name.label("group_name"),
            UsageRecord.requested_model,
            UsageRecord.route_attempt,
            UsageRecord.model_name,
            UsageRecord.upstream_model,
            UsageRecord.streamed,
            UsageRecord.prompt_tokens,
            UsageRecord.completion_tokens,
            UsageRecord.cached_prompt_tokens,
            UsageRecord.cost,
            UsageRecord.currency,
            UsageRecord.status,
            UsageRecord.latency_ms,
            UsageRecord.api_key_id,
        )
        .select_from(UsageRecord)
        .outerjoin(User, User.id == UsageRecord.user_id)
        .outerjoin(Group, Group.id == UsageRecord.group_id)
        .where(*_conditions(f))
        .order_by(UsageRecord.created_at.desc(), UsageRecord.id.desc())
    )


def _row(r: Any) -> RequestLogRow:
    return RequestLogRow(
        request_id=r.request_id,
        created_at=r.created_at,
        user_id=r.user_id,
        user_label=r.user_label,
        group_id=r.group_id,
        group_name=r.group_name,
        requested_model=r.requested_model,
        route_attempt=r.route_attempt,
        model=r.model_name,
        upstream_model=r.upstream_model,
        streamed=bool(r.streamed),
        prompt_tokens=r.prompt_tokens,
        completion_tokens=r.completion_tokens,
        cached_prompt_tokens=r.cached_prompt_tokens,
        cost=r.cost,
        currency=r.currency,
        status=str(r.status),
        latency_ms=r.latency_ms,
        source="api_key" if r.api_key_id is not None else "token",
    )


async def list_requests(
    session: AsyncSession, f: RequestLogFilter, page: PageParams
) -> Page[RequestLogRow]:
    total = await count_of(session, _count_query(f))
    rows = (await session.execute(page.apply(_listing_query(f)))).all()
    return page.page([_row(r) for r in rows], total)


def _text(value: str | None) -> str:
    """A text cell safe to open in a spreadsheet.

    A cell that begins with ``=``, ``+``, ``-`` or ``@`` is a formula to Excel,
    and a display name or email is attacker-influenced. A leading apostrophe
    makes it text.
    """
    if not value:
        return ""
    return "'" + value if value[0] in "=+-@\t\r" else value


async def requests_to_csv(session: AsyncSession, f: RequestLogFilter) -> str:
    total = await count_of(session, _count_query(f))
    if total > MAX_EXPORT_ROWS:
        raise BadRequestError(
            f"{total} requests match; an export holds at most {MAX_EXPORT_ROWS}. "
            "Narrow the period or add a filter.",
            code="export_too_large",
        )
    rows = (await session.execute(_listing_query(f))).all()
    buffer = io.StringIO()
    writer = csv.writer(buffer, lineterminator="\r\n")
    writer.writerow(CSV_HEADER)
    for raw in rows:
        r = _row(raw)
        writer.writerow(
            [
                r.created_at.isoformat(),
                _text(r.request_id),
                _text(r.user_label),
                _text(r.group_name),
                _text(r.requested_model),
                _text(r.model),
                _text(r.upstream_model),
                "true" if r.streamed else "false",
                r.prompt_tokens,
                r.completion_tokens,
                r.cached_prompt_tokens,
                # Plain decimal text, never a float and never "1E-7".
                f"{r.cost:f}",
                r.currency,
                "" if r.latency_ms is None else r.latency_ms,
                r.status,
                r.source,
                "" if r.route_attempt is None else r.route_attempt,
            ]
        )
    return buffer.getvalue()
