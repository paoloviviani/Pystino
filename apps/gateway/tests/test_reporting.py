"""The reporting API and the reset endpoint.

The reconciliation property is the one worth stating twice: a monthly report and a
monthly quota must return the same number for the same period, and a reset must
change the second without touching the first. Both are asserted here against the
HTTP surface, and in test_quota_calendar.py against the engine.

Schema violations are asserted as 400, not 422: the gateway rewrites FastAPI's
validation response into the OpenAI error envelope (see main.validation_error_handler).
"""

from __future__ import annotations

import csv
import io
import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import httpx
import pytest
from conftest import Seeded
from gateway.models import (
    Group,
    LimitMetric,
    LimitRule,
    LimitScope,
    UsageRecord,
    UsageSource,
    UsageStatus,
    User,
)
from gateway.periods import PeriodKind, parse_period
from gateway.quota import DatabaseCounterStore, QuotaAmounts, QuotaExceeded, QuotaSubject
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from test_admin import as_user, make_admin
from test_quota_engine import engine_for

THIS_MONTH = datetime.now(UTC).strftime("%Y-%m")


async def record(
    session: AsyncSession,
    *,
    group_id: uuid.UUID | None = None,
    user_id: uuid.UUID | None = None,
    api_key_id: uuid.UUID | None = None,
    model: str = "test-model",
    cost: str = "1",
    prompt: int = 100,
    completion: int = 50,
    at: datetime | None = None,
    source: UsageSource = UsageSource.UPSTREAM_EXACT,
    status: UsageStatus = UsageStatus.COMPLETED,
    searches: int = 0,
    own_searches: int = 0,
) -> UsageRecord:
    row = UsageRecord(
        request_id=f"r-{uuid.uuid4().hex[:8]}",
        status=status,
        group_id=group_id,
        user_id=user_id,
        api_key_id=api_key_id,
        model_name=model,
        currency="EUR",
        prompt_tokens=prompt,
        completion_tokens=completion,
        total_tokens=prompt + completion,
        cost=Decimal(cost),
        usage_source=source,
        search_count=searches,
        own_search_requests=own_searches,
        created_at=at or datetime.now(UTC),
    )
    session.add(row)
    await session.commit()
    return row


def inside_this_month(hours_ago: int = 1) -> datetime:
    """A moment inside the current month, whatever day it is run.

    On the 1st, "an hour ago" can be last month; clamping keeps the test from
    failing once a month for a reason that has nothing to do with the code.
    """
    now = datetime.now(UTC)
    month = parse_period(now.strftime("%Y-%m"))
    return max(now - timedelta(hours=hours_ago), month.start + timedelta(minutes=1))


@pytest.fixture
async def admin_client(
    app: object,
    client: httpx.AsyncClient,
    seeded: Seeded,
    session_factory: async_sessionmaker[AsyncSession],
) -> httpx.AsyncClient:
    as_user(app, await make_admin(session_factory, seeded))
    return client


class TestPeriodSelection:
    async def test_defaults_to_the_current_month(self, admin_client: httpx.AsyncClient) -> None:
        body = (await admin_client.get("/api/admin/reports/usage")).json()
        assert body["period"]["label"] == THIS_MONTH
        assert body["period"]["kind"] == "month"

    async def test_the_period_is_echoed_with_its_boundaries_and_timezone(
        self, admin_client: httpx.AsyncClient
    ) -> None:
        """A report whose boundaries are invisible cannot be reconciled."""
        body = (await admin_client.get("/api/admin/reports/usage?period=2026-03")).json()
        period = body["period"]
        assert period["timezone"] == "Europe/Rome"
        # Rome is UTC+1 in early March: the month starts at 23:00 UTC on 28 February.
        assert period["start"].startswith("2026-02-28T23:00")
        assert period["end"].startswith("2026-03-31T22:00")

    @pytest.mark.parametrize(
        "period,label", [("2026-Q3", "2026-Q3"), ("2026", "2026"), ("2026-08-15", "2026-08-15")]
    )
    async def test_named_periods(
        self, admin_client: httpx.AsyncClient, period: str, label: str
    ) -> None:
        body = (await admin_client.get(f"/api/admin/reports/usage?period={period}")).json()
        assert body["period"]["label"] == label

    async def test_an_unparsable_period_is_400_with_the_accepted_forms(
        self, admin_client: httpx.AsyncClient
    ) -> None:
        response = await admin_client.get("/api/admin/reports/usage?period=last-month")
        assert response.status_code == 400
        assert "2026-08" in response.json()["error"]["message"]

    async def test_an_explicit_range(self, admin_client: httpx.AsyncClient) -> None:
        response = await admin_client.get(
            "/api/admin/reports/usage?start=2026-08-01T00:00:00Z&end=2026-08-08T00:00:00Z"
        )
        assert response.status_code == 200
        assert response.json()["period"]["kind"] is None

    async def test_a_period_and_a_range_together_are_refused(
        self, admin_client: httpx.AsyncClient
    ) -> None:
        response = await admin_client.get(
            "/api/admin/reports/usage?period=2026-08&start=2026-08-01T00:00:00Z"
        )
        assert response.status_code == 400

    async def test_half_a_range_is_refused(self, admin_client: httpx.AsyncClient) -> None:
        response = await admin_client.get("/api/admin/reports/usage?start=2026-08-01T00:00:00Z")
        assert response.status_code == 400


