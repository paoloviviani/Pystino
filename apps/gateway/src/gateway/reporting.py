"""Usage reporting: what was spent, by whom, over a calendar period.

Reads ``usage_records`` — the ledger — and nothing else. Quota counters are a
cache and can be reset, rebuilt or lost; a chargeback report may not depend on
anything with those properties. That separation is the whole reason a quota reset
leaves the report untouched (ADR 0025).

Period boundaries come from :mod:`gateway.periods`, the same function calendar
quotas use, so "August" means one thing in this codebase (ADR 0024).

Three disclosures are computed rather than left implicit, because each one is a way
a total can be misread:

* **estimated** — the provider did not return token counts and we inferred them.
* **unavailable** — usage could not be determined at all; the cost is a floor.
* **in flight** — requests still streaming are excluded entirely.
"""

from __future__ import annotations

import csv
import io
import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from enum import StrEnum
from typing import Any, Literal

from sqlalchemy import ColumnElement, Select, case, func, literal, select
from sqlalchemy.ext.asyncio import AsyncSession

from gateway.errors import BadRequestError
from gateway.models import (
    ApiKey,
    CostSource,
    Group,
    ModelDef,
    Provider,
    UsageRecord,
    UsageSource,
    UsageStatus,
    User,
)
from gateway.periods import (
    Period,
    PeriodError,
    PeriodKind,
    explicit_range,
    parse_period,
    period_containing,
)
from gateway.schemas import BillingDrift, PeriodResponse, UsageReport, UsageReportRow
from gateway.types import format_money_prose, utcnow


class GroupBy(StrEnum):
    GROUP = "group"
    USER = "user"
    MODEL = "model"
    API_KEY = "api_key"
    DAY = "day"
    TOTAL = "total"


# What a null grouping key means, per dimension. A request with no group is not
# the same thing as a request whose group was deleted, and both are different from
# a total: saying so keeps unattributed spend visible instead of dropping the row.
_NO_KEY_LABEL = {
    GroupBy.GROUP: "(no group)",
    GroupBy.USER: "(no user)",
    GroupBy.MODEL: "(unknown model)",
    GroupBy.API_KEY: "(no API key — browser session)",
    GroupBy.DAY: "(no date)",
    GroupBy.TOTAL: "all usage",
}

_DELETED_LABEL = {
    GroupBy.GROUP: "(deleted group)",
    GroupBy.USER: "(erased user)",
    GroupBy.API_KEY: "(deleted key)",
}


GroupByParam = Literal["group", "user", "model", "api_key", "day", "total"]


def resolve_period(
    name: str, start: datetime | None, end: datetime | None, timezone: str
) -> Period:
    """The period a report request asks for.

    Named periods are preferred and are the default: ``2026-08`` means the same
    thing whenever it is run, whereas "the last 30 days" produces a different
    answer every time and cannot be reconciled with an invoice.
    """
    if name and (start or end):
        raise BadRequestError(
            "Give either a named period or a start/end range, not both.",
            code="ambiguous_period",
        )
    try:
        if start or end:
            if not (start and end):
                raise BadRequestError("A range needs both start and end.", code="incomplete_range")
            return explicit_range(start, end)
        if name:
            return parse_period(name, timezone=timezone)
        return period_containing(PeriodKind.MONTH, utcnow(), timezone=timezone)
    except PeriodError as exc:
        raise BadRequestError(str(exc), code="invalid_period") from exc


@dataclass(frozen=True, slots=True)
class ReportFilter:
    """Which slice of the ledger to report on.

    ``user_id`` is what makes the same code serve both surfaces: the admin report
    leaves it unset, and ``/api/me`` pins it to the caller.
    """

    period: Period
    group_by: GroupBy = GroupBy.GROUP
    group_id: uuid.UUID | None = None
    user_id: uuid.UUID | None = None
    api_key_id: uuid.UUID | None = None
    model_name: str | None = None


def _day_expression(timezone: str, dialect: str) -> ColumnElement[Any]:
    """The local calendar day of each record, as text.

    Grouping by UTC day would put an hour or two of every evening's spend on the
    wrong day — obvious to anyone comparing a daily chart against a monthly total
    they already trust, since the month is computed in local time.

    Dispatched on the dialect rather than expressed portably: there is no portable
    spelling. PostgreSQL has a real timezone database and does this correctly
    including across DST. SQLite (tests only) does not, so it groups by UTC day;
    accepted because no deployment reports from SQLite.
    """
    if dialect == "postgresql":
        return func.to_char(func.timezone(timezone, UsageRecord.created_at), "YYYY-MM-DD")
    return func.strftime("%Y-%m-%d", UsageRecord.created_at)


