"""Column types shared by the ORM.

Two things here are load-bearing for correctness:

* **Money** is ``Numeric``, never float. Cost is summed against monetary quotas,
  and binary floating point cannot represent 0.01.
* **TZDateTime** guarantees every datetime crossing the ORM boundary is
  timezone-aware UTC. Rolling-window quota arithmetic on a mix of naive and
  aware datetimes fails in ways that only show up under load, near midnight, or
  in another timezone.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import DateTime, Dialect, Numeric
from sqlalchemy.types import TypeDecorator

# 24 digits total, 12 after the point. Per-token prices run to ~1e-9 per token
# and a single request's cost to ~1e0, so 12 decimal places leaves headroom
# without ever needing to round mid-calculation.
Money = Numeric(24, 12, asdecimal=True)

# Fractional tokens do not exist, but per-million-token prices are fractional, so
# prices use Money and token counts stay integers.
MONEY_QUANTUM = Decimal("0.000000000001")


def utcnow() -> datetime:
    """Timezone-aware current time.

    Always use this rather than ``datetime.utcnow()``, which returns a naive
    datetime and is deprecated.
    """
    return datetime.now(UTC)


class TZDateTime(TypeDecorator[datetime]):
    """A ``DateTime`` that is always timezone-aware UTC in Python.

    PostgreSQL ``timestamptz`` already round-trips an offset. SQLite has no
    timezone concept and hands back naive datetimes, which would silently compare
    as "earlier" than aware ones and raise TypeError on subtraction. This
    normalises both directions so application code never has to care.
    """

    impl = DateTime(timezone=True)
    cache_ok = True

    def process_bind_param(self, value: datetime | None, dialect: Dialect) -> datetime | None:
        if value is None:
            return None
        if value.tzinfo is None:
            raise ValueError(
                f"naive datetime passed to a TZDateTime column: {value!r}. "
                "Use gateway.types.utcnow()."
            )
        return value.astimezone(UTC)

    def process_result_value(self, value: datetime | None, dialect: Dialect) -> datetime | None:
        if value is None:
            return None
        if value.tzinfo is None:
            return value.replace(tzinfo=UTC)
        return value.astimezone(UTC)


def as_decimal(value: Any) -> Decimal:
    """Coerce to Decimal without ever going through float.

    ``Decimal(0.1)`` is 0.1000000000000000055511151231257827. Passing a float
    into money arithmetic is the classic way to introduce cent-level drift, so
    floats are routed through their exact string form.
    """
    if isinstance(value, Decimal):
        return value
    if isinstance(value, float):
        return Decimal(repr(value))
    return Decimal(value)