class TestGrouping:
    async def test_by_group(
        self, admin_client: httpx.AsyncClient, session: AsyncSession, seeded: Seeded
    ) -> None:
        other = Group(name="teaching")
        session.add(other)
        await session.flush()
        await record(session, group_id=seeded.group.id, cost="3", at=inside_this_month())
        await record(session, group_id=other.id, cost="7", at=inside_this_month())

        body = (await admin_client.get("/api/admin/reports/usage?group_by=group")).json()
        rows = {row["label"]: row for row in body["rows"]}
        assert rows["research"]["cost"] == "3.000000000000"
        assert rows["teaching"]["cost"] == "7.000000000000"
        # Highest spend first: the row an administrator is looking for.
        assert body["rows"][0]["label"] == "teaching"
        assert body["totals"]["cost"] == "10.000000000000"

    async def test_by_model(
        self, admin_client: httpx.AsyncClient, session: AsyncSession, seeded: Seeded
    ) -> None:
        await record(session, group_id=seeded.group.id, model="a", cost="1", at=inside_this_month())
        await record(session, group_id=seeded.group.id, model="b", cost="2", at=inside_this_month())
        body = (await admin_client.get("/api/admin/reports/usage?group_by=model")).json()
        assert [row["label"] for row in body["rows"]] == ["b", "a"]

    async def test_by_day_is_ordered_by_date_not_by_spend(
        self, admin_client: httpx.AsyncClient, session: AsyncSession, seeded: Seeded
    ) -> None:
        """A time series read out of order is unreadable, whatever the totals say."""
        month = parse_period("2026-08")
        for day, cost in ((3, "5"), (1, "9"), (2, "1")):
            await record(
                session,
                group_id=seeded.group.id,
                cost=cost,
                at=month.start + timedelta(days=day, hours=12),
            )
        body = (
            await admin_client.get("/api/admin/reports/usage?period=2026-08&group_by=day")
        ).json()
        labels = [row["label"] for row in body["rows"]]
        assert labels == sorted(labels)
        assert len(labels) == 3

    async def test_by_total_returns_no_rows_only_a_total(
        self, admin_client: httpx.AsyncClient, session: AsyncSession, seeded: Seeded
    ) -> None:
        await record(session, group_id=seeded.group.id, cost="4", at=inside_this_month())
        body = (await admin_client.get("/api/admin/reports/usage?group_by=total")).json()
        assert body["rows"] == []
        assert body["totals"]["cost"] == "4.000000000000"

    async def test_unattributed_spend_stays_visible(
        self, admin_client: httpx.AsyncClient, session: AsyncSession, seeded: Seeded
    ) -> None:
        """A row with no group is labelled, not dropped: money that belongs to
        nobody is exactly the money worth noticing."""
        await record(session, group_id=None, cost="2", at=inside_this_month())
        body = (await admin_client.get("/api/admin/reports/usage?group_by=group")).json()
        assert body["rows"][0]["label"] == "(no group)"
        assert body["totals"]["cost"] == "2.000000000000"

    async def test_an_erased_user_keeps_their_spend_in_the_report(
        self,
        admin_client: httpx.AsyncClient,
        session: AsyncSession,
        seeded: Seeded,
    ) -> None:
        """usage_records.user_id is ON DELETE SET NULL, so the row survives the
        person. Last month's total must not change because someone left."""
        ghost = User(issuer="https://idp.test", subject="gone", email="gone@example.org")
        session.add(ghost)
        await session.flush()
        await record(session, user_id=ghost.id, cost="6", at=inside_this_month())
        before = (await admin_client.get("/api/admin/reports/usage?group_by=user")).json()
        assert before["totals"]["cost"] == "6.000000000000"

        await session.delete(ghost)
        await session.commit()

        after = (await admin_client.get("/api/admin/reports/usage?group_by=user")).json()
        assert after["totals"]["cost"] == "6.000000000000"
        assert after["rows"][0]["label"] == "(no user)"


