"""Offset pagination for the management API.

One shape for every listing, so a client needs one helper rather than a lookup
table of which endpoints truncate.

**Offset, not a cursor.** A cursor is the better answer for an append-only feed
consumed forwards, but the console's tables are sorted by name and want to say
"51-100 of 812" and to jump to the last page. A cursor can express neither. The
costs are real and bounded: a deep ``offset`` makes the database walk the rows
it skips, and a row inserted while an operator pages can shift the window so an
entry is seen twice or not at all. Both are acceptable for an administrative
table over thousands of rows; neither is acceptable for billing, which is why
nothing in the ledger is read through this.

**Row listings paginate; aggregations do not.** ``/api/admin/users`` is a
listing and truncates. ``/api/admin/reports/usage`` is a sum over a period, and
a truncated sum is a wrong number presented as a total — those endpoints return
every row they aggregated, or they would be lying.
"""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import Depends, Query
from pydantic import BaseModel, Field
from sqlalchemy import Select, func, select
from sqlalchemy.ext.asyncio import AsyncSession

DEFAULT_LIMIT = 50
MAX_LIMIT = 200


class Page[T](BaseModel):
    """A window onto a listing, and how big the listing actually is.

    ``total`` is what the filter matched, not what was returned: a client shows
    "50 of 812" from this alone, and knows there is more without a second
    request.
    """

    items: list[T]
    total: int = Field(ge=0)
    limit: int = Field(ge=1)
    offset: int = Field(ge=0)


class PageParams(BaseModel):
    """``limit`` and ``offset``, validated once.

    The ceiling is not a formality. Without it a caller can ask for every row
    and the endpoint that was paginated to protect the process against a large
    organisation goes back to loading it all.
    """

    limit: int = DEFAULT_LIMIT
    offset: int = 0

    def apply[R: tuple[Any, ...]](self, stmt: Select[R]) -> Select[R]:
        return stmt.limit(self.limit).offset(self.offset)

    def page[T](self, items: list[T], total: int) -> Page[T]:
        return Page(items=items, total=total, limit=self.limit, offset=self.offset)

    def slice[T](self, items: list[T]) -> Page[T]:
        """Paginate a list already in memory.

        For the few listings whose rows are assembled in Python rather than
        selected — a price history read off a loaded relationship, quota rules
        that need live counter values before they can be sorted. The database
        work is not saved, but the response shape and the caller's code are the
        same as everywhere else.
        """
        return self.page(items[self.offset : self.offset + self.limit], len(items))


def page_params(
    limit: Annotated[
        int,
        Query(ge=1, le=MAX_LIMIT, description=f"Rows to return, at most {MAX_LIMIT}."),
    ] = DEFAULT_LIMIT,
    offset: Annotated[int, Query(ge=0, description="Rows to skip.")] = 0,
) -> PageParams:
    return PageParams(limit=limit, offset=offset)


PageDep = Annotated[PageParams, Depends(page_params)]


async def count_of(session: AsyncSession, stmt: Select[Any]) -> int:
    """How many rows the statement matches, ignoring its ordering and window.

    ``order_by`` is stripped because ordering a count is pointless work, and
    because Postgres rejects an ``ORDER BY`` over a column that a wrapped
    subquery does not expose.
    """
    counted = select(func.count()).select_from(stmt.order_by(None).subquery())
    return int((await session.execute(counted)).scalar_one())
