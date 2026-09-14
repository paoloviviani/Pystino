"""``POST /v1/search``: one shape in, one shape out, through the group policy.

The unified route revisits ADR 0071's refusal to translate, narrowly: the
billing group's policy (``groups.search_model_id``) names the one backend
that runs, the plugin rebuilds the vendor's own request at its default depth,
and the vendor's own answer comes back normalised to title, URL and snippet.
What the tests hold is that the policy — not the request — picks the backend;
that the translation is honest in both directions; and that the metering is
the same count-before-the-call the passthrough keeps.
"""

from __future__ import annotations

import uuid
from decimal import Decimal
from typing import Any

import httpx
from conftest import FakeUpstream, Seeded
from gateway.models import (
    ApiSurface,
    Group,
    GroupModelAccess,
    LimitMetric,
    LimitRule,
    LimitScope,
    ModelDef,
    UsageRecord,
    UsageStatus,
)
from gateway.redaction import RedactionOutcome
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from test_oidc_policy import admin_session as admin_session
from test_search_surface import add_backend, exa_body, jina_body, latest_record, linkup_body

SEARCH = LimitMetric.OWN_SEARCH_REQUESTS


async def set_policy(session: AsyncSession, group_id: uuid.UUID, model: ModelDef | None) -> None:
    """Point the group's unified-search policy at a backend, the way the
    admin route would after validating it. Loaded fresh rather than mutated
    through the fixture object, so no test depends on whose session the
    fixture row is bound to."""
    group = await session.get(Group, group_id)
    assert group is not None
    group.search_model_id = model.id if model is not None else None
    await session.commit()