class TestFilters:
    async def test_by_group_id(
        self, admin_client: httpx.AsyncClient, session: AsyncSession, seeded: Seeded
    ) -> None:
        other = Group(name="teaching")
        session.add(other)
        await session.flush()
        await record(session, group_id=seeded.group.id, cost="3", at=inside_this_month())
        await record(session, group_id=other.id, cost="7", at=inside_this_month())

        body = (
            await admin_client.get(
                f"/api/admin/reports/usage?group_by=model&group_id={seeded.group.id}"
            )
        ).json()
        assert body["totals"]["cost"] == "3.000000000000"

    async def test_by_model_name(
        self, admin_client: httpx.AsyncClient, session: AsyncSession, seeded: Seeded
    ) -> None:
        await record(session, group_id=seeded.group.id, model="a", cost="1", at=inside_this_month())
        await record(session, group_id=seeded.group.id, model="b", cost="2", at=inside_this_month())
        body = (await admin_client.get("/api/admin/reports/usage?model=b")).json()
        assert body["totals"]["cost"] == "2.000000000000"


class TestBoundaries:
    async def test_spend_outside_the_period_is_excluded(
        self, admin_client: httpx.AsyncClient, session: AsyncSession, seeded: Seeded
    ) -> None:
        august = parse_period("2026-08")
        await record(
            session, group_id=seeded.group.id, cost="1", at=august.start - timedelta(seconds=1)
        )
        await record(session, group_id=seeded.group.id, cost="2", at=august.start)
        await record(
            session, group_id=seeded.group.id, cost="4", at=august.end - timedelta(seconds=1)
        )
        # The first instant of September belongs to September.
        await record(session, group_id=seeded.group.id, cost="8", at=august.end)

        body = (await admin_client.get("/api/admin/reports/usage?period=2026-08")).json()
        assert body["totals"]["cost"] == "6.000000000000"

    async def test_consecutive_months_partition_the_spend(
        self, admin_client: httpx.AsyncClient, session: AsyncSession, seeded: Seeded
    ) -> None:
        """Nothing counted twice, nothing lost between the two reports."""
        august = parse_period("2026-08")
        for offset in (-1, 0, 1):
            await record(
                session,
                group_id=seeded.group.id,
                cost="5",
                at=august.end + timedelta(seconds=offset),
            )
        first = (await admin_client.get("/api/admin/reports/usage?period=2026-08")).json()
        second = (await admin_client.get("/api/admin/reports/usage?period=2026-09")).json()
        assert Decimal(first["totals"]["cost"]) == Decimal(5)
        assert Decimal(second["totals"]["cost"]) == Decimal(10)


class TestDisclosures:
    async def test_estimated_usage_is_declared(
        self, admin_client: httpx.AsyncClient, session: AsyncSession, seeded: Seeded
    ) -> None:
        await record(session, group_id=seeded.group.id, cost="1", at=inside_this_month())
        await record(
            session,
            group_id=seeded.group.id,
            cost="1",
            at=inside_this_month(),
            source=UsageSource.ESTIMATED,
        )
        body = (await admin_client.get("/api/admin/reports/usage")).json()
        assert body["totals"]["estimated_requests"] == 1
        assert any("estimated" in note for note in body["disclosures"])

    async def test_missing_usage_says_the_total_is_a_floor(
        self, admin_client: httpx.AsyncClient, session: AsyncSession, seeded: Seeded
    ) -> None:
        await record(
            session,
            group_id=seeded.group.id,
            cost="0",
            at=inside_this_month(),
            source=UsageSource.UNAVAILABLE,
        )
        body = (await admin_client.get("/api/admin/reports/usage")).json()
        assert any("floor" in note for note in body["disclosures"])

    async def test_in_flight_requests_are_excluded_and_said_so(
        self, admin_client: httpx.AsyncClient, session: AsyncSession, seeded: Seeded
    ) -> None:
        await record(
            session,
            group_id=seeded.group.id,
            cost="3",
            at=inside_this_month(),
            status=UsageStatus.IN_PROGRESS,
        )
        body = (await admin_client.get("/api/admin/reports/usage")).json()
        assert Decimal(body["totals"]["cost"]) == 0
        assert any("in flight" in note for note in body["disclosures"])

    async def test_a_clean_report_has_no_disclosures(
        self, admin_client: httpx.AsyncClient, session: AsyncSession, seeded: Seeded
    ) -> None:
        await record(session, group_id=seeded.group.id, cost="1", at=inside_this_month())
        body = (await admin_client.get("/api/admin/reports/usage")).json()
        assert body["disclosures"] == []


