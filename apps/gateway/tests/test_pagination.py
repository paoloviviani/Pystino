"""Pagination on the management listings.

The properties worth pinning are the ones whose absence is invisible. A page
that silently drops rows still renders; a `total` that counts the page rather
than the match still shows a number; a search that filters after the window
still returns something. Each of those is a listing that lies, and none of them
looks broken from the outside.

The walk test is the load-bearing one: paging from 0 to the end must yield every
row exactly once. Off-by-one in `offset` is the classic way to lose a row in the
middle of a catalogue, and it cannot be seen on page one.
"""

from __future__ import annotations

import uuid
from decimal import Decimal

import httpx
import pytest
from conftest import Seeded
from gateway.models import Group, LimitMetric, LimitRule, LimitScope, ModelDef, ModelPrice, User
from gateway.pagination import DEFAULT_LIMIT, MAX_LIMIT
from gateway.types import utcnow
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from test_admin import as_user, make_admin


@pytest.fixture
async def admin_client(
    app: object,
    client: httpx.AsyncClient,
    seeded: Seeded,
    session_factory: async_sessionmaker[AsyncSession],
) -> httpx.AsyncClient:
    as_user(app, await make_admin(session_factory, seeded))
    return client


async def _add_models(session: AsyncSession, seeded: Seeded, count: int) -> list[str]:
    """`count` extra models with sortable, searchable names."""
    names = [f"bulk-model-{index:03d}" for index in range(count)]
    for name in names:
        session.add(
            ModelDef(
                name=name,
                upstream_model=f"upstream/{name}",
                provider_id=seeded.provider.id,
            )
        )
    await session.commit()
    return names


async def _add_users(session: AsyncSession, count: int) -> list[str]:
    emails = [f"bulk-{index:03d}@example.org" for index in range(count)]
    for index, email in enumerate(emails):
        session.add(
            User(
                issuer="https://idp.test",
                subject=f"bulk-subject-{index}",
                email=email,
                display_name=f"Bulk {index}",
            )
        )
    await session.commit()
    return emails


async def _walk(client: httpx.AsyncClient, path: str, *, limit: int) -> list[dict]:
    """Every row, collected a page at a time the way a client would."""
    collected: list[dict] = []
    offset = 0
    while True:
        body = (await client.get(path, params={"limit": limit, "offset": offset})).json()
        collected.extend(body["items"])
        offset += limit
        if offset >= body["total"]:
            return collected


class TestEnvelope:
    async def test_a_listing_reports_the_match_not_the_page(
        self, admin_client: httpx.AsyncClient, session: AsyncSession, seeded: Seeded
    ) -> None:
        """`total` is what a client needs to know there is more."""
        await _add_models(session, seeded, 20)

        body = (await admin_client.get("/api/admin/models", params={"limit": 5})).json()
        assert len(body["items"]) == 5
        assert body["total"] == 21  # the twenty plus the seeded one
        assert body["limit"] == 5
        assert body["offset"] == 0

    async def test_the_default_limit_is_applied_without_being_asked(
        self, admin_client: httpx.AsyncClient, session: AsyncSession, seeded: Seeded
    ) -> None:
        """An un-paginated client must not be able to pull the whole table."""
        await _add_models(session, seeded, DEFAULT_LIMIT + 10)

        body = (await admin_client.get("/api/admin/models")).json()
        assert len(body["items"]) == DEFAULT_LIMIT
        assert body["total"] == DEFAULT_LIMIT + 11