class TestPolicyResolution:
    async def test_no_policy_is_a_404_naming_the_missing_configuration(
        self,
        client: httpx.AsyncClient,
        seeded: Seeded,
        session: AsyncSession,
        fake_upstream: FakeUpstream,
    ) -> None:
        """A group with no policy cannot use the unified route at all — and
        nothing is counted, because nothing could run."""
        await add_backend(session, seeded)
        fake_upstream.set_json(linkup_body())

        response = await client.post(
            "/v1/search", json={"query": "european cloud providers"}, headers=seeded.auth
        )
        assert response.status_code == 404, response.text
        assert response.json()["error"]["code"] == "no_search_provider"
        assert len(fake_upstream.bodies) == 0
        rows = (await session.execute(select(UsageRecord))).scalars().all()
        assert rows == []

    async def test_the_policy_backend_runs_at_default_depth(
        self,
        client: httpx.AsyncClient,
        seeded: Seeded,
        session: AsyncSession,
        fake_upstream: FakeUpstream,
    ) -> None:
        """The request names no backend and no tier: the policy's backend runs
        at its default depth, and the count is what the caller asked for."""
        anchor = await add_backend(session, seeded)
        await set_policy(session, seeded.group.id, anchor)
        fake_upstream.set_json(linkup_body())

        response = await client.post(
            "/v1/search",
            json={"query": "european cloud providers", "max_results": 7},
            headers=seeded.auth,
        )
        assert response.status_code == 200, response.text
        assert fake_upstream.last_body == {
            "q": "european cloud providers",
            "depth": "standard",
            "outputType": "searchResults",
            "maxResults": 7,
        }

    async def test_exa_runs_in_its_own_dialect(
        self,
        client: httpx.AsyncClient,
        seeded: Seeded,
        session: AsyncSession,
        fake_upstream: FakeUpstream,
    ) -> None:
        """The same unified body reaches Exa as ``query``/``numResults`` —
        the caller never learns which dialect ran."""
        anchor = await add_backend(session, seeded, plugin="exa")
        await set_policy(session, seeded.group.id, anchor)
        fake_upstream.set_json(exa_body())

        response = await client.post(
            "/v1/search", json={"query": "capital of france"}, headers=seeded.auth
        )
        assert response.status_code == 200, response.text
        assert fake_upstream.last_body == {"query": "capital of france", "numResults": 5}

    async def test_jina_runs_as_a_post_with_a_json_body_not_a_get(
        self,
        client: httpx.AsyncClient,
        seeded: Seeded,
        session: AsyncSession,
        fake_upstream: FakeUpstream,
    ) -> None:
        """Jina's documented search shape is ``POST`` with ``q``/``num`` —
        not the GET with ``?q=`` the motivating example showed — so the query
        travels as data and never reaches a URL, like every other backend."""
        anchor = await add_backend(session, seeded, plugin="jina")
        await set_policy(session, seeded.group.id, anchor)
        fake_upstream.set_json(jina_body())

        response = await client.post(
            "/v1/search",
            json={"query": "Jina AI", "max_results": 7},
            headers=seeded.auth,
        )
        assert response.status_code == 200, response.text
        assert fake_upstream.last_body == {"q": "Jina AI", "num": 7}

    async def test_jina_excludes_page_content_and_authenticates_as_bearer(
        self,
        client: httpx.AsyncClient,
        seeded: Seeded,
        session: AsyncSession,
        fake_upstream: FakeUpstream,
    ) -> None:
        """The unified call carries ``X-Respond-With: no-content`` so Jina
        answers with titles and URLs rather than whole pages — plus the
        backend's own bearer credential."""
        anchor = await add_backend(session, seeded, plugin="jina", key="jina-key-1")
        await set_policy(session, seeded.group.id, anchor)
        fake_upstream.set_json(jina_body())

        response = await client.post("/v1/search", json={"query": "anything"}, headers=seeded.auth)
        assert response.status_code == 200, response.text
        sent = fake_upstream.headers[-1]
        assert sent.get("x-respond-with") == "no-content"
        assert sent.get("authorization") == "Bearer jina-key-1"

    async def test_a_jina_backend_pointed_at_the_eu_host_searches_through_it(
        self,
        client: httpx.AsyncClient,
        seeded: Seeded,
        session: AsyncSession,
        fake_upstream: FakeUpstream,
    ) -> None:
        """The unified route resolves the upstream from the provider row's
        base URL, so switching a backend to the vendor's documented EU host
        moves the request and nothing else about the call — same body, same
        headers, same count. The whole path is exercised because a base URL
        that looked honoured while a cached client still pointed at the old
        host would be exactly the failure this shape can hide."""
        anchor = await add_backend(
            session, seeded, plugin="jina", base_url="https://eu.s.jina.ai"
        )
        await set_policy(session, seeded.group.id, anchor)
        fake_upstream.set_json(jina_body())

        response = await client.post("/v1/search", json={"query": "anything"}, headers=seeded.auth)
        assert response.status_code == 200, response.text
        assert fake_upstream.urls[-1] == "https://eu.s.jina.ai/"
        assert fake_upstream.last_body == {"q": "anything", "num": 5}
        sent = fake_upstream.headers[-1]
        assert sent.get("x-respond-with") == "no-content"
        record = await latest_record(session)
        assert record.own_search_requests == 1
        assert record.own_search_backend == "jina"

    async def test_a_policy_pointing_nowhere_granted_is_a_404(
        self,
        client: httpx.AsyncClient,
        seeded: Seeded,
        session: AsyncSession,
        fake_upstream: FakeUpstream,
    ) -> None:
        """Revoking the grant out from under a policy (which the admin route
        refuses, so this is built by hand) leaves the backend invisible — a
        404, not a 403 — and the vendor is never called."""
        anchor = await add_backend(session, seeded)
        await set_policy(session, seeded.group.id, anchor)
        await session.execute(
            GroupModelAccess.__table__.delete().where(
                GroupModelAccess.group_id == seeded.group.id,
                GroupModelAccess.model_id == anchor.id,
            )
        )
        await session.commit()
        fake_upstream.set_json(linkup_body())

        response = await client.post("/v1/search", json={"query": "anything"}, headers=seeded.auth)
        assert response.status_code == 404, response.text
        assert len(fake_upstream.bodies) == 0

    async def test_unknown_fields_are_refused_not_dropped(
        self,
        client: httpx.AsyncClient,
        seeded: Seeded,
        session: AsyncSession,
    ) -> None:
        """The unified body is rebuilt, not forwarded: an unrecognised field
        would be silently dropped, so it is refused instead."""
        anchor = await add_backend(session, seeded)
        await set_policy(session, seeded.group.id, anchor)

        response = await client.post(
            "/v1/search",
            json={"query": "anything", "type": "deep-reasoning"},
            headers=seeded.auth,
        )
        assert response.status_code == 400, response.text

    async def test_bounds_are_checked_here_not_upstream(
        self,
        client: httpx.AsyncClient,
        seeded: Seeded,
        session: AsyncSession,
        fake_upstream: FakeUpstream,
    ) -> None:
        anchor = await add_backend(session, seeded)
        await set_policy(session, seeded.group.id, anchor)
        fake_upstream.set_json(linkup_body())

        out_of_bounds = [
            {"query": "anything", "max_results": 0},
            {"query": "anything", "max_results": 11},
        ]
        for body in out_of_bounds:
            response = await client.post("/v1/search", json=body, headers=seeded.auth)
            assert response.status_code == 400, response.text
        assert len(fake_upstream.bodies) == 0