def _key_column(group_by: GroupBy, timezone: str, dialect: str) -> Any:
    match group_by:
        case GroupBy.GROUP:
            return UsageRecord.group_id
        case GroupBy.USER:
            return UsageRecord.user_id
        case GroupBy.MODEL:
            return UsageRecord.model_name
        case GroupBy.API_KEY:
            return UsageRecord.api_key_id
        case GroupBy.DAY:
            return _day_expression(timezone, dialect)
        case GroupBy.TOTAL:
            # A constant, so the aggregate query keeps exactly one shape.
            return literal("total")


def _base_query(filters: ReportFilter, timezone: str, dialect: str) -> Select[Any]:
    key = _key_column(filters.group_by, timezone, dialect).label("key")
    query = select(
        key,
        func.count(UsageRecord.id).label("requests"),
        func.coalesce(func.sum(UsageRecord.prompt_tokens), 0).label("prompt_tokens"),
        func.coalesce(func.sum(UsageRecord.completion_tokens), 0).label("completion_tokens"),
        func.coalesce(func.sum(UsageRecord.total_tokens), 0).label("total_tokens"),
        func.coalesce(func.sum(UsageRecord.image_count), 0).label("images"),
        func.coalesce(func.sum(UsageRecord.search_count), 0).label("searches"),
        func.coalesce(func.sum(UsageRecord.cost), 0).label("cost"),
        func.coalesce(func.sum(UsageRecord.cost_native), 0).label("native_cost"),
        func.min(UsageRecord.cost_currency).label("native_currency"),
        func.coalesce(
            func.sum(case((UsageRecord.usage_source == UsageSource.ESTIMATED, 1), else_=0)), 0
        ).label("estimated"),
        func.coalesce(
            func.sum(case((UsageRecord.usage_source == UsageSource.UNAVAILABLE, 1), else_=0)), 0
        ).label("unavailable"),
    ).where(
        # Half-open, matching Period and the quota engine exactly. A record at
        # 00:00:00 on 1 September belongs to September, in both places.
        UsageRecord.created_at >= filters.period.start,
        UsageRecord.created_at < filters.period.end,
        # In-flight requests have no final usage yet. Counting them would make a
        # report change value while you are reading it.
        UsageRecord.status != UsageStatus.IN_PROGRESS,
    )

    if filters.group_id is not None:
        query = query.where(UsageRecord.group_id == filters.group_id)
    if filters.user_id is not None:
        query = query.where(UsageRecord.user_id == filters.user_id)
    if filters.api_key_id is not None:
        query = query.where(UsageRecord.api_key_id == filters.api_key_id)
    if filters.model_name is not None:
        query = query.where(UsageRecord.model_name == filters.model_name)

    return query.group_by(key)


async def _labels(session: AsyncSession, group_by: GroupBy, keys: Sequence[Any]) -> dict[Any, str]:
    """Human names for the grouping keys present in this report.

    One query for the keys actually returned, not a join: a left join would
    multiply rows if a name were ever non-unique, and the label is presentation
    rather than part of the aggregate.
    """
    ids = [key for key in keys if isinstance(key, uuid.UUID)]
    if not ids:
        return {}

    match group_by:
        case GroupBy.GROUP:
            rows = (
                await session.execute(select(Group.id, Group.name).where(Group.id.in_(ids)))
            ).all()
        case GroupBy.USER:
            rows = (
                await session.execute(
                    select(User.id, func.coalesce(User.email, User.subject)).where(User.id.in_(ids))
                )
            ).all()
        case GroupBy.API_KEY:
            rows = (
                await session.execute(
                    select(
                        ApiKey.id,
                        # A key's name is optional; its prefix always identifies it.
                        case((ApiKey.name != "", ApiKey.name), else_=ApiKey.prefix),
                    ).where(ApiKey.id.in_(ids))
                )
            ).all()
        case _:
            return {}
    return {row[0]: row[1] for row in rows}


