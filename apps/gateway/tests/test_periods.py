"""Calendar period boundaries.

This is the function both reporting and calendar quotas depend on, so a bug here
misallocates spend in two places at once and they still agree with each other. The
DST cases are the point: Europe/Rome shifts twice a year, so a month is not a fixed
number of hours and anything built by adding seconds drifts.
"""

from __future__ import annotations

import itertools
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest
from gateway.periods import (
    PeriodError,
    PeriodKind,
    explicit_range,
    parse_period,
    period_containing,
)

ROME = ZoneInfo("Europe/Rome")


def rome(text: str) -> datetime:
    """A local Rome wall-clock time, as UTC."""
    return datetime.fromisoformat(text).replace(tzinfo=ROME).astimezone(UTC)


class TestMonth:
    def test_starts_at_local_midnight_not_utc(self) -> None:
        """Rome is UTC+1 in winter, so January starts at 23:00 UTC on 31 December."""
        period = parse_period("2026-01")
        assert period.start == datetime(2025, 12, 31, 23, 0, tzinfo=UTC)
        assert period.end == datetime(2026, 1, 31, 23, 0, tzinfo=UTC)

    def test_summer_month_is_utc_plus_two(self) -> None:
        period = parse_period("2026-07")
        assert period.start == datetime(2026, 6, 30, 22, 0, tzinfo=UTC)
        assert period.end == datetime(2026, 7, 31, 22, 0, tzinfo=UTC)

    def test_utc_timezone_gives_utc_boundaries(self) -> None:
        period = parse_period("2026-01", timezone="UTC")
        assert period.start == datetime(2026, 1, 1, 0, 0, tzinfo=UTC)

    def test_december_rolls_the_year(self) -> None:
        period = parse_period("2026-12")
        assert period.end == rome("2027-01-01T00:00")

    def test_period_containing_a_moment(self) -> None:
        period = period_containing(PeriodKind.MONTH, rome("2026-08-15T12:00"))
        assert period.label == "2026-08"
        assert period.contains(rome("2026-08-01T00:00"))
        assert period.contains(rome("2026-08-31T23:59"))
        # Half-open: the first instant of September belongs to September.
        assert not period.contains(rome("2026-09-01T00:00"))


class TestDaylightSaving:
    """March and October, where naive arithmetic goes wrong."""

    def test_the_march_transition_month_is_one_hour_short(self) -> None:
        """Rome springs forward on the last Sunday of March 2026 (the 29th)."""
        march = parse_period("2026-03")
        hours = (march.end - march.start).total_seconds() / 3600
        assert hours == 31 * 24 - 1, f"expected 743 hours, got {hours}"

    def test_the_october_transition_month_is_one_hour_long(self) -> None:
        october = parse_period("2026-10")
        hours = (october.end - october.start).total_seconds() / 3600
        assert hours == 31 * 24 + 1, f"expected 745 hours, got {hours}"

    def test_boundaries_still_land_on_local_midnight_across_the_shift(self) -> None:
        for name in ("2026-03", "2026-04", "2026-10", "2026-11"):
            period = parse_period(name)
            for edge in (period.start, period.end):
                local = edge.astimezone(ROME)
                assert (local.hour, local.minute) == (0, 0), f"{name} {edge} -> {local}"

    def test_consecutive_months_meet_exactly(self) -> None:
        """No gap and no overlap, or spend falls between two reports."""
        for first, second in [
            ("2026-02", "2026-03"),
            ("2026-03", "2026-04"),
            ("2026-09", "2026-10"),
            ("2026-10", "2026-11"),
        ]:
            assert parse_period(first).end == parse_period(second).start

    def test_a_transition_day_is_23_or_25_hours(self) -> None:
        spring = parse_period("2026-03-29")
        autumn = parse_period("2026-10-25")
        assert (spring.end - spring.start) == timedelta(hours=23)
        assert (autumn.end - autumn.start) == timedelta(hours=25)