class TestNormalisedAnswer:
    async def test_linkup_answers_come_back_as_title_url_snippet(
        self,
        client: httpx.AsyncClient,
        seeded: Seeded,
        session: AsyncSession,
        fake_upstream: FakeUpstream,
    ) -> None:
        """Linkup's ``name``/``content`` arrive as title/snippet, and the
        backend is named because one search never mixes backends."""
        anchor = await add_backend(session, seeded)
        await set_policy(session, seeded.group.id, anchor)
        fake_upstream.set_json(linkup_body(count=2))

        response = await client.post("/v1/search", json={"query": "anything"}, headers=seeded.auth)
        assert response.status_code == 200, response.text
        assert response.json() == {
            "results": [
                {
                    "title": "Result 0",
                    "url": "https://example.org/0",
                    "snippet": "An extract from result 0.",
                },
                {
                    "title": "Result 1",
                    "url": "https://example.org/1",
                    "snippet": "An extract from result 1.",
                },
            ],
            "backend": "linkup",
        }

    async def test_exa_answers_come_back_in_the_same_shape(
        self,
        client: httpx.AsyncClient,
        seeded: Seeded,
        session: AsyncSession,
        fake_upstream: FakeUpstream,
    ) -> None:
        anchor = await add_backend(session, seeded, plugin="exa")
        await set_policy(session, seeded.group.id, anchor)
        fake_upstream.set_json(
            {
                "requestId": "req-exa-9",
                "results": [
                    {"title": "Titled", "url": "https://example.org/a", "summary": "Short."},
                    {"title": "Texty", "url": "https://example.org/b", "text": "Longer."},
                    {"title": "Bare", "url": "https://example.org/c"},
                ],
            }
        )

        response = await client.post("/v1/search", json={"query": "anything"}, headers=seeded.auth)
        assert response.status_code == 200, response.text
        assert response.json() == {
            "results": [
                {"title": "Titled", "url": "https://example.org/a", "snippet": "Short."},
                {"title": "Texty", "url": "https://example.org/b", "snippet": "Longer."},
                {"title": "Bare", "url": "https://example.org/c", "snippet": ""},
            ],
            "backend": "exa",
        }

    async def test_jina_answers_come_back_in_the_same_shape(
        self,
        client: httpx.AsyncClient,
        seeded: Seeded,
        session: AsyncSession,
        fake_upstream: FakeUpstream,
    ) -> None:
        """Jina's ``data[].title``/``description`` arrive as title/snippet —
        with the page text as the fallback for an answer that carried content
        despite the header, and an empty snippet where there is neither."""
        anchor = await add_backend(session, seeded, plugin="jina")
        await set_policy(session, seeded.group.id, anchor)
        fake_upstream.set_json(
            {
                "code": 200,
                "status": 20000,
                "data": [
                    {
                        "title": "Described",
                        "url": "https://example.org/a",
                        "description": "Short.",
                        "content": "Longer.",
                    },
                    {
                        "title": "Texty",
                        "url": "https://example.org/b",
                        "content": "Longer.",
                    },
                    {"title": "Bare", "url": "https://example.org/c"},
                    {"title": "Nowhere", "description": "No URL."},
                ],
            }
        )

        response = await client.post("/v1/search", json={"query": "anything"}, headers=seeded.auth)
        assert response.status_code == 200, response.text
        assert response.json() == {
            "results": [
                {"title": "Described", "url": "https://example.org/a", "snippet": "Short."},
                {"title": "Texty", "url": "https://example.org/b", "snippet": "Longer."},
                {"title": "Bare", "url": "https://example.org/c", "snippet": ""},
            ],
            "backend": "jina",
        }

    async def test_a_jina_envelope_that_is_not_a_list_is_an_empty_answer(
        self,
        client: httpx.AsyncClient,
        seeded: Seeded,
        session: AsyncSession,
        fake_upstream: FakeUpstream,
    ) -> None:
        """A 2xx whose ``data`` is not a list still counts — the vendor
        received the request — but answers nothing rather than failing."""
        anchor = await add_backend(session, seeded, plugin="jina")
        await set_policy(session, seeded.group.id, anchor)
        fake_upstream.set_json({"code": 200, "status": 20000, "data": "not a list"})

        response = await client.post("/v1/search", json={"query": "anything"}, headers=seeded.auth)
        assert response.status_code == 200, response.text
        assert response.json() == {"results": [], "backend": "jina"}
        record = await latest_record(session)
        assert record.own_search_requests == 1
        assert record.status is UsageStatus.COMPLETED

    async def test_entries_without_a_url_are_dropped_not_repaired(
        self,
        client: httpx.AsyncClient,
        seeded: Seeded,
        session: AsyncSession,
        fake_upstream: FakeUpstream,
    ) -> None:
        anchor = await add_backend(session, seeded)
        await set_policy(session, seeded.group.id, anchor)
        fake_upstream.set_json(
            {
                "results": [
                    {"name": "Kept", "url": "https://example.org/kept", "content": "Yes."},
                    {"name": "No URL", "content": "Nowhere to fetch."},
                    {"name": "Empty URL", "url": "", "content": "Also nowhere."},
                    "not even a dict",
                    {"url": "https://example.org/untitled"},
                ]
            }
        )

        response = await client.post("/v1/search", json={"query": "anything"}, headers=seeded.auth)
        assert response.status_code == 200, response.text
        assert response.json() == {
            "results": [
                {"title": "Kept", "url": "https://example.org/kept", "snippet": "Yes."},
                {"title": "untitled", "url": "https://example.org/untitled", "snippet": ""},
            ],
            "backend": "linkup",
        }

    async def test_an_unreadable_answer_is_an_empty_answer_not_an_error(
        self,
        client: httpx.AsyncClient,
        seeded: Seeded,
        session: AsyncSession,
        fake_upstream: FakeUpstream,
    ) -> None:
        """A 2xx the reader cannot parse still counts — the vendor received
        the request — but answers nothing rather than failing the turn."""
        anchor = await add_backend(session, seeded)
        await set_policy(session, seeded.group.id, anchor)
        fake_upstream.set_json({"results": "not a list"})

        response = await client.post("/v1/search", json={"query": "anything"}, headers=seeded.auth)
        assert response.status_code == 200, response.text
        assert response.json() == {"results": [], "backend": "linkup"}
        record = await latest_record(session)
        assert record.own_search_requests == 1
        assert record.status is UsageStatus.COMPLETED