def _row(
    group_by: GroupBy, key: Any, labels: dict[Any, str], values: Sequence[Any]
) -> UsageReportRow:
    (
        requests,
        prompt,
        completion,
        total,
        images,
        searches,
        cost,
        native_cost,
        native_currency,
        estimated,
        unavailable,
    ) = values
    if key is None:
        label = _NO_KEY_LABEL[group_by]
    else:
        # A key with no label is a subject that has since been deleted. The spend
        # stays in the report — dropping it would silently change last month's
        # total when someone leaves the foundation.
        label = labels.get(key) or (
            _DELETED_LABEL.get(group_by, str(key)) if isinstance(key, uuid.UUID) else str(key)
        )
    # The native figure is only *reportable* for a model row: a model's rows
    # share one price currency, so its native sum means something. A group,
    # user or day mixes currencies and reports in the billing currency only.
    show_native = group_by == GroupBy.MODEL and native_currency is not None
    return UsageReportRow(
        key=str(key) if key is not None else None,
        label=label,
        requests=int(requests or 0),
        prompt_tokens=int(prompt or 0),
        completion_tokens=int(completion or 0),
        total_tokens=int(total or 0),
        images=int(images or 0),
        searches=int(searches or 0),
        cost=Decimal(str(cost or 0)),
        native_cost=Decimal(str(native_cost or 0)) if show_native else None,
        native_currency=native_currency if show_native else None,
        estimated_requests=int(estimated or 0),
        unavailable_requests=int(unavailable or 0),
    )


def _totals(rows: Sequence[UsageReportRow]) -> UsageReportRow:
    return UsageReportRow(
        key=None,
        label="total",
        requests=sum(row.requests for row in rows),
        prompt_tokens=sum(row.prompt_tokens for row in rows),
        completion_tokens=sum(row.completion_tokens for row in rows),
        total_tokens=sum(row.total_tokens for row in rows),
        images=sum(row.images for row in rows),
        searches=sum(row.searches for row in rows),
        cost=sum((row.cost for row in rows), Decimal(0)),
        estimated_requests=sum(row.estimated_requests for row in rows),
        unavailable_requests=sum(row.unavailable_requests for row in rows),
    )


async def _reconciliation(
    session: AsyncSession, filters: ReportFilter, *, currency: str
) -> list[BillingDrift]:
    """Our cost against the provider's, one row per provider.

    Grouped by provider and never totalled, because a total is not a comparison:
    it adds requests whose provider reports a cost to requests whose provider
    does not, so the two sides cover different sets of requests and the result
    reads as a large discrepancy when nothing at all is wrong. That was a real
    bug — the fake upstream's spend was being weighed against the one real
    provider's figure.

    Only rows with a provider figure count, on **both** sides. Anything else
    would reintroduce the same mismatch inside a single provider: a request made
    before it named a plugin that reads its figure has our cost and not theirs.

    The provider comes from the model, since ``usage_records`` records the
    provider the *upstream* named rather than our row. A record whose model has
    since been deleted has no provider to attribute to and is left out; saying
    so is better than guessing, and it is visible as a gap between this and the
    report totals.
    """
    query = (
        select(
            Provider.name,
            func.count(UsageRecord.id),
            func.coalesce(func.sum(UsageRecord.cost), 0),
            func.coalesce(func.sum(UsageRecord.upstream_cost), 0),
            UsageRecord.upstream_cost_currency,
        )
        .join(ModelDef, ModelDef.id == UsageRecord.model_id)
        .join(Provider, Provider.id == ModelDef.provider_id)
        .where(
            UsageRecord.created_at >= filters.period.start,
            UsageRecord.created_at < filters.period.end,
            UsageRecord.status != UsageStatus.IN_PROGRESS,
            UsageRecord.upstream_cost.is_not(None),
        )
        .group_by(Provider.name, UsageRecord.upstream_cost_currency)
        .order_by(Provider.name)
    )
    if filters.group_id is not None:
        query = query.where(UsageRecord.group_id == filters.group_id)
    if filters.user_id is not None:
        query = query.where(UsageRecord.user_id == filters.user_id)
    if filters.api_key_id is not None:
        query = query.where(UsageRecord.api_key_id == filters.api_key_id)
    if filters.model_name is not None:
        query = query.where(UsageRecord.model_name == filters.model_name)

    return [
        BillingDrift(
            provider=name,
            requests=int(requests or 0),
            cost=Decimal(str(cost or 0)),
            currency=currency,
            upstream_cost=Decimal(str(upstream or 0)),
            upstream_currency=upstream_currency or currency,
        )
        for name, requests, cost, upstream, upstream_currency in (
            await session.execute(query)
        ).all()
    ]