class TestQuarterAndYear:
    def test_calendar_quarters(self) -> None:
        """January-December, per the organisation's accounting."""
        assert parse_period("2026-Q1").start == rome("2026-01-01T00:00")
        assert parse_period("2026-Q1").end == rome("2026-04-01T00:00")
        assert parse_period("2026-Q3").start == rome("2026-07-01T00:00")
        assert parse_period("2026-Q4").end == rome("2027-01-01T00:00")

    def test_quarters_tile_the_year_without_gaps(self) -> None:
        year = parse_period("2026")
        quarters = [parse_period(f"2026-Q{n}") for n in (1, 2, 3, 4)]
        assert quarters[0].start == year.start
        assert quarters[-1].end == year.end
        for earlier, later in itertools.pairwise(quarters):
            assert earlier.end == later.start

    def test_quarter_containing(self) -> None:
        assert period_containing(PeriodKind.QUARTER, rome("2026-05-04T09:00")).label == "2026-Q2"

    def test_year(self) -> None:
        period = parse_period("2026")
        assert period.start == rome("2026-01-01T00:00")
        assert period.end == rome("2027-01-01T00:00")


class TestWeekAndDay:
    def test_weeks_start_on_monday(self) -> None:
        period = period_containing(PeriodKind.WEEK, rome("2026-08-15T12:00"))
        assert period.start.astimezone(ROME).weekday() == 0
        assert (period.end - period.start) == timedelta(days=7)

    def test_iso_week_round_trip(self) -> None:
        containing = period_containing(PeriodKind.WEEK, rome("2026-08-15T12:00"))
        assert parse_period(containing.label).start == containing.start

    def test_day(self) -> None:
        period = parse_period("2026-08-15")
        assert period.start == rome("2026-08-15T00:00")
        assert period.end == rome("2026-08-16T00:00")


class TestParsing:
    @pytest.mark.parametrize(
        "name,kind",
        [
            ("2026-08", PeriodKind.MONTH),
            ("2026-Q3", PeriodKind.QUARTER),
            ("2026-q3", PeriodKind.QUARTER),
            ("2026", PeriodKind.YEAR),
            ("2026-W33", PeriodKind.WEEK),
            ("2026-08-15", PeriodKind.DAY),
            ("  2026-08  ", PeriodKind.MONTH),
        ],
    )
    def test_accepted_forms(self, name: str, kind: PeriodKind) -> None:
        assert parse_period(name).kind is kind

    @pytest.mark.parametrize(
        "name", ["", "August", "2026-13", "2026-00", "2026-Q5", "2026-02-30", "26-08", "2026-W99"]
    )
    def test_rejected_forms(self, name: str) -> None:
        with pytest.raises(PeriodError):
            parse_period(name)

    def test_the_error_says_what_is_accepted(self) -> None:
        with pytest.raises(PeriodError) as caught:
            parse_period("last month")
        assert "2026-08" in str(caught.value)

    def test_unknown_timezone_is_refused(self) -> None:
        with pytest.raises(PeriodError):
            parse_period("2026-08", timezone="Mars/Olympus_Mons")


class TestRangeAndHelpers:
    def test_explicit_range(self) -> None:
        period = explicit_range(rome("2026-08-01T00:00"), rome("2026-08-08T00:00"))
        assert period.kind is None
        assert (period.end - period.start) == timedelta(days=7)

    def test_a_backwards_range_is_refused(self) -> None:
        with pytest.raises(PeriodError):
            explicit_range(rome("2026-08-08T00:00"), rome("2026-08-01T00:00"))

    def test_seconds_remaining_is_exact_not_estimated(self) -> None:
        """Unlike a rolling window, a calendar period knows exactly when it ends."""
        period = parse_period("2026-08")
        now = period.end - timedelta(hours=12)
        assert period.seconds_remaining(now) == 12 * 3600

    def test_seconds_remaining_never_goes_negative(self) -> None:
        period = parse_period("2026-08")
        assert period.seconds_remaining(period.end + timedelta(days=1)) == 0

    def test_periods_are_hashable_and_comparable(self) -> None:
        assert parse_period("2026-08") == parse_period("2026-08")
        assert isinstance(hash(parse_period("2026-08")), int)
        assert parse_period("2026-08") != parse_period("2026-09")