class TestCsv:
    async def test_csv_carries_the_period_on_every_row(
        self, admin_client: httpx.AsyncClient, session: AsyncSession, seeded: Seeded
    ) -> None:
        """A spreadsheet gets mailed on, renamed and opened six months later."""
        await record(session, group_id=seeded.group.id, cost="3", at=inside_this_month())
        response = await admin_client.get("/api/admin/reports/usage.csv")

        assert response.status_code == 200
        assert response.headers["content-type"].startswith("text/csv")
        assert f"usage-{THIS_MONTH}-by-group.csv" in response.headers["content-disposition"]

        rows = list(csv.DictReader(io.StringIO(response.text)))
        assert [row["period"] for row in rows] == [THIS_MONTH, THIS_MONTH]
        assert rows[0]["label"] == "research"
        assert rows[-1]["label"] == "total"
        assert rows[-1]["currency"] == "EUR"

    async def test_the_csv_carries_the_search_count(
        self, admin_client: httpx.AsyncClient, session: AsyncSession, seeded: Seeded
    ) -> None:
        """Found by the live check, not by this suite.

        The export keeps its own list of columns, so a unit added to the report
        reaches the screen and silently misses the spreadsheet — which is the
        artefact somebody reconciles an invoice in.
        """
        await record(
            session, group_id=seeded.group.id, cost="3", searches=6, at=inside_this_month()
        )
        response = await admin_client.get("/api/admin/reports/usage.csv")

        rows = list(csv.DictReader(io.StringIO(response.text)))
        assert [row["searches"] for row in rows] == ["6", "6"]

    async def test_costs_are_plain_decimals_not_scientific_notation(
        self, admin_client: httpx.AsyncClient, session: AsyncSession, seeded: Seeded
    ) -> None:
        """A spreadsheet reading "1E-7" as text is a support ticket."""
        await record(session, group_id=seeded.group.id, cost="0.0000001", at=inside_this_month())
        response = await admin_client.get("/api/admin/reports/usage.csv")
        costs = [row["cost"] for row in csv.DictReader(io.StringIO(response.text))]
        assert costs == ["0.000000100000", "0.000000100000"]

    async def test_a_comma_in_a_group_name_does_not_shift_the_columns(
        self, admin_client: httpx.AsyncClient, session: AsyncSession, seeded: Seeded
    ) -> None:
        """A group called `Research, AI` is not exotic, and an unquoted comma
        silently moves every number one column to the right."""
        awkward = Group(name='Research, AI ("core")')
        session.add(awkward)
        await session.flush()
        await record(session, group_id=awkward.id, cost="3", at=inside_this_month())

        response = await admin_client.get("/api/admin/reports/usage.csv")
        rows = list(csv.DictReader(io.StringIO(response.text)))
        assert rows[0]["label"] == 'Research, AI ("core")'
        assert Decimal(rows[0]["cost"]) == Decimal(3)

    async def test_csv_and_json_agree(
        self, admin_client: httpx.AsyncClient, session: AsyncSession, seeded: Seeded
    ) -> None:
        await record(session, group_id=seeded.group.id, cost="2.5", at=inside_this_month())
        body = (await admin_client.get("/api/admin/reports/usage")).json()
        rows = list(
            csv.DictReader(
                io.StringIO((await admin_client.get("/api/admin/reports/usage.csv")).text)
            )
        )
        assert Decimal(rows[-1]["cost"]) == Decimal(body["totals"]["cost"])