class TestMetering:
    async def test_one_search_is_one_count_with_no_cost(
        self,
        client: httpx.AsyncClient,
        seeded: Seeded,
        session: AsyncSession,
        fake_upstream: FakeUpstream,
    ) -> None:
        anchor = await add_backend(session, seeded)
        await set_policy(session, seeded.group.id, anchor)
        fake_upstream.set_json(linkup_body())

        response = await client.post("/v1/search", json={"query": "anything"}, headers=seeded.auth)
        assert response.status_code == 200

        record = await latest_record(session)
        assert record.api_surface is ApiSurface.SEARCH
        assert record.status is UsageStatus.COMPLETED
        assert record.own_search_requests == 1
        assert record.own_search_backend == "linkup"
        # The unified route offers no tier, so the ledger records none.
        assert record.own_search_tier is None
        assert record.cost == Decimal(0)
        assert record.total_tokens == 0

    async def test_a_jina_search_is_one_count_with_no_cost(
        self,
        client: httpx.AsyncClient,
        seeded: Seeded,
        session: AsyncSession,
        fake_upstream: FakeUpstream,
    ) -> None:
        """The same ledger shape through Jina: one request counted, no price —
        Jina bills its own key in tokens, which is the vendor's business."""
        anchor = await add_backend(session, seeded, plugin="jina")
        await set_policy(session, seeded.group.id, anchor)
        fake_upstream.set_json(jina_body())

        response = await client.post("/v1/search", json={"query": "anything"}, headers=seeded.auth)
        assert response.status_code == 200

        record = await latest_record(session)
        assert record.api_surface is ApiSurface.SEARCH
        assert record.status is UsageStatus.COMPLETED
        assert record.own_search_requests == 1
        assert record.own_search_backend == "jina"
        assert record.own_search_tier is None
        assert record.cost == Decimal(0)
        assert record.total_tokens == 0

    async def test_a_refused_search_keeps_its_row_and_its_count(
        self,
        client: httpx.AsyncClient,
        seeded: Seeded,
        session: AsyncSession,
        fake_upstream: FakeUpstream,
    ) -> None:
        anchor = await add_backend(session, seeded)
        await set_policy(session, seeded.group.id, anchor)
        fake_upstream.set_json({"error": {"message": "insufficient credits"}}, status=402)

        response = await client.post("/v1/search", json={"query": "anything"}, headers=seeded.auth)
        assert response.status_code == 402
        # The vendor's own error, verbatim — the administrator pointed at
        # this backend, so they are the one who can act on it.
        assert response.json() == {"error": {"message": "insufficient credits"}}

        record = await latest_record(session)
        assert record.own_search_requests == 1
        assert record.status is not UsageStatus.COMPLETED

    async def test_a_refused_jina_search_keeps_its_count(
        self,
        client: httpx.AsyncClient,
        seeded: Seeded,
        session: AsyncSession,
        fake_upstream: FakeUpstream,
    ) -> None:
        """Counted before the call and never unwound — a vendor error still
        leaves the count, because a ceiling that forgave a failure would be
        raisable by making the search fail."""
        anchor = await add_backend(session, seeded, plugin="jina")
        await set_policy(session, seeded.group.id, anchor)
        fake_upstream.set_json({"error": {"message": "insufficient balance"}}, status=402)

        response = await client.post("/v1/search", json={"query": "anything"}, headers=seeded.auth)
        assert response.status_code == 402
        assert response.json() == {"error": {"message": "insufficient balance"}}

        record = await latest_record(session)
        assert record.own_search_requests == 1
        assert record.own_search_backend == "jina"
        assert record.status is not UsageStatus.COMPLETED

    async def test_a_search_rule_refuses_before_the_backend_is_called(
        self,
        client: httpx.AsyncClient,
        seeded: Seeded,
        session: AsyncSession,
        fake_upstream: FakeUpstream,
    ) -> None:
        anchor = await add_backend(session, seeded)
        await set_policy(session, seeded.group.id, anchor)
        async with session.begin():
            session.add(
                LimitRule(
                    name="searches",
                    scope=LimitScope.GROUP,
                    scope_id=seeded.group.id,
                    metric=SEARCH,
                    window_seconds=3600,
                    limit_value=Decimal(1),
                )
            )
        fake_upstream.set_json(linkup_body())

        assert (
            await client.post("/v1/search", json={"query": "one"}, headers=seeded.auth)
        ).status_code == 200
        calls_before = len(fake_upstream.bodies)
        response = await client.post("/v1/search", json={"query": "two"}, headers=seeded.auth)
        assert response.status_code == 429
        assert len(fake_upstream.bodies) == calls_before


