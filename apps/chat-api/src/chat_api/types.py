"""Small shared types."""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import DateTime
from sqlalchemy.engine.interfaces import Dialect
from sqlalchemy.types import TypeDecorator


def utcnow() -> datetime:
    """Timezone-aware UTC. Never ``datetime.utcnow()``, which returns naive."""
    return datetime.now(UTC)


class TZDateTime(TypeDecorator[datetime]):
    """A ``DateTime`` that is always timezone-aware UTC in Python.

    PostgreSQL ``timestamptz`` round-trips an offset; SQLite has no timezone
    concept and hands back naive datetimes, which raise TypeError the moment
    they meet an aware one. The unit suite runs on SQLite and the deployment on
    PostgreSQL, so without this the tests fail on a comparison the production
    database would have got right — which is the wrong way round for a test to
    be wrong.

    The same decorator exists in the gateway. Copied rather than shared: this
    service imports nothing from it.
    """

    impl = DateTime(timezone=True)
    cache_ok = True

    def process_bind_param(self, value: datetime | None, dialect: Dialect) -> datetime | None:
        if value is None:
            return None
        if value.tzinfo is None:
            raise ValueError(
                f"naive datetime passed to a TZDateTime column: {value!r}. Use utcnow()."
            )
        return value.astimezone(UTC)

    def process_result_value(self, value: datetime | None, dialect: Dialect) -> datetime | None:
        if value is None:
            return None
        return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)