class TestWalking:
    async def test_paging_through_yields_every_row_exactly_once(
        self, admin_client: httpx.AsyncClient, session: AsyncSession, seeded: Seeded
    ) -> None:
        names = await _add_models(session, seeded, 23)

        walked = [row["name"] for row in await _walk(admin_client, "/api/admin/models", limit=7)]

        assert walked == sorted([*names, seeded.model.name])
        assert len(walked) == len(set(walked))

    async def test_a_page_boundary_that_lands_exactly_on_the_total_terminates(
        self, admin_client: httpx.AsyncClient, session: AsyncSession, seeded: Seeded
    ) -> None:
        """24 rows in pages of 8: the third page is full and there is no fourth."""
        await _add_models(session, seeded, 23)

        third = (
            await admin_client.get("/api/admin/models", params={"limit": 8, "offset": 16})
        ).json()
        fourth = (
            await admin_client.get("/api/admin/models", params={"limit": 8, "offset": 24})
        ).json()

        assert len(third["items"]) == 8
        assert fourth["items"] == []
        assert fourth["total"] == 24

    async def test_an_offset_past_the_end_still_reports_the_true_total(
        self, admin_client: httpx.AsyncClient, session: AsyncSession, seeded: Seeded
    ) -> None:
        """Otherwise a client that overshoots concludes the table is empty."""
        await _add_models(session, seeded, 5)

        body = (await admin_client.get("/api/admin/models", params={"offset": 1000})).json()
        assert body["items"] == []
        assert body["total"] == 6


class TestBounds:
    @pytest.mark.parametrize(
        "params",
        [
            {"limit": 0},
            {"limit": -1},
            {"limit": MAX_LIMIT + 1},
            {"offset": -1},
        ],
    )
    async def test_an_impossible_window_is_refused(
        self, admin_client: httpx.AsyncClient, seeded: Seeded, params: dict[str, int]
    ) -> None:
        """Refused, not clamped.

        Silently returning fewer rows than asked for is how a client ends up
        treating a truncated list as complete. A rejection is a bug report.

        400 rather than FastAPI's 422: the app rewrites validation failures
        into the OpenAI error shape, so every bad request on this surface
        answers the same way.
        """
        response = await admin_client.get("/api/admin/models", params=params)
        assert response.status_code == 400
        assert "limit" in response.text or "offset" in response.text

    async def test_the_ceiling_itself_is_allowed(
        self, admin_client: httpx.AsyncClient, seeded: Seeded
    ) -> None:
        response = await admin_client.get("/api/admin/models", params={"limit": MAX_LIMIT})
        assert response.status_code == 200
        assert response.json()["limit"] == MAX_LIMIT


class TestSearch:
    async def test_a_search_narrows_the_total_and_not_only_the_page(
        self, admin_client: httpx.AsyncClient, session: AsyncSession, seeded: Seeded
    ) -> None:
        """The whole point of moving the filter to the server.

        A filter applied to the page would leave `total` at the unfiltered
        count, and the console would offer pages that do not exist.
        """
        await _add_models(session, seeded, 20)

        body = (await admin_client.get("/api/admin/models", params={"q": "bulk-model-01"})).json()
        assert body["total"] == 10  # 010 through 019
        assert all("bulk-model-01" in row["name"] for row in body["items"])

    async def test_a_search_matches_the_upstream_name_too(
        self, admin_client: httpx.AsyncClient, seeded: Seeded
    ) -> None:
        """An operator reconciling with a provider's list has their names, not ours."""
        body = (await admin_client.get("/api/admin/models", params={"q": "upstream/test"})).json()
        assert [row["name"] for row in body["items"]] == [seeded.model.name]

    async def test_a_search_ignores_case(
        self, admin_client: httpx.AsyncClient, seeded: Seeded
    ) -> None:
        body = (await admin_client.get("/api/admin/models", params={"q": "TEST-MODEL"})).json()
        assert body["total"] == 1

    async def test_a_wildcard_in_the_search_term_is_a_literal(
        self, admin_client: httpx.AsyncClient, session: AsyncSession, seeded: Seeded
    ) -> None:
        """`_` is LIKE's single-character wildcard.

        Unescaped, a search for `test_model` would also match `test-model` —
        which is exactly the sort of near-miss that makes an operator think a
        model exists twice.
        """
        session.add(
            ModelDef(
                name="test_model",
                upstream_model="upstream/underscored",
                provider_id=seeded.provider.id,
            )
        )
        await session.commit()

        body = (await admin_client.get("/api/admin/models", params={"q": "test_model"})).json()
        assert [row["name"] for row in body["items"]] == ["test_model"]

        percent = (await admin_client.get("/api/admin/models", params={"q": "%"})).json()
        assert percent["total"] == 0

    async def test_a_blank_search_is_not_a_filter(
        self, admin_client: httpx.AsyncClient, seeded: Seeded
    ) -> None:
        """The console sends `q=` while the box is empty."""
        body = (await admin_client.get("/api/admin/models", params={"q": "   "})).json()
        assert body["total"] == 1

    async def test_users_can_be_searched_by_email_and_by_subject(
        self, admin_client: httpx.AsyncClient, session: AsyncSession, seeded: Seeded
    ) -> None:
        await _add_users(session, 12)

        by_email = (await admin_client.get("/api/admin/users", params={"q": "bulk-00"})).json()
        assert by_email["total"] == 10

        # No email claim from the IdP leaves the subject as the only handle.
        by_subject = (
            await admin_client.get("/api/admin/users", params={"q": "bulk-subject-11"})
        ).json()
        assert [row["subject"] for row in by_subject["items"]] == ["bulk-subject-11"]

    async def test_users_can_be_filtered_by_active(
        self, admin_client: httpx.AsyncClient, session: AsyncSession, seeded: Seeded
    ) -> None:
        await _add_users(session, 4)
        user = (await admin_client.get("/api/admin/users", params={"q": "bulk-000"})).json()[
            "items"
        ][0]
        await admin_client.patch(f"/api/admin/users/{user['id']}", json={"is_active": False})

        inactive = (await admin_client.get("/api/admin/users", params={"is_active": False})).json()
        assert [row["email"] for row in inactive["items"]] == ["bulk-000@example.org"]