class TestQueryRedaction:
    async def test_the_query_is_screened_before_the_vendor_sees_it(
        self,
        app: object,
        client: httpx.AsyncClient,
        seeded: Seeded,
        session: AsyncSession,
        fake_upstream: FakeUpstream,
    ) -> None:
        """The unified route knows which field holds the query, so the
        standard engine screens it — recovering what the passthrough gives
        up by promising not to read the body."""
        anchor = await add_backend(session, seeded)
        await set_policy(session, seeded.group.id, anchor)
        fake_upstream.set_json(linkup_body())
        app.state.redactor = _ShoutyRedactor()  # type: ignore[attr-defined]

        response = await client.post(
            "/v1/search", json={"query": "contact Mario Rossi"}, headers=seeded.auth
        )
        assert response.status_code == 200, response.text
        assert fake_upstream.last_body["q"] == "contact <PERSON_X>"

    async def test_the_query_is_screened_before_jina_sees_it(
        self,
        app: object,
        client: httpx.AsyncClient,
        seeded: Seeded,
        session: AsyncSession,
        fake_upstream: FakeUpstream,
    ) -> None:
        """Jina's ``q`` carries the redacted query too — the route screens the
        one field it reads, whichever dialect the policy backend speaks."""
        anchor = await add_backend(session, seeded, plugin="jina")
        await set_policy(session, seeded.group.id, anchor)
        fake_upstream.set_json(jina_body())
        app.state.redactor = _ShoutyRedactor()  # type: ignore[attr-defined]

        response = await client.post(
            "/v1/search", json={"query": "contact Mario Rossi"}, headers=seeded.auth
        )
        assert response.status_code == 200, response.text
        assert fake_upstream.last_body == {"q": "contact <PERSON_X>", "num": 5}