class TestSelfService:
    async def test_a_user_sees_their_own_spend(
        self,
        app: object,
        client: httpx.AsyncClient,
        session: AsyncSession,
        seeded: Seeded,
    ) -> None:
        as_user(app, seeded.user)
        await record(session, user_id=seeded.user.id, cost="4", at=inside_this_month())
        body = (await client.get("/api/me/reports/usage")).json()
        assert body["totals"]["cost"] == "4.000000000000"

    async def test_a_user_does_not_see_anyone_elses(
        self,
        app: object,
        client: httpx.AsyncClient,
        session: AsyncSession,
        seeded: Seeded,
    ) -> None:
        """The filter is pinned server-side; there is no parameter that widens it."""
        other = User(issuer="https://idp.test", subject="other", email="other@example.org")
        session.add(other)
        await session.flush()
        as_user(app, seeded.user)
        await record(session, user_id=other.id, cost="99", at=inside_this_month())
        await record(session, user_id=seeded.user.id, cost="1", at=inside_this_month())

        body = (await client.get("/api/me/reports/usage")).json()
        assert body["totals"]["cost"] == "1.000000000000"

    async def test_a_user_cannot_group_by_user(
        self, app: object, client: httpx.AsyncClient, seeded: Seeded
    ) -> None:
        as_user(app, seeded.user)
        assert (await client.get("/api/me/reports/usage?group_by=user")).status_code == 400

    async def test_self_service_csv(
        self,
        app: object,
        client: httpx.AsyncClient,
        session: AsyncSession,
        seeded: Seeded,
    ) -> None:
        as_user(app, seeded.user)
        await record(session, user_id=seeded.user.id, cost="4", at=inside_this_month())
        response = await client.get("/api/me/reports/usage.csv")
        assert response.status_code == 200
        assert "my-usage-" in response.headers["content-disposition"]

    async def test_the_admin_report_is_still_admin_only(
        self, app: object, client: httpx.AsyncClient, seeded: Seeded
    ) -> None:
        as_user(app, seeded.user)
        assert (await client.get("/api/admin/reports/usage")).status_code == 403


class TestLimitsApi:
    async def test_a_calendar_rule_can_be_created(self, admin_client: httpx.AsyncClient) -> None:
        response = await admin_client.post(
            "/api/admin/limits",
            json={
                "name": "monthly",
                "scope": "global",
                "metric": "cost",
                "period": "month",
                "limit_value": "100",
            },
        )
        assert response.status_code == 201
        body = response.json()
        assert body["period"] == "month"
        assert body["window_seconds"] is None
        assert body["window_label"] == "month"

    async def test_both_window_kinds_at_once_is_refused(
        self, admin_client: httpx.AsyncClient
    ) -> None:
        response = await admin_client.post(
            "/api/admin/limits",
            json={
                "scope": "global",
                "metric": "cost",
                "period": "month",
                "window_seconds": 3600,
                "limit_value": "100",
            },
        )
        assert response.status_code == 400

    async def test_neither_window_kind_is_refused(self, admin_client: httpx.AsyncClient) -> None:
        response = await admin_client.post(
            "/api/admin/limits",
            json={"scope": "global", "metric": "cost", "limit_value": "100"},
        )
        assert response.status_code == 400

    async def test_a_second_identical_rule_is_409_not_a_silent_duplicate(
        self, admin_client: httpx.AsyncClient
    ) -> None:
        payload = {
            "scope": "global",
            "metric": "cost",
            "period": "month",
            "limit_value": "100",
        }
        assert (await admin_client.post("/api/admin/limits", json=payload)).status_code == 201
        assert (await admin_client.post("/api/admin/limits", json=payload)).status_code == 409

    async def test_an_api_key_scoped_rule_needs_a_real_key(
        self, admin_client: httpx.AsyncClient, seeded: Seeded
    ) -> None:
        missing = await admin_client.post(
            "/api/admin/limits",
            json={
                "scope": "api_key",
                "scope_id": str(uuid.uuid4()),
                "metric": "cost",
                "period": "month",
                "limit_value": "5",
            },
        )
        assert missing.status_code == 404

        real = await admin_client.post(
            "/api/admin/limits",
            json={
                "scope": "api_key",
                "scope_id": str(seeded.api_key.id),
                "metric": "cost",
                "period": "month",
                "limit_value": "5",
            },
        )
        assert real.status_code == 201

    async def test_current_value_reflects_consumption(
        self,
        admin_client: httpx.AsyncClient,
        session: AsyncSession,
        seeded: Seeded,
    ) -> None:
        """What an administrator sees is what the next request will be judged
        against — the same counters, not a parallel calculation."""
        created = (
            await admin_client.post(
                "/api/admin/limits",
                json={
                    "name": "group monthly",
                    "scope": "group",
                    "scope_id": str(seeded.group.id),
                    "metric": "cost",
                    "period": "month",
                    "limit_value": "50",
                },
            )
        ).json()

        subject = QuotaSubject(seeded.user.id, seeded.group.id)
        # The app's own engine, so the counters are the ones the API will read.
        quota = admin_client._transport.app.state.quota_engine  # type: ignore[attr-defined]
        reservation = await quota.check_and_reserve(session, subject, QuotaAmounts())
        await quota.settle(reservation, QuotaAmounts(cost=Decimal("12.5")))

        rules = (await admin_client.get("/api/admin/limits")).json()["items"]
        rule = next(entry for entry in rules if entry["id"] == created["id"])
        assert Decimal(rule["current_value"]) == Decimal("12.5")