def _disclosures(
    totals: UsageReportRow,
    in_flight: int,
    substituted: int = 0,
    reconciliation: Sequence[BillingDrift] | None = None,
    fell_back: int = 0,
    metering_enabled: bool = True,
) -> list[str]:
    notes: list[str] = []
    if not metering_enabled:
        # First, and on its own terms. An unmetered deployment writes no rows
        # at all, so the table below is empty for a reason that has nothing to
        # do with traffic — and "no usage this week" is precisely what an empty
        # report otherwise means. Saying it here is the same principle as
        # `own_prices_fallback` never being silent: a gap is reported as a gap.
        notes.append(
            "Metering is off for this deployment, so no usage is recorded and this "
            "report covers nothing. Requests are served and access is still "
            "enforced; only the ledger is absent."
        )
    if totals.estimated_requests:
        notes.append(
            f"{totals.estimated_requests} of {totals.requests} requests have estimated "
            "token counts: the provider did not report usage, so their cost is inferred."
        )
    if totals.unavailable_requests:
        notes.append(
            f"{totals.unavailable_requests} of {totals.requests} requests have no usage "
            "data: their cost is recorded as zero, so this total is a floor."
        )
    if in_flight:
        notes.append(
            f"{in_flight} request(s) were still in flight and are excluded; "
            "they will appear once complete."
        )
    if totals.images:
        # Otherwise an image row reads as a bug: real money against zero
        # tokens. Most image models are priced per picture and report no token
        # usage at all (ADR 0030).
        notes.append(
            f"{totals.images} image(s) were generated: image models are commonly priced "
            "per image, so they add cost without adding tokens."
        )
    if totals.searches:
        # Same reason as the image sentence, one step less obvious: this cost
        # lands on requests the caller thinks of as ordinary completions, so
        # without a line saying so the tokens simply look mispriced. And the
        # figure is a floor when the model carries no `per_search` rate — the
        # searches are recorded either way, which is what makes the gap
        # findable before the provider's invoice arrives (ADR 0058).
        notes.append(
            f"{totals.searches} provider-side web search(es) were billed on top of "
            "tokens. They are charged from each model's per-search rate; a model "
            "without one records the searches and charges nothing for them."
        )
    if fell_back:
        # Said at all because a pass-through deployment silently billing from
        # prices nobody maintains looks identical to one that is working. The
        # sentence itself only has to state the fact; the reason lives here.
        notes.append(
            f"{fell_back} request(s) are charged from our price table: their provider "
            "bills from its own reported cost and reported none."
        )
    for drift in reconciliation or ():
        # Rounded to milli-units, like every other figure a person reads. The
        # exact amounts stay on the `BillingDrift` model beside this sentence, so
        # an administrator reconciling against an invoice still has all twelve
        # decimal places — see `format_money_prose`.
        theirs = format_money_prose(drift.upstream_cost, drift.upstream_currency)
        ours = format_money_prose(drift.cost, drift.currency)
        # Printing the same rounded figure twice — "it reported 0.004 EUR against
        # 0.004 EUR charged here" — reads as a warning about nothing and invites
        # the reader to hunt for a gap that is smaller than the precision shown.
        # Agreement is the useful thing to say when that is what happened.
        if theirs == ours:
            headline = (
                f"{drift.provider}: its reported cost agrees with ours to within a "
                f"milli-unit over {drift.requests} request(s), at {ours}."
            )
        else:
            headline = (
                f"{drift.provider}: it reported {theirs} for {drift.requests} "
                f"request(s) against {ours} charged here."
            )
        # A persistent gap is a stale price row or an upstream rate change, and
        # that is the only thing an operator does about it — so it stays in the
        # sentence. What was dropped from it: that the provider's figure is
        # never billed from, which is not even true in `provider_reported` mode
        # and is what `cost_source` says on every row anyway.
        notes.append(
            f"{headline} Covers only the requests that provider reported a cost for, so "
            "it is not comparable to the totals above. A persistent gap means a stale "
            "price row or an upstream rate change."
        )
    if substituted:
        # A router with model fallback can serve a different model than the one
        # asked for, and bills for the one that ran (ADR 0028). We price from the
        # requested model, so this figure is not necessarily what the provider
        # will invoice. Said out loud rather than left to be discovered.
        notes.append(
            f"{substituted} request(s) were served by a substitute model and priced at "
            "the requested model's price, so this total may not match the provider's "
            "invoice for them."
        )
    return notes