class _ShoutyRedactor:
    """Replaces one fixed name, deterministically — the seam, not the policy."""

    name = "test"

    async def redact_request(
        self, messages: list[dict], *, policy: object | None = None
    ) -> RedactionOutcome:
        rewritten = []
        for message in messages:
            text = str(message.get("content") or "")
            text = text.replace("Mario Rossi", "<PERSON_X>")
            rewritten.append({**message, "content": text})
        return RedactionOutcome(messages=rewritten, entity_count=0, engine="test")

    def response_stage(self, outcome: RedactionOutcome):  # type: ignore[no-untyped-def]
        from gateway.sse.pipeline import passthrough

        return passthrough

    async def restore_response(self, text: str, outcome: RedactionOutcome) -> Any:
        from gateway.redaction import Restored

        return Restored(text=text, restored=False)


class TestAdminPolicy:
    async def test_setting_a_granted_backend_then_listing_shows_it(
        self,
        client: httpx.AsyncClient,
        seeded: Seeded,
        session: AsyncSession,
        admin_session: dict[str, str],
    ) -> None:
        anchor = await add_backend(session, seeded)
        response = await client.put(
            f"/api/admin/groups/{seeded.group.id}/search-backend",
            json={"model_id": str(anchor.id)},
            headers=admin_session,
        )
        assert response.status_code == 204, response.text

        listing = await client.get("/api/admin/groups", headers=admin_session)
        assert listing.status_code == 200
        row = next(item for item in listing.json()["items"] if item["id"] == str(seeded.group.id))
        assert row["search_backend"] == anchor.name

    async def test_setting_an_ungranted_backend_is_a_409(
        self,
        client: httpx.AsyncClient,
        seeded: Seeded,
        session: AsyncSession,
        admin_session: dict[str, str],
    ) -> None:
        anchor = await add_backend(session, seeded, granted=False)
        response = await client.put(
            f"/api/admin/groups/{seeded.group.id}/search-backend",
            json={"model_id": str(anchor.id)},
            headers=admin_session,
        )
        assert response.status_code == 409, response.text

    async def test_setting_a_non_search_model_is_a_400(
        self,
        client: httpx.AsyncClient,
        seeded: Seeded,
        admin_session: dict[str, str],
    ) -> None:
        response = await client.put(
            f"/api/admin/groups/{seeded.group.id}/search-backend",
            json={"model_id": str(seeded.model.id)},
            headers=admin_session,
        )
        assert response.status_code == 400, response.text

    async def test_clearing_the_policy_disables_the_route(
        self,
        client: httpx.AsyncClient,
        seeded: Seeded,
        session: AsyncSession,
        fake_upstream: FakeUpstream,
        admin_session: dict[str, str],
    ) -> None:
        anchor = await add_backend(session, seeded)
        await set_policy(session, seeded.group.id, anchor)
        fake_upstream.set_json(linkup_body())
        assert (
            await client.post("/v1/search", json={"query": "one"}, headers=seeded.auth)
        ).status_code == 200

        cleared = await client.put(
            f"/api/admin/groups/{seeded.group.id}/search-backend",
            json={"model_id": None},
            headers=admin_session,
        )
        assert cleared.status_code == 204, cleared.text
        assert (
            await client.post("/v1/search", json={"query": "two"}, headers=seeded.auth)
        ).status_code == 404

    async def test_revoking_a_policy_backend_is_blocked_until_repointed(
        self,
        client: httpx.AsyncClient,
        seeded: Seeded,
        session: AsyncSession,
        admin_session: dict[str, str],
    ) -> None:
        anchor = await add_backend(session, seeded)
        await set_policy(session, seeded.group.id, anchor)

        refused = await client.delete(
            f"/api/admin/groups/{seeded.group.id}/models/{anchor.id}",
            headers=admin_session,
        )
        assert refused.status_code == 409, refused.text

        # Other groups' grants are untouched by the guard: revoking elsewhere
        # still works.
        other = await client.delete(
            f"/api/admin/groups/{seeded.group.id}/models/{seeded.model.id}",
            headers=admin_session,
        )
        assert other.status_code == 204, other.text

    async def test_deleting_a_policy_backend_is_blocked(
        self,
        client: httpx.AsyncClient,
        seeded: Seeded,
        session: AsyncSession,
        admin_session: dict[str, str],
    ) -> None:
        anchor = await add_backend(session, seeded)
        await set_policy(session, seeded.group.id, anchor)

        refused = await client.delete(f"/api/admin/models/{anchor.id}", headers=admin_session)
        assert refused.status_code == 409, refused.text

    async def test_unknown_group_is_404(
        self,
        client: httpx.AsyncClient,
        admin_session: dict[str, str],
    ) -> None:
        missing = uuid.uuid4()
        response = await client.put(
            f"/api/admin/groups/{missing}/search-backend",
            json={"model_id": None},
            headers=admin_session,
        )
        assert response.status_code == 404, response.text

    async def test_unknown_model_is_404(
        self,
        client: httpx.AsyncClient,
        seeded: Seeded,
        admin_session: dict[str, str],
    ) -> None:
        response = await client.put(
            f"/api/admin/groups/{seeded.group.id}/search-backend",
            json={"model_id": str(uuid.uuid4())},
            headers=admin_session,
        )
        assert response.status_code == 404, response.text