class TestSlicedInMemory:
    """Prices and quota rules are windowed in Python, and must behave the same."""

    async def test_a_price_history_paginates(
        self, admin_client: httpx.AsyncClient, session: AsyncSession, seeded: Seeded
    ) -> None:
        for day in range(1, 6):
            session.add(
                ModelPrice(
                    model_id=seeded.model.id,
                    input_per_mtok=Decimal(day),
                    output_per_mtok=Decimal(day * 2),
                    currency="EUR",
                    effective_from=utcnow().replace(year=2020 + day),
                )
            )
        await session.commit()

        path = f"/api/admin/models/{seeded.model.id}/prices"
        body = (await admin_client.get(path, params={"limit": 2})).json()
        assert len(body["items"]) == 2
        assert body["total"] == 6  # the seeded price plus five

        walked = await _walk(admin_client, path, limit=2)
        assert len(walked) == 6
        assert len({row["id"] for row in walked}) == 6

    async def test_quota_rules_paginate_with_their_live_values(
        self, admin_client: httpx.AsyncClient, session: AsyncSession, seeded: Seeded
    ) -> None:
        """The counter read must not be lost to the windowing."""
        # One rule per scope, metric and window is enforced by a unique index,
        # so the windows have to differ for four rules to exist at all.
        for index in range(4):
            session.add(
                LimitRule(
                    name=f"rule-{index}",
                    scope=LimitScope.GROUP,
                    scope_id=seeded.group.id,
                    metric=LimitMetric.COST,
                    window_seconds=3600 + index,
                    limit_value=Decimal("10"),
                )
            )
        await session.commit()

        body = (await admin_client.get("/api/admin/limits", params={"limit": 3})).json()
        assert body["total"] == 4
        assert len(body["items"]) == 3
        assert all("current_value" in row for row in body["items"])