class TestTotalsAreConsistent:
    async def test_a_users_own_report_sums_into_their_groups(
        self,
        app: object,
        client: httpx.AsyncClient,
        session: AsyncSession,
        session_factory: async_sessionmaker[AsyncSession],
        seeded: Seeded,
    ) -> None:
        """What a user sees for themselves has to be part of what their group lead
        sees for the group — the same ledger, the same period, one query builder."""
        colleague = User(issuer="https://idp.test", subject="colleague", email="c@example.org")
        session.add(colleague)
        await session.flush()
        await record(
            session,
            group_id=seeded.group.id,
            user_id=seeded.user.id,
            cost="2",
            at=inside_this_month(),
        )
        await record(
            session,
            group_id=seeded.group.id,
            user_id=colleague.id,
            cost="3",
            at=inside_this_month(),
        )

        as_user(app, seeded.user)
        mine = (await client.get("/api/me/reports/usage")).json()

        as_user(app, await make_admin(session_factory, seeded))
        theirs = (
            await client.get(f"/api/admin/reports/usage?group_id={seeded.group.id}&group_by=user")
        ).json()

        assert Decimal(mine["totals"]["cost"]) == Decimal(2)
        assert Decimal(theirs["totals"]["cost"]) == Decimal(5)
        row = next(entry for entry in theirs["rows"] if entry["key"] == str(seeded.user.id))
        assert Decimal(row["cost"]) == Decimal(mine["totals"]["cost"])

    async def test_the_rows_sum_to_the_total_on_every_dimension(
        self, admin_client: httpx.AsyncClient, session: AsyncSession, seeded: Seeded
    ) -> None:
        """Whichever way the same spend is sliced, it adds up to the same money."""
        other = Group(name="teaching")
        session.add(other)
        await session.flush()
        await record(
            session, group_id=seeded.group.id, model="a", cost="1.5", at=inside_this_month()
        )
        await record(session, group_id=other.id, model="b", cost="2.25", at=inside_this_month())
        await record(session, group_id=None, model="a", cost="0.25", at=inside_this_month())

        for dimension in ("group", "user", "model", "api_key", "day", "total"):
            body = (await admin_client.get(f"/api/admin/reports/usage?group_by={dimension}")).json()
            total = Decimal(body["totals"]["cost"])
            assert total == Decimal(4), dimension
            if body["rows"]:
                assert sum(Decimal(row["cost"]) for row in body["rows"]) == total, dimension


class TestSeedingANewRule:
    async def test_a_rule_created_mid_period_counts_the_spend_already_there(
        self, admin_client: httpx.AsyncClient, session: AsyncSession, seeded: Seeded
    ) -> None:
        """Found against the live stack: a new rule's counter started at zero.

        For "EUR 5 this month" created on the 20th that means the cap is wrong for
        eleven days — the group has already spent the money and the gateway does
        not know. The cold-cache rebuild cannot help: the cache is not cold, only
        this rule is.
        """
        await record(session, group_id=seeded.group.id, cost="7", at=inside_this_month())

        created = (
            await admin_client.post(
                "/api/admin/limits",
                json={
                    "scope": "group",
                    "scope_id": str(seeded.group.id),
                    "metric": "cost",
                    "period": "month",
                    "limit_value": "10",
                },
            )
        ).json()
        assert Decimal(created["current_value"]) == Decimal(7)

    async def test_the_seeded_counter_matches_the_report(
        self, admin_client: httpx.AsyncClient, session: AsyncSession, seeded: Seeded
    ) -> None:
        await record(session, group_id=seeded.group.id, cost="2.5", at=inside_this_month())
        created = (
            await admin_client.post(
                "/api/admin/limits",
                json={
                    "scope": "group",
                    "scope_id": str(seeded.group.id),
                    "metric": "cost",
                    "period": "month",
                    "limit_value": "10",
                },
            )
        ).json()
        report = (
            await admin_client.get(
                f"/api/admin/reports/usage?group_id={seeded.group.id}&group_by=total"
            )
        ).json()
        assert Decimal(created["current_value"]) == Decimal(report["totals"]["cost"])

    async def test_seeding_a_scope_with_no_history_is_zero_not_an_error(
        self, admin_client: httpx.AsyncClient, seeded: Seeded
    ) -> None:
        created = (
            await admin_client.post(
                "/api/admin/limits",
                json={
                    "scope": "global",
                    "metric": "requests",
                    "window_seconds": 3600,
                    "limit_value": "10",
                },
            )
        ).json()
        assert Decimal(created["current_value"] or 0) == 0


