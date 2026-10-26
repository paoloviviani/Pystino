"""What a search request costs: the price in force, and the money arithmetic.

A web-search backend is billed **per request**, by the price row an
administrator set for it (``SearchPrice``). Three states have to stay distinct
on a usage row, because collapsing any two of them is a wrong invoice nobody
can explain afterwards:

* **priced**: a row was in force when the search ran. Cost is the price times
  the number of requests, and the row is referenced.
* **billed out of band**: the same, with a price of 0. Cost is 0 and the row is
  *still referenced*. The operator settles the real vendor cost outside this
  gateway and has said so.
* **unpriced**: no row was in force. Cost is 0 and the reference is null, which
  is how a report finds searches nobody priced. This is the state every backend
  starts in, so that a deployment upgrading to this version bills nothing it
  was not told to bill.

One search is one request, whatever the result count: ``max_results`` never
enters the arithmetic.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import TYPE_CHECKING

from sqlalchemy import select

from gateway.models import SearchPrice
from gateway.types import utcnow

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from gateway.fx import FXService

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class SearchQuote:
    """The cost of ``count`` search requests, native and in the billing currency.

    ``price`` is None exactly when the searches are unpriced; every amount is
    then zero. ``fx_rate`` is None when no conversion was needed.
    """

    price: SearchPrice | None
    native: Decimal
    native_currency: str | None
    billed: Decimal
    fx_rate: Decimal | None

    @classmethod
    def unpriced(cls) -> SearchQuote:
        return cls(
            price=None, native=Decimal(0), native_currency=None, billed=Decimal(0), fx_rate=None
        )


def select_search_price(
    prices: list[SearchPrice], *, tier: str | None = None, at: datetime | None = None
) -> SearchPrice | None:
    """The price in force at ``at``: the tier's own when it has one, else the backend's.

    Append-only and effective-dated like ``select_price``: the most recent row
    that had already taken effect, rows dated in the future ignored. A tier
    override wins only when one is in force; a tier with no override of its own
    takes the backend-wide price rather than going unpriced.
    """
    moment = at or utcnow()
    applicable = [price for price in prices if price.effective_from <= moment]
    if tier:
        own = [price for price in applicable if price.tier == tier]
        if own:
            return max(own, key=lambda price: price.effective_from)
    default = [price for price in applicable if not price.tier]
    if not default:
        return None
    return max(default, key=lambda price: price.effective_from)


async def load_search_prices(session: AsyncSession, backend: str) -> list[SearchPrice]:
    rows = await session.execute(select(SearchPrice).where(SearchPrice.backend == backend))
    return list(rows.scalars().all())


async def quote_search(
    session: AsyncSession,
    *,
    fx: FXService | None,
    backend: str,
    tier: str | None,
    count: int,
    billing_currency: str,
    at: datetime | None = None,
) -> SearchQuote:
    """Price ``count`` requests to ``backend`` at the moment ``at``.

    A price in another currency converts at the day's rate (ADR 0054). When no
    rate is available the searches are recorded **unpriced** and the failure is
    logged, never billed at a guessed rate, which is what a model priced in a
    foreign currency does in the same position.
    """
    if count <= 0:
        return SearchQuote.unpriced()
    price = select_search_price(await load_search_prices(session, backend), tier=tier, at=at)
    if price is None:
        return SearchQuote.unpriced()
    native = Decimal(count) * Decimal(price.per_request)
    currency = price.currency.upper()
    if currency == billing_currency.upper():
        return SearchQuote(
            price=price, native=native, native_currency=currency, billed=native, fx_rate=None
        )
    quote = await fx.rate(currency, billing_currency) if fx is not None else None
    if quote is None:
        logger.error(
            "search backend %s is priced in %s but no %s->%s rate is available; "
            "recording the searches unpriced",
            backend,
            currency,
            currency,
            billing_currency,
        )
        return SearchQuote.unpriced()
    return SearchQuote(
        price=price,
        native=native,
        native_currency=currency,
        billed=native * quote.rate,
        fx_rate=quote.rate,
    )