async def build_report(
    session: AsyncSession,
    filters: ReportFilter,
    *,
    currency: str,
    timezone: str,
    metering_enabled: bool = True,
) -> UsageReport:
    """Aggregate the ledger over one period."""
    dialect = session.bind.dialect.name if session.bind is not None else "postgresql"
    rows = (await session.execute(_base_query(filters, timezone, dialect))).all()
    labels = await _labels(session, filters.group_by, [row[0] for row in rows])

    report_rows = [_row(filters.group_by, row[0], labels, row[1:]) for row in rows]
    if filters.group_by is GroupBy.DAY:
        report_rows.sort(key=lambda row: row.label)
    else:
        report_rows.sort(key=lambda row: (-row.cost, row.label))

    totals = _totals(report_rows)
    if filters.group_by is GroupBy.TOTAL:
        report_rows = []

    in_flight = (
        await session.execute(
            select(func.count(UsageRecord.id)).where(
                UsageRecord.created_at >= filters.period.start,
                UsageRecord.created_at < filters.period.end,
                UsageRecord.status == UsageStatus.IN_PROGRESS,
            )
        )
    ).scalar_one()

    # Requests the provider served with a model other than the one asked for.
    # The flag is set at write time by the recorder, which is the only place that
    # can tell: comparing `model_name` (ours) with `upstream_model` (theirs) would
    # mark every request, since those differ by design.
    substituted = (
        await session.execute(
            select(func.count(UsageRecord.id)).where(
                UsageRecord.created_at >= filters.period.start,
                UsageRecord.created_at < filters.period.end,
                UsageRecord.status != UsageStatus.IN_PROGRESS,
                UsageRecord.model_substituted.is_(True),
            )
        )
    ).scalar_one()

    # Requests where the provider is configured for pass-through billing but
    # reported nothing, so our own prices were used instead. Counted because a
    # silent fallback means a deployment billing from a price table nobody is
    # maintaining, which looks exactly like one that is working.
    fell_back = (
        await session.execute(
            select(func.count(UsageRecord.id)).where(
                UsageRecord.created_at >= filters.period.start,
                UsageRecord.created_at < filters.period.end,
                UsageRecord.status != UsageStatus.IN_PROGRESS,
                UsageRecord.cost_source == CostSource.OWN_PRICES_FALLBACK,
            )
        )
    ).scalar_one()

    reconciliation = await _reconciliation(session, filters, currency=currency)

    return UsageReport(
        period=PeriodResponse(
            label=filters.period.label,
            kind=filters.period.kind.value if filters.period.kind else None,
            start=filters.period.start,
            end=filters.period.end,
            timezone=timezone,
        ),
        group_by=filters.group_by.value,
        currency=currency,
        rows=report_rows,
        totals=totals,
        reconciliation=reconciliation,
        disclosures=_disclosures(
            totals,
            int(in_flight or 0),
            int(substituted or 0),
            reconciliation,
            int(fell_back or 0),
            metering_enabled,
        ),
    )


CSV_HEADER = [
    "period",
    "period_start",
    "period_end",
    "timezone",
    "group_by",
    "key",
    "label",
    "requests",
    "prompt_tokens",
    "completion_tokens",
    "total_tokens",
    "images",
    "searches",
    "cost",
    "currency",
    "estimated_requests",
    "unavailable_requests",
]


def report_to_csv(report: UsageReport) -> str:
    """Render a report as CSV, one row per subject plus a TOTAL row.

    Every row repeats the period and timezone. Redundant on screen, and the reason
    a spreadsheet that has been mailed on, renamed and opened six months later can
    still say what it covers.

    ``\\r\\n`` line endings per RFC 4180, which is also what Excel expects.
    """
    buffer = io.StringIO()
    writer = csv.writer(buffer, lineterminator="\r\n")
    writer.writerow(CSV_HEADER)

    period = report.period
    for row in [*report.rows, report.totals]:
        writer.writerow(
            [
                period.label,
                period.start.isoformat(),
                period.end.isoformat(),
                period.timezone,
                report.group_by,
                row.key or "",
                row.label,
                row.requests,
                row.prompt_tokens,
                row.completion_tokens,
                row.total_tokens,
                row.images,
                row.searches,
                # Plain decimal, never scientific notation: a spreadsheet reading
                # "1E-7" as text is a support ticket.
                f"{row.cost:f}",
                report.currency,
                row.estimated_requests,
                row.unavailable_requests,
            ]
        )
    return buffer.getvalue()


async def known_model_names(session: AsyncSession) -> list[str]:
    """Catalogue names, for validating a ``model`` filter."""
    return list((await session.execute(select(ModelDef.name))).scalars().all())