class TestListingsThatMustNotTruncate:
    async def test_a_report_returns_every_row_it_aggregated(
        self, admin_client: httpx.AsyncClient, session: AsyncSession, seeded: Seeded
    ) -> None:
        """An aggregation is a total. Truncating one produces a wrong number
        that looks exactly like a right one."""
        response = await admin_client.get(
            "/api/admin/reports/usage", params={"limit": 1, "offset": 0}
        )
        assert response.status_code == 200
        assert "items" not in response.json()
        assert "rows" in response.json()

    async def test_the_openai_model_list_keeps_its_documented_shape(
        self, client: httpx.AsyncClient, seeded: Seeded
    ) -> None:
        """`/v1/models` is somebody else's schema, and it is bounded by what a
        single caller has been granted."""
        body = (await client.get("/v1/models", headers=seeded.auth)).json()
        assert body["object"] == "list"
        assert isinstance(body["data"], list)


class TestEditingOffThePage:
    async def test_a_user_edited_from_a_later_page_is_still_returned(
        self, admin_client: httpx.AsyncClient, session: AsyncSession, seeded: Seeded
    ) -> None:
        """The regression pagination introduces.

        The PATCH used to answer by re-reading the listing and picking its row
        out of it, which stops working the moment the listing is a page: the
        user just edited is very often not on the first one.
        """
        await _add_users(session, DEFAULT_LIMIT + 20)
        last = (
            await admin_client.get("/api/admin/users", params={"q": "bulk-06", "limit": MAX_LIMIT})
        ).json()["items"][-1]

        response = await admin_client.patch(
            f"/api/admin/users/{last['id']}", json={"is_active": False}
        )

        assert response.status_code == 200
        assert response.json()["id"] == last["id"]
        assert response.json()["is_active"] is False

    async def test_a_group_a_user_bills_to_is_still_named_on_a_later_page(
        self, admin_client: httpx.AsyncClient, session: AsyncSession, seeded: Seeded
    ) -> None:
        """The billing group names are looked up for the page's users only, so
        the lookup has to follow the page rather than the first fifty groups."""
        await _add_users(session, 60)

        body = (
            await admin_client.get("/api/admin/users", params={"q": "member@example.org"})
        ).json()
        assert body["items"][0]["default_billing_group"] == seeded.group.name


class TestGroupsAndProviders:
    async def test_groups_paginate_and_keep_their_counts(
        self, admin_client: httpx.AsyncClient, session: AsyncSession, seeded: Seeded
    ) -> None:
        for index in range(30):
            session.add(Group(name=f"bulk-group-{index:03d}"))
        await session.commit()

        walked = await _walk(admin_client, "/api/admin/groups", limit=9)
        assert len(walked) == 31
        seeded_row = next(row for row in walked if row["name"] == seeded.group.name)
        assert seeded_row["member_count"] == 1
        assert seeded_row["models"] == [seeded.model.name]

    async def test_a_provider_page_keeps_its_model_count(
        self, admin_client: httpx.AsyncClient, session: AsyncSession, seeded: Seeded
    ) -> None:
        await _add_models(session, seeded, 3)

        body = (await admin_client.get("/api/admin/providers")).json()
        assert body["total"] == 1
        assert body["items"][0]["model_count"] == 4

    async def test_models_can_be_narrowed_to_one_provider(
        self, admin_client: httpx.AsyncClient, session: AsyncSession, seeded: Seeded
    ) -> None:
        await _add_models(session, seeded, 3)

        body = (
            await admin_client.get(
                "/api/admin/models", params={"provider_id": str(seeded.provider.id)}
            )
        ).json()
        assert body["total"] == 4

        other = (
            await admin_client.get("/api/admin/models", params={"provider_id": str(uuid.uuid4())})
        ).json()
        assert other["total"] == 0


class TestOwnKeys:
    async def test_a_caller_pages_their_own_keys(
        self, app: object, client: httpx.AsyncClient, seeded: Seeded
    ) -> None:
        as_user(app, seeded.user)
        for index in range(5):
            await client.post("/api/me/keys", json={"name": f"key-{index}"})

        body = (await client.get("/api/me/keys", params={"limit": 2})).json()
        assert body["total"] == 6  # the seeded key plus five
        assert len(body["items"]) == 2

        walked = await _walk(client, "/api/me/keys", limit=2)
        assert len({row["id"] for row in walked}) == 6