class TestResetEndpoint:
    async def test_a_reset_needs_a_reason(
        self, admin_client: httpx.AsyncClient, session: AsyncSession, seeded: Seeded
    ) -> None:
        rule = LimitRule(
            name="m",
            scope=LimitScope.GROUP,
            scope_id=seeded.group.id,
            metric=LimitMetric.COST,
            period=PeriodKind.MONTH,
            limit_value=Decimal(5),
        )
        session.add(rule)
        await session.commit()

        assert (
            await admin_client.post(f"/api/admin/limits/{rule.id}/reset", json={})
        ).status_code == 400
        assert (
            await admin_client.post(f"/api/admin/limits/{rule.id}/reset", json={"reason": ""})
        ).status_code == 400

    async def test_a_reset_records_who_and_why(
        self, admin_client: httpx.AsyncClient, session: AsyncSession, seeded: Seeded
    ) -> None:
        rule = LimitRule(
            name="m",
            scope=LimitScope.GROUP,
            scope_id=seeded.group.id,
            metric=LimitMetric.COST,
            period=PeriodKind.MONTH,
            limit_value=Decimal(5),
        )
        session.add(rule)
        await session.commit()

        response = await admin_client.post(
            f"/api/admin/limits/{rule.id}/reset", json={"reason": "grant extension approved"}
        )
        assert response.status_code == 200
        assert response.json()["reason"] == "grant extension approved"
        assert response.json()["created_by_email"] == seeded.user.email

        trail = (await admin_client.get(f"/api/admin/limits/{rule.id}/resets")).json()["items"]
        assert [entry["reason"] for entry in trail] == ["grant extension approved"]

    async def test_resetting_an_unknown_rule_is_404(self, admin_client: httpx.AsyncClient) -> None:
        response = await admin_client.post(
            f"/api/admin/limits/{uuid.uuid4()}/reset", json={"reason": "nothing there"}
        )
        assert response.status_code == 404

    async def test_a_reset_changes_enforcement_and_not_the_report(
        self,
        admin_client: httpx.AsyncClient,
        session: AsyncSession,
        session_factory: async_sessionmaker[AsyncSession],
        seeded: Seeded,
    ) -> None:
        """The property the whole design turns on, end to end over HTTP."""
        rule = LimitRule(
            name="m",
            scope=LimitScope.GROUP,
            scope_id=seeded.group.id,
            metric=LimitMetric.COST,
            period=PeriodKind.MONTH,
            limit_value=Decimal(5),
        )
        session.add(rule)
        await session.commit()
        await record(session, group_id=seeded.group.id, cost="9", at=inside_this_month())

        before = (await admin_client.get("/api/admin/reports/usage")).json()
        quota = engine_for(fallback=DatabaseCounterStore(session_factory))
        assert await quota.rebuild_if_cache_is_cold(session) is True
        with pytest.raises(QuotaExceeded):
            await quota.check_and_reserve(
                session, QuotaSubject(None, seeded.group.id), QuotaAmounts()
            )

        assert (
            await admin_client.post(
                f"/api/admin/limits/{rule.id}/reset", json={"reason": "legitimate overspend"}
            )
        ).status_code == 200

        # Enforcement moved...
        assert not (
            await quota.check_and_reserve(
                session, QuotaSubject(None, seeded.group.id), QuotaAmounts()
            )
        ).is_empty
        # ...and the bill did not.
        after = (await admin_client.get("/api/admin/reports/usage")).json()
        assert after["totals"] == before["totals"]
        assert Decimal(after["totals"]["cost"]) == Decimal(9)


class TestReportAndQuotaAgree:
    async def test_the_same_month_gives_the_same_number(
        self,
        admin_client: httpx.AsyncClient,
        session: AsyncSession,
        session_factory: async_sessionmaker[AsyncSession],
        seeded: Seeded,
    ) -> None:
        """A monthly budget and a monthly report read the same ledger through the
        same period boundaries. If these two ever disagree, one of them is lying to
        someone about money."""
        await record(session, group_id=seeded.group.id, cost="3.25", at=inside_this_month())
        await record(session, group_id=seeded.group.id, cost="1.75", at=inside_this_month(2))

        report = (
            await admin_client.get(f"/api/admin/reports/usage?group_id={seeded.group.id}")
        ).json()

        rule = LimitRule(
            name="m",
            scope=LimitScope.GROUP,
            scope_id=seeded.group.id,
            metric=LimitMetric.COST,
            period=PeriodKind.MONTH,
            limit_value=Decimal(1000),
        )
        session.add(rule)
        await session.commit()
        await session.refresh(rule, attribute_names=["resets"])

        quota = engine_for(fallback=DatabaseCounterStore(session_factory))
        assert await quota.rebuild_if_cache_is_cold(session) is True
        current = await quota.current_values([rule])

        assert current[rule.id] == Decimal(report["totals"]["cost"])
        assert current[rule.id] == Decimal(5)


