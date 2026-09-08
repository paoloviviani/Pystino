"""Foreign-exchange rates for pricing that is not in the billing currency.

See ADR 0054.

The policy the operator chose, precisely scoped:

* A model **priced in USD keeps its USD prices**, and its usage rows keep the
  native figure (`cost_native`/`cost_currency`) — the per-provider and
  per-model breakdowns report what the counterparty actually charges.
* Conversion applies where an **immediate decision or an aggregate** needs one
  currency: quota admission (a usage estimate compared against a limit set in
  the billing currency) and the aggregate figures in reports and overviews.
* The rate is **fetched once per day** from the ECB reference rates via
  frankfurter.dev (no key, no registration), persisted in the database, and
  served from memory on the hot path. If the API does not answer, the **last
  known rate** is used — a stale rate is a small, visible imprecision; a
  refused request is an outage.

Rates are never invented: with no stored rate and no reachable API there is
no conversion, and the caller decides what that means (admission refuses a
USD-priced model rather than bill it as if it were euros).
"""

import asyncio
import contextlib
import logging
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal

import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from gateway.config import Settings
from gateway.models import FXRate as FXRateRow
from gateway.types import utcnow

logger = logging.getLogger(__name__)

#: ECB reference rates, via frankfurter.dev: no key, HTTP, daily publication.
_DEFAULT_API = "https://api.frankfurter.dev/v1/latest"

 #: One fetch per day per pair; the rate is the whole day's decision input.
_REFRESH_SECONDS = 3600.0


@dataclass(frozen=True)
class FXQuote:
    base: str
    quote: str
    rate: Decimal  # quote units per one base unit
    as_of: datetime
    source: str  # "api" | "cache"


class FXService:
    """Daily USD/EUR (and any pair) rates: fetched, persisted, remembered."""

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        settings: Settings,
        *,
        api_url: str = _DEFAULT_API,
        http: httpx.AsyncClient | None = None,
        refresh_seconds: float = _REFRESH_SECONDS,
    ) -> None:
        self._session_factory = session_factory
        self._settings = settings
        self._api_url = api_url
        self._http = http or httpx.AsyncClient(timeout=httpx.Timeout(10.0))
        self._own_http = http is None
        self._refresh_seconds = refresh_seconds
        # (base, quote) -> rate. On the hot path this is the whole read: the
        # database is touched at most once per day per pair, and only by the
        # poller or the first request after a restart.
        self._memory: dict[tuple[str, str], tuple[Decimal, str]] = {}
        self._task: asyncio.Task[None] | None = None

    async def close(self) -> None:
        if self._own_http:
            with contextlib.suppress(Exception):
                await self._http.aclose()

    @property
    def refresh_seconds(self) -> float:
        return self._refresh_seconds

    def start(self) -> None:
        self._task = asyncio.create_task(self._run())

    async def refresh_once(self) -> None:
        """Fetch every pair the catalogue needs, once. Exposed for startup."""
        for base, quote in await self._needed_pairs():
            await self.rate(base, quote)

    async def _needed_pairs(self) -> list[tuple[str, str]]:
        """The pairs the catalogue's price currencies imply, against the
        billing currency. Computed rather than configured: a new USD-priced
        import starts being convertible with no settings change."""
        billing = self._settings.billing_currency.upper()
        async with self._session_factory() as session:
            from gateway.models import ModelPrice

            rows = await session.execute(select(ModelPrice.currency).distinct())
            currencies = {str(currency).upper() for currency in rows.scalars().all()}
        return [(c, billing) for c in sorted(currencies) if c != billing]

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
            self._task = None

    async def cached_rate(self, base: str, quote: str) -> FXQuote | None:
        """The rate from memory or the database — no network. Never fails.

        Admission calls this for every USD-priced request, so everything here
        is a dictionary lookup after the first fetch of the day.
        """
        base, quote = base.upper(), quote.upper()
        if base == quote:
            return FXQuote(base, quote, Decimal(1), utcnow(), "parity")
        today = utcnow().date().isoformat()
        cached = self._memory.get((base, quote))
        if cached is not None and cached[1] == today:
            return FXQuote(base, quote, cached[0], utcnow(), "memory")
        async with self._session_factory() as session:
            row = (
                await session.execute(
                    select(FXRateRow).where(FXRateRow.base == base, FXRateRow.quote == quote)
                )
            ).scalar_one_or_none()
        if row is None:
            return None
        self._memory[(base, quote)] = (row.rate, row.fetched_at.date().isoformat())
        return FXQuote(base, quote, row.rate, row.fetched_at, "cache")

    async def rate(self, base: str, quote: str) -> FXQuote | None:
        """The rate for the pair, fetching today's if not already held.

        Fetch failure falls back to the last known rate, however old — and the
        returned ``source`` says so, because a decision made on yesterday's
        rate is a decision someone may want to know about.
        """
        base, quote = base.upper(), quote.upper()
        rate = await self.cached_rate(base, quote)
        if rate is not None and rate.as_of.date().isoformat() == utcnow().date().isoformat():
            return rate

        fresh = await self._fetch(base, quote)
        if fresh is not None:
            await self._persist(fresh)
            self._memory[(base, quote)] = (fresh.rate, fresh.as_of.date().isoformat())
            return fresh

        return rate  # last known, however old; None if there never was one

    async def _fetch(self, base: str, quote: str) -> FXQuote | None:
        try:
            response = await self._http.get(
                self._api_url, params={"base": base, "symbols": quote}
            )
            response.raise_for_status()
            document = response.json()
            rate = document["rates"][quote]
            as_of = document.get("date") or utcnow().date().isoformat()
        except Exception:
            logger.warning("fx fetch %s->%s failed; keeping the last known rate", base, quote)
            return None
        return FXQuote(
            base=base,
            quote=quote,
            rate=Decimal(str(rate)),
            as_of=datetime.fromisoformat(as_of).replace(tzinfo=UTC),
            source="api",
        )

    async def _persist(self, rate: FXQuote) -> None:
        async with self._session_factory() as session:
            row = (
                await session.execute(
                    select(FXRateRow).where(
                        FXRateRow.base == rate.base, FXRateRow.quote == rate.quote
                    )
                )
            ).scalar_one_or_none()
            if row is None:
                row = FXRateRow(id=uuid.uuid4(), base=rate.base, quote=rate.quote)
                session.add(row)
            row.rate = rate.rate
            row.fetched_at = rate.as_of
            await session.commit()

    async def _run(self) -> None:
        """Keep the day's rates warm, so no request ever waits on the API."""
        while True:
            try:
                for base, quote in await self._needed_pairs():
                    await self.rate(base, quote)
            except Exception:
                logger.exception("fx refresh failed")
            await asyncio.sleep(self._refresh_seconds)
