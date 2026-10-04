"""Calendar period boundaries.

**The single implementation of "when does August start".** Reporting uses it
(ADR 0024) and calendar quotas use it
(ADR 0025), which is what makes a monthly
budget and a monthly report agree by construction rather than by vigilance.

Two things make this less trivial than it looks:

* **Boundaries are local, not UTC.** An organisation's January starts at midnight in
  its own timezone. Computing it in UTC misallocates an hour of spend at each end of
  every month, silently.
* **DST.** Europe/Rome shifts twice a year, so a month is not a fixed number of
  hours and naive arithmetic drifts. Every boundary here is built by constructing a
  *local* wall-clock datetime and then converting, never by adding seconds.

Everything is pure and takes an explicit clock, so the awkward cases are testable
without waiting for March.
"""

from __future__ import annotations

import enum
import re
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from zoneinfo import ZoneInfo


class PeriodKind(enum.StrEnum):
    DAY = "day"
    WEEK = "week"
    MONTH = "month"
    QUARTER = "quarter"
    YEAR = "year"


# Calendar quarters and a January-December year, per the organisation's accounting.
# A fiscal year starting elsewhere would be a config field here, not a migration.
_QUARTER_START_MONTH = {1: 1, 2: 4, 3: 7, 4: 10}

_MONTH_RE = re.compile(r"^(\d{4})-(\d{2})$")
_QUARTER_RE = re.compile(r"^(\d{4})-Q([1-4])$", re.IGNORECASE)
_YEAR_RE = re.compile(r"^(\d{4})$")
_WEEK_RE = re.compile(r"^(\d{4})-W(\d{2})$", re.IGNORECASE)
_DAY_RE = re.compile(r"^(\d{4})-(\d{2})-(\d{2})$")


class PeriodError(ValueError):
    """A period name that cannot be resolved."""


@dataclass(frozen=True, slots=True)
class Period:
    """A half-open ``[start, end)`` range in UTC, with its human label."""

    kind: PeriodKind | None
    label: str
    start: datetime
    end: datetime

    def contains(self, moment: datetime) -> bool:
        return self.start <= moment < self.end

    def seconds_remaining(self, now: datetime) -> int:
        """Whole seconds until this period ends, never negative.

        Used for the `Retry-After` on a calendar quota, where — unlike a rolling
        window — the answer is exact rather than estimated.
        """
        return max(0, int((self.end - now).total_seconds()))


def _zone(timezone: str) -> ZoneInfo:
    try:
        return ZoneInfo(timezone)
    except Exception as exc:
        raise PeriodError(f"unknown timezone {timezone!r}") from exc


def _local_midnight(day: date, zone: ZoneInfo) -> datetime:
    """Midnight local time on *day*, as an aware UTC datetime.

    Built from a wall-clock date rather than by arithmetic, so a DST transition
    inside the period cannot shift the boundary.
    """
    return datetime(day.year, day.month, day.day, tzinfo=zone).astimezone(UTC)


def _add_months(day: date, months: int) -> date:
    total = (day.year * 12 + day.month - 1) + months
    year, month = divmod(total, 12)
    return date(year, month + 1, 1)


def period_containing(
    kind: PeriodKind, moment: datetime, *, timezone: str = "Europe/Rome"
) -> Period:
    """The period of *kind* that contains *moment*."""
    zone = _zone(timezone)
    local = moment.astimezone(zone)
    today = local.date()

    match kind:
        case PeriodKind.DAY:
            start_day, end_day = today, today + timedelta(days=1)
            label = today.isoformat()
        case PeriodKind.WEEK:
            # ISO 8601: weeks start on Monday, which is also what Cortecs uses.
            start_day = today - timedelta(days=today.weekday())
            end_day = start_day + timedelta(days=7)
            iso = start_day.isocalendar()
            label = f"{iso.year}-W{iso.week:02d}"
        case PeriodKind.MONTH:
            start_day = today.replace(day=1)
            end_day = _add_months(start_day, 1)
            label = f"{start_day.year}-{start_day.month:02d}"
        case PeriodKind.QUARTER:
            quarter = (today.month - 1) // 3 + 1
            start_day = date(today.year, _QUARTER_START_MONTH[quarter], 1)
            end_day = _add_months(start_day, 3)
            label = f"{start_day.year}-Q{quarter}"
        case PeriodKind.YEAR:
            start_day = date(today.year, 1, 1)
            end_day = date(today.year + 1, 1, 1)
            label = str(today.year)

    return Period(
        kind=kind,
        label=label,
        start=_local_midnight(start_day, zone),
        end=_local_midnight(end_day, zone),
    )


def parse_period(name: str, *, timezone: str = "Europe/Rome") -> Period:
    """Resolve a named period: ``2026-08``, ``2026-Q3``, ``2026``, ``2026-W33``, ``2026-08-15``.

    Named rather than relative, so a report for last month returns the same numbers
    whenever it is run.
    """
    zone = _zone(timezone)
    text = name.strip()

    if match := _MONTH_RE.match(text):
        year, month = int(match.group(1)), int(match.group(2))
        if not 1 <= month <= 12:
            raise PeriodError(f"{name!r} is not a real month")
        start_day = date(year, month, 1)
        return Period(
            PeriodKind.MONTH,
            f"{year}-{month:02d}",
            _local_midnight(start_day, zone),
            _local_midnight(_add_months(start_day, 1), zone),
        )

    if match := _QUARTER_RE.match(text):
        year, quarter = int(match.group(1)), int(match.group(2))
        start_day = date(year, _QUARTER_START_MONTH[quarter], 1)
        return Period(
            PeriodKind.QUARTER,
            f"{year}-Q{quarter}",
            _local_midnight(start_day, zone),
            _local_midnight(_add_months(start_day, 3), zone),
        )

    if match := _DAY_RE.match(text):
        try:
            day = date(int(match.group(1)), int(match.group(2)), int(match.group(3)))
        except ValueError as exc:
            raise PeriodError(f"{name!r} is not a real date") from exc
        return Period(
            PeriodKind.DAY,
            day.isoformat(),
            _local_midnight(day, zone),
            _local_midnight(day + timedelta(days=1), zone),
        )

    if match := _WEEK_RE.match(text):
        year, week = int(match.group(1)), int(match.group(2))
        try:
            start_day = date.fromisocalendar(year, week, 1)
        except ValueError as exc:
            raise PeriodError(f"{name!r} is not a real ISO week") from exc
        return Period(
            PeriodKind.WEEK,
            f"{year}-W{week:02d}",
            _local_midnight(start_day, zone),
            _local_midnight(start_day + timedelta(days=7), zone),
        )

    if match := _YEAR_RE.match(text):
        year = int(match.group(1))
        return Period(
            PeriodKind.YEAR,
            str(year),
            _local_midnight(date(year, 1, 1), zone),
            _local_midnight(date(year + 1, 1, 1), zone),
        )

    raise PeriodError(
        f"{name!r} is not a period. Expected one of: 2026-08, 2026-Q3, 2026, 2026-W33, 2026-08-15."
    )


def explicit_range(start: datetime, end: datetime) -> Period:
    """An arbitrary ``[start, end)`` range, for reports that are not a named period."""
    if start >= end:
        raise PeriodError("the start of a range must be before its end")
    return Period(
        kind=None,
        label=f"{start.isoformat()}..{end.isoformat()}",
        start=start.astimezone(UTC),
        end=end.astimezone(UTC),
    )