class TestSearchesInTheReport:
    """ADR 0058: the count a provider's invoice is itemised by.

    Reported for the reason `images` is, and one sharper: a search charge lands
    on a row that otherwise looks like an ordinary completion, so without the
    count part of the spend has no visible cause.
    """

    async def test_the_count_is_summed_and_disclosed(
        self,
        admin_client: httpx.AsyncClient,
        seeded: Seeded,
        session: AsyncSession,
    ) -> None:
        await record(session, group_id=seeded.group.id, cost="1", searches=3)
        await record(session, group_id=seeded.group.id, cost="2", searches=4)

        response = await admin_client.get(f"/api/admin/reports/usage?period={THIS_MONTH}")
        assert response.status_code == 200
        body = response.json()
        assert body["totals"]["searches"] == 7
        assert any("web search" in note for note in body["disclosures"])

    async def test_a_month_without_searches_says_nothing_about_them(
        self,
        admin_client: httpx.AsyncClient,
        seeded: Seeded,
        session: AsyncSession,
    ) -> None:
        """A disclosure for something that did not happen is noise."""
        await record(session, group_id=seeded.group.id, cost="1")

        response = await admin_client.get(f"/api/admin/reports/usage?period={THIS_MONTH}")
        body = response.json()
        assert body["totals"]["searches"] == 0
        assert not any("web search" in note for note in body["disclosures"])


class TestOwnSearches:
    """Our own search backends, counted and never priced.

    The whole reason these are a separate column: one is the counterparty
    searching while it serves a chat request, the other is this deployment
    calling Exa or Staan. Added together they reconcile against neither
    invoice, which is the same failure `cost` / `computed_cost` /
    `upstream_cost` are kept apart to avoid.
    """

    async def test_theirs_and_ours_are_reported_apart(
        self, admin_client: httpx.AsyncClient, session: AsyncSession, seeded: Seeded
    ) -> None:
        # Deliberately unequal. Equal numbers would pass against an
        # implementation that summed the wrong column.
        await record(
            session,
            group_id=seeded.group.id,
            cost="1",
            searches=9,
            own_searches=2,
            at=inside_this_month(),
        )

        body = (
            await admin_client.get(f"/api/admin/reports/usage?period={THIS_MONTH}")
        ).json()
        assert body["totals"]["searches"] == 9
        assert body["totals"]["own_searches"] == 2

    async def test_the_csv_carries_ours_too(
        self, admin_client: httpx.AsyncClient, session: AsyncSession, seeded: Seeded
    ) -> None:
        """The export keeps its own column list, and the last unit added to the
        report reached the screen while silently missing the spreadsheet —
        which is the artefact somebody reconciles an invoice in."""
        await record(
            session,
            group_id=seeded.group.id,
            cost="1",
            searches=9,
            own_searches=2,
            at=inside_this_month(),
        )
        response = await admin_client.get("/api/admin/reports/usage.csv")

        rows = list(csv.DictReader(io.StringIO(response.text)))
        assert [row["own_searches"] for row in rows] == ["2", "2"]
        assert [row["searches"] for row in rows] == ["9", "9"]

    async def test_the_disclosure_says_their_cost_is_absent(
        self, admin_client: httpx.AsyncClient, session: AsyncSession, seeded: Seeded
    ) -> None:
        """A count with no price in a report full of prices reads as free."""
        await record(
            session, group_id=seeded.group.id, cost="1", own_searches=4, at=inside_this_month()
        )

        body = (
            await admin_client.get(f"/api/admin/reports/usage?period={THIS_MONTH}")
        ).json()
        note = next(n for n in body["disclosures"] if "own search backends" in n)
        assert "counted, not priced" in note
        assert "4" in note

    async def test_no_disclosure_when_we_searched_nothing(
        self, admin_client: httpx.AsyncClient, session: AsyncSession, seeded: Seeded
    ) -> None:
        await record(
            session, group_id=seeded.group.id, cost="1", searches=3, at=inside_this_month()
        )

        body = (
            await admin_client.get(f"/api/admin/reports/usage?period={THIS_MONTH}")
        ).json()
        assert body["totals"]["own_searches"] == 0
        assert not any("own search backends" in n for n in body["disclosures"])
