"""``POST /v1/search``: one surface, metered by the request and never priced.

What is pinned here is everything that would otherwise be found on a vendor's
invoice or in an unenforced ceiling. Ground rule 3 applies with its full
weight: a wrong answer in this file is a search budget that does not hold.

Four properties carry it, and each has a test that fails if the property is
removed.

**The count reaches the ledger, in its own column.** ``own_search_requests``
is ours; ``search_count`` is the counterparty's server-side search from ADR
0058. Every test that touches a row asserts both, because summing them is the
mistake that is easy to make and invisible afterwards.

**The tier label is the one we asked for.** It comes from the model row, which
is what makes a depth a grant rather than a request parameter — and neither
vendor reports a trustworthy one back.

**A failed search still counts.** Vendors bill requests received, and a
ceiling that forgave a failure would be raisable by making the search fail.
Three failure shapes are covered: the vendor refusing, the vendor being
unreachable, and the vendor answering 2xx with a body we cannot read.

**Nothing a caller sends reaches the address.** ``backend_options`` is merged
*under* the fields the plugin controls, so it can neither change the tier that
was reserved against nor the output shape the reader expects — and it is a
JSON body in every case, never a URL.

No vendor is called. Both backends are driven through the same
``MockTransport`` the rest of the suite uses: a test that needs the internet is
not a test, and we hold no keys for either of them.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any

import httpx
import orjson
import pytest
from conftest import UPSTREAM_BASE, FakeUpstream, Seeded
from gateway.models import (
    ApiSurface,
    GroupModelAccess,
    LimitMetric,
    LimitRule,
    LimitScope,
    ModelDef,
    ModelKind,
    Provider,
    ProviderKind,
    UsageRecord,
    UsageStatus,
)
from gateway.plugins import registry as plugin_registry
from gateway.plugins.search import SearchPlugin
from gateway.secrets import SecretBox, hint_for
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

SEARCH = LimitMetric.OWN_SEARCH_REQUESTS


# --------------------------------------------------------------------------
# fixtures in the shape an administrator would configure
# --------------------------------------------------------------------------


async def add_backend(
    session: AsyncSession,
    seeded: Seeded,
    *,
    plugin: str = "linkup",
    tier: str = "standard",
    name: str | None = None,
    key: str = "search-key",
) -> ModelDef:
    """A search provider and one of its tiers, granted to the seeded group.

    Deliberately built the way the admin API would build it — a `Provider` row
    with an encrypted credential and a `ModelDef` whose `upstream_model` is the
    tier — rather than through a shortcut, so the decrypt-and-send path and the
    access grant are both exercised.
    """
    box = SecretBox(["test-encryption-key-not-for-production"])
    provider = Provider(
        name=f"{plugin}-{tier}",
        # The fake upstream, standing in for the vendor's host. The plugin's
        # `search_path` is appended to it exactly as it would be in production.
        base_url=UPSTREAM_BASE,
        api_key_encrypted=box.encrypt(key),
        api_key_hint=hint_for(key),
        plugin=plugin,
        kind=ProviderKind.SEARCH,
    )
    session.add(provider)
    await session.flush()

    model = ModelDef(
        name=name or f"{plugin}-{tier}",
        # For a search backend this is the tier, and nothing else.
        upstream_model=tier,
        provider_id=provider.id,
        kind=ModelKind.SEARCH,
    )
    session.add(model)
    await session.flush()
    session.add(GroupModelAccess(group_id=seeded.group.id, model_id=model.id))
    await session.commit()
    return model


def linkup_body(count: int = 2, *, images: int = 0) -> dict[str, Any]:
    """Linkup's `searchResults` shape, as its OpenAPI document defines it.

    Note `name` rather than `title`, and that an image hit carries neither
    `content` nor `favicon`.
    """
    results: list[dict[str, Any]] = [
        {
            "type": "text",
            "name": f"Result {index}",
            "url": f"https://example.org/{index}",
            "content": f"An extract from result {index}.",
            "favicon": "https://example.org/favicon.ico",
        }
        for index in range(count)
    ]
    results += [
        {"type": "image", "name": f"Picture {index}", "url": f"https://example.org/i{index}.png"}
        for index in range(images)
    ]
    return {"results": results}


def exa_body(count: int = 2, *, text: bool = False) -> dict[str, Any]:
    """Exa's search response, including the `costDollars` object verbatim."""
    return {
        "requestId": "req-exa-1",
        "results": [
            {
                "title": f"Result {index}",
                "url": f"https://example.org/{index}",
                "id": f"https://example.org/{index}",
                "publishedDate": "2026-01-01",
                "author": "A Writer",
                "summary": f"A summary of result {index}.",
                **({"text": f"The full text of result {index}."} if text else {}),
            }
            for index in range(count)
        ],
        "costDollars": {"total": 0.007, "search": {"neural": 0.007}},
        "searchTime": 312.4,
    }


async def latest_record(session: AsyncSession) -> UsageRecord:
    return (
        (
            await session.execute(
                select(UsageRecord).order_by(UsageRecord.created_at.desc()).limit(1)
            )
        )
        .scalars()
        .one()
    )


# --------------------------------------------------------------------------
# the metering, which is the whole point of the surface
# --------------------------------------------------------------------------


class TestMetering:
    async def test_one_search_reaches_the_ledger_as_a_count_with_no_cost(
        self,
        client: httpx.AsyncClient,
        seeded: Seeded,
        session: AsyncSession,
        fake_upstream: FakeUpstream,
    ) -> None:
        model = await add_backend(session, seeded)
        fake_upstream.set_json(linkup_body())

        response = await client.post(
            "/v1/search",
            json={"model": model.name, "query": "european cloud providers"},
            headers=seeded.auth,
        )
        assert response.status_code == 200

        record = await latest_record(session)
        assert record.api_surface is ApiSurface.SEARCH
        assert record.status is UsageStatus.COMPLETED
        assert record.own_search_requests == 1
        assert record.own_search_backend == "linkup"
        assert record.own_search_tier == "standard"
        # Never priced. Not "priced at zero because the model has no price row"
        # — there is no rate anywhere for this to multiply.
        assert record.cost == Decimal(0)
        assert record.total_tokens == 0
        # Ours and theirs are different columns. Summing them would leave no
        # report able to tell "Anthropic searched" from "we called Linkup".
        assert record.search_count == 0

    async def test_two_searches_are_two_rows_and_two_counts(
        self,
        client: httpx.AsyncClient,
        seeded: Seeded,
        session: AsyncSession,
        fake_upstream: FakeUpstream,
    ) -> None:
        """The count is per request, not per result: five results is one search."""
        model = await add_backend(session, seeded)
        fake_upstream.set_json(linkup_body(count=5))

        for _ in range(2):
            assert (
                await client.post(
                    "/v1/search",
                    json={"model": model.name, "query": "anything"},
                    headers=seeded.auth,
                )
            ).status_code == 200

        rows = (
            (
                await session.execute(
                    select(UsageRecord).where(UsageRecord.api_surface == ApiSurface.SEARCH)
                )
            )
            .scalars()
            .all()
        )
        assert len(rows) == 2
        assert [row.own_search_requests for row in rows] == [1, 1]

    async def test_the_tier_recorded_is_the_one_asked_for(
        self,
        client: httpx.AsyncClient,
        seeded: Seeded,
        session: AsyncSession,
        fake_upstream: FakeUpstream,
    ) -> None:
        """From the model row, and it is also what went out on the wire.

        The two halves matter together: a label that did not match the request
        would put the cheap tier's name on a dear tier's invoice line.
        """
        model = await add_backend(session, seeded, tier="deep", name="linkup-deep")
        fake_upstream.set_json(linkup_body())

        await client.post(
            "/v1/search", json={"model": model.name, "query": "q"}, headers=seeded.auth
        )

        assert fake_upstream.last_body["depth"] == "deep"
        record = await latest_record(session)
        assert record.own_search_tier == "deep"

    async def test_the_response_reports_the_count_and_no_money(
        self,
        client: httpx.AsyncClient,
        seeded: Seeded,
        session: AsyncSession,
        fake_upstream: FakeUpstream,
    ) -> None:
        """Including Exa's `costDollars`, which is read and deliberately not passed on."""
        model = await add_backend(session, seeded, plugin="exa", tier="auto")
        fake_upstream.set_json(exa_body())

        response = await client.post(
            "/v1/search", json={"model": model.name, "query": "q"}, headers=seeded.auth
        )
        body = response.json()
        assert body["object"] == "search.results"
        assert body["backend"] == "exa"
        assert body["tier"] == "auto"
        assert body["search"] == {"requests": 1}
        # Our name for the model, never the tier that went upstream.
        assert body["model"] == model.name
        assert "cost" not in body
        assert "costDollars" not in orjson.dumps(body).decode()


# --------------------------------------------------------------------------
# a failed search is still a search
# --------------------------------------------------------------------------


class TestFailuresStillCount:
    async def test_a_refused_search_keeps_its_usage_row_and_its_count(
        self,
        client: httpx.AsyncClient,
        seeded: Seeded,
        session: AsyncSession,
        fake_upstream: FakeUpstream,
    ) -> None:
        """The vendor's own 429, passed through, with the search still counted.

        Counting only a 2xx would make the ceiling raisable by making the
        search fail — and the vendor bills the request either way.
        """
        model = await add_backend(session, seeded)
        fake_upstream.set_json({"error": "insufficient credits"}, status=429)

        response = await client.post(
            "/v1/search", json={"model": model.name, "query": "q"}, headers=seeded.auth
        )
        assert response.status_code == 429
        # Passed through rather than summarised.
        assert response.json() == {"error": "insufficient credits"}

        record = await latest_record(session)
        assert record.status is UsageStatus.UPSTREAM_ERROR
        assert record.upstream_status == 429
        assert record.own_search_requests == 1
        assert record.own_search_backend == "linkup"
        assert record.search_count == 0

    async def test_an_unreachable_backend_keeps_its_usage_row_and_its_count(
        self,
        client: httpx.AsyncClient,
        seeded: Seeded,
        session: AsyncSession,
        fake_upstream: FakeUpstream,
    ) -> None:
        """A transport failure, which is the case where "did they bill us?"
        genuinely cannot be known. Counted, because the alternative is a
        ceiling that a timeout resets."""
        model = await add_backend(session, seeded)

        def refuse(_: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("connection refused")

        fake_upstream._responder = refuse  # the harness's own seam

        response = await client.post(
            "/v1/search", json={"model": model.name, "query": "q"}, headers=seeded.auth
        )
        # 502, the same answer every other surface gives for a counterparty it
        # could not reach — not 503, which this gateway reserves for its own
        # dependencies being down.
        assert response.status_code == 502
        assert response.json()["error"]["code"] == "upstream_unavailable"

        record = await latest_record(session)
        assert record.status is UsageStatus.UPSTREAM_ERROR
        assert record.own_search_requests == 1

    async def test_a_2xx_body_we_cannot_read_is_a_502_and_still_counts(
        self,
        client: httpx.AsyncClient,
        seeded: Seeded,
        session: AsyncSession,
        fake_upstream: FakeUpstream,
    ) -> None:
        """An empty result list would record a successful search that found
        nothing, which nobody would ever investigate."""
        model = await add_backend(session, seeded)
        fake_upstream.set_json({"answer": "Linkup changed its shape"})

        response = await client.post(
            "/v1/search", json={"model": model.name, "query": "q"}, headers=seeded.auth
        )
        assert response.status_code == 502
        assert response.json()["error"]["code"] == "search_response_unreadable"

        record = await latest_record(session)
        assert record.own_search_requests == 1
        assert record.status is UsageStatus.UPSTREAM_ERROR
        # The vendor's real status, not the 502 we answered with. Recording the
        # 502 here would send whoever reconciles the invoice looking for a
        # vendor-side failure that never happened: they served the search.
        assert record.upstream_status == 200


# --------------------------------------------------------------------------
# quotas
# --------------------------------------------------------------------------


class TestQuota:
    async def test_a_search_rule_refuses_the_request_before_the_backend_is_called(
        self,
        client: httpx.AsyncClient,
        seeded: Seeded,
        session: AsyncSession,
        fake_upstream: FakeUpstream,
    ) -> None:
        model = await add_backend(session, seeded)
        session.add(
            LimitRule(
                name="no searches at all",
                scope=LimitScope.GROUP,
                scope_id=seeded.group.id,
                metric=SEARCH,
                window_seconds=3600,
                limit_value=Decimal(0),
            )
        )
        await session.commit()
        fake_upstream.set_json(linkup_body())

        response = await client.post(
            "/v1/search", json={"model": model.name, "query": "q"}, headers=seeded.auth
        )
        assert response.status_code == 429
        assert response.json()["error"]["code"] == "quota_exceeded"
        assert int(response.headers["retry-after"]) >= 1
        # Refused before any money was spent, which is what reserving first buys.
        assert fake_upstream.bodies == []

    async def test_the_budget_is_spent_by_searching_and_then_refuses(
        self,
        client: httpx.AsyncClient,
        seeded: Seeded,
        session: AsyncSession,
        fake_upstream: FakeUpstream,
    ) -> None:
        model = await add_backend(session, seeded)
        session.add(
            LimitRule(
                name="one search an hour",
                scope=LimitScope.GROUP,
                scope_id=seeded.group.id,
                metric=SEARCH,
                window_seconds=3600,
                limit_value=Decimal(1),
            )
        )
        await session.commit()
        fake_upstream.set_json(linkup_body())

        payload = {"model": model.name, "query": "q"}
        first = await client.post("/v1/search", json=payload, headers=seeded.auth)
        assert first.status_code == 200
        second = await client.post("/v1/search", json=payload, headers=seeded.auth)
        assert second.status_code == 429

    async def test_concurrent_searches_are_bounded_by_the_reservation(
        self,
        client: httpx.AsyncClient,
        seeded: Seeded,
        session: AsyncSession,
        fake_upstream: FakeUpstream,
    ) -> None:
        """What reserving actually buys, and the only test here that proves it.

        Found by mutation: setting ``worst_case_own_searches`` back to its
        default of zero left both *sequential* quota tests above passing. They
        were testing the settle, not the reservation — the engine refuses on
        ``current >= limit`` measured on what the window held *before* this
        request, so for one request at a time it makes no difference whether
        the count lands at reserve or at settle.

        It makes all the difference to two at once. With the reservation, each
        request increments before either is judged, so the second sees a full
        window and is refused. Without it, both read an empty window, both are
        admitted, and the ceiling of one is quietly a ceiling of two — which is
        the shape of every quota bug worth having a test for.
        """
        import asyncio

        model = await add_backend(session, seeded)
        session.add(
            LimitRule(
                name="one search an hour",
                scope=LimitScope.GROUP,
                scope_id=seeded.group.id,
                metric=SEARCH,
                window_seconds=3600,
                limit_value=Decimal(1),
            )
        )
        await session.commit()
        fake_upstream.set_json(linkup_body())

        payload = {"model": model.name, "query": "q"}
        first, second = await asyncio.gather(
            client.post("/v1/search", json=payload, headers=seeded.auth),
            client.post("/v1/search", json=payload, headers=seeded.auth),
        )
        assert sorted([first.status_code, second.status_code]) == [200, 429]

    async def test_an_ordinary_completion_does_not_spend_the_search_budget(
        self,
        client: httpx.AsyncClient,
        seeded: Seeded,
        session: AsyncSession,
        fake_upstream: FakeUpstream,
    ) -> None:
        """The asymmetry `QuotaAmounts` records: `requests` defaults to one and
        this defaults to zero, so a search budget is not exhausted by traffic
        that never searched."""
        model = await add_backend(session, seeded)
        session.add(
            LimitRule(
                name="one search an hour",
                scope=LimitScope.GROUP,
                scope_id=seeded.group.id,
                metric=SEARCH,
                window_seconds=3600,
                limit_value=Decimal(1),
            )
        )
        await session.commit()

        fake_upstream.set_json(
            {
                "id": "c1",
                "object": "chat.completion",
                "choices": [{"index": 0, "message": {"role": "assistant", "content": "hi"}}],
                "usage": {"prompt_tokens": 10, "completion_tokens": 5},
            }
        )
        chat = await client.post(
            "/v1/chat/completions",
            json={"model": seeded.model.name, "messages": [{"role": "user", "content": "hi"}]},
            headers=seeded.auth,
        )
        assert chat.status_code == 200

        fake_upstream.set_json(linkup_body())
        search = await client.post(
            "/v1/search", json={"model": model.name, "query": "q"}, headers=seeded.auth
        )
        assert search.status_code == 200


# --------------------------------------------------------------------------
# what actually goes out on the wire, per vendor
# --------------------------------------------------------------------------


class TestLinkupRequest:
    async def test_the_body_is_linkups_own_shape_with_a_bearer_token(
        self,
        client: httpx.AsyncClient,
        seeded: Seeded,
        session: AsyncSession,
        fake_upstream: FakeUpstream,
    ) -> None:
        model = await add_backend(session, seeded)
        fake_upstream.set_json(linkup_body())

        await client.post(
            "/v1/search",
            json={
                "model": model.name,
                "query": "european cloud",
                "max_results": 5,
                "include_domains": ["example.org"],
                "exclude_domains": ["spam.test"],
            },
            headers=seeded.auth,
        )

        sent = fake_upstream.last_body
        assert sent["q"] == "european cloud"
        assert sent["depth"] == "standard"
        # Always searchResults: the other two output types return a model's
        # prose through a route that meters searches and counts no tokens.
        assert sent["outputType"] == "searchResults"
        assert sent["maxResults"] == 5
        assert sent["includeDomains"] == ["example.org"]
        assert sent["excludeDomains"] == ["spam.test"]
        assert fake_upstream.headers[-1]["authorization"] == "Bearer search-key"

    async def test_results_are_normalised_and_images_are_kept_apart(
        self,
        client: httpx.AsyncClient,
        seeded: Seeded,
        session: AsyncSession,
        fake_upstream: FakeUpstream,
    ) -> None:
        """`name` becomes `title` and `content` becomes the snippet — not the
        content, because Linkup cannot return a page and a snippet in the field
        that means "the page" would be indistinguishable from one."""
        model = await add_backend(session, seeded)
        fake_upstream.set_json(linkup_body(count=1, images=1))

        body = (
            await client.post(
                "/v1/search", json={"model": model.name, "query": "q"}, headers=seeded.auth
            )
        ).json()

        text, image = body["results"]
        assert text["title"] == "Result 0"
        assert text["snippet"] == "An extract from result 0."
        assert text["content"] is None
        assert text["type"] == "text"
        assert image["type"] == "image"
        assert image["snippet"] is None

    async def test_asking_linkup_for_page_content_is_refused_not_faked(
        self,
        client: httpx.AsyncClient,
        seeded: Seeded,
        session: AsyncSession,
        fake_upstream: FakeUpstream,
    ) -> None:
        """Nothing left the deployment and nothing was counted: the refusal
        happens before the reservation."""
        model = await add_backend(session, seeded)

        response = await client.post(
            "/v1/search",
            json={"model": model.name, "query": "q", "include_content": True},
            headers=seeded.auth,
        )
        assert response.status_code == 400
        assert response.json()["error"]["code"] == "search_not_supported"
        assert fake_upstream.bodies == []
        assert (
            await session.execute(
                select(UsageRecord).where(UsageRecord.api_surface == ApiSurface.SEARCH)
            )
        ).scalars().all() == []

    async def test_a_tier_the_vendor_does_not_have_is_refused_rather_than_substituted(
        self,
        client: httpx.AsyncClient,
        seeded: Seeded,
        session: AsyncSession,
        fake_upstream: FakeUpstream,
    ) -> None:
        """`auto` silently falling back between engines is the one thing
        docs/web-search-plan.md says not to copy. A depth is the same: a
        cheaper search served under a dearer name."""
        model = await add_backend(session, seeded, tier="turbo", name="linkup-turbo")

        response = await client.post(
            "/v1/search", json={"model": model.name, "query": "q"}, headers=seeded.auth
        )
        assert response.status_code == 400
        assert "flash, fast, standard, deep" in response.json()["error"]["message"]
        assert fake_upstream.bodies == []


class TestExaRequest:
    async def test_the_body_is_exas_own_shape_with_an_api_key_header(
        self,
        client: httpx.AsyncClient,
        seeded: Seeded,
        session: AsyncSession,
        fake_upstream: FakeUpstream,
    ) -> None:
        model = await add_backend(session, seeded, plugin="exa", tier="deep-reasoning")
        fake_upstream.set_json(exa_body())

        await client.post(
            "/v1/search",
            json={"model": model.name, "query": "european cloud", "max_results": 3},
            headers=seeded.auth,
        )

        sent = fake_upstream.last_body
        assert sent["query"] == "european cloud"
        assert sent["type"] == "deep-reasoning"
        assert sent["numResults"] == 3
        # Exa's key goes in its own header, not as a bearer token.
        assert fake_upstream.headers[-1]["x-api-key"] == "search-key"
        assert "authorization" not in fake_upstream.headers[-1]

    async def test_page_text_is_asked_for_only_when_requested(
        self,
        client: httpx.AsyncClient,
        seeded: Seeded,
        session: AsyncSession,
        fake_upstream: FakeUpstream,
    ) -> None:
        """It is a separate charge on the same call, which is why it is opt-in
        — and why a default Exa search comes back with no page text at all."""
        model = await add_backend(session, seeded, plugin="exa", tier="auto")
        fake_upstream.set_json(exa_body(text=True))

        await client.post(
            "/v1/search", json={"model": model.name, "query": "q"}, headers=seeded.auth
        )
        assert "contents" not in fake_upstream.last_body

        body = (
            await client.post(
                "/v1/search",
                json={"model": model.name, "query": "q", "include_content": True},
                headers=seeded.auth,
            )
        ).json()
        assert fake_upstream.last_body["contents"] == {"text": True}
        first = body["results"][0]
        assert first["content"] == "The full text of result 0."
        # The summary is the snippet; the page text is not, or a 5,000-character
        # body would look like a one-line description.
        assert first["snippet"] == "A summary of result 0."
        assert first["published_at"] == "2026-01-01"
        assert first["author"] == "A Writer"


# --------------------------------------------------------------------------
# safety: what a caller can and cannot reach
# --------------------------------------------------------------------------


class TestSafety:
    async def test_backend_options_cannot_change_the_tier_or_the_output_shape(
        self,
        client: httpx.AsyncClient,
        seeded: Seeded,
        session: AsyncSession,
        fake_upstream: FakeUpstream,
    ) -> None:
        """The escape hatch is merged *under* the fields the plugin controls.

        Reaching `depth` would let a caller run — and be billed for — a tier
        their grant does not include while the ledger recorded the granted one;
        reaching `outputType` would return a model's prose through a route that
        counts no tokens.
        """
        model = await add_backend(session, seeded)
        fake_upstream.set_json(linkup_body())

        await client.post(
            "/v1/search",
            json={
                "model": model.name,
                "query": "q",
                "backend_options": {
                    "depth": "deep",
                    "outputType": "sourcedAnswer",
                    "fromDate": "2026-01-01",
                },
            },
            headers=seeded.auth,
        )

        sent = fake_upstream.last_body
        assert sent["depth"] == "standard"
        assert sent["outputType"] == "searchResults"
        # What the hatch is actually for still gets through.
        assert sent["fromDate"] == "2026-01-01"

    async def test_nothing_a_caller_sends_reaches_the_address(
        self,
        client: httpx.AsyncClient,
        seeded: Seeded,
        session: AsyncSession,
        fake_upstream: FakeUpstream,
    ) -> None:
        """The URL is the provider row plus the plugin's path, and that is all.

        Written down because the neighbouring bug is real: a client added
        beside an existing one that did not inherit its SSRF guard. Here there
        is nothing to inherit — a caller-supplied value has no route to the URL
        at all — and this test is what keeps that true.
        """
        model = await add_backend(session, seeded)
        seen: list[str] = []

        def record_url(request: httpx.Request) -> httpx.Response:
            seen.append(str(request.url))
            return httpx.Response(200, json=linkup_body())

        fake_upstream._responder = record_url  # the harness's own seam

        await client.post(
            "/v1/search",
            json={
                "model": model.name,
                "query": "http://169.254.169.254/latest/meta-data/",
                "include_domains": ["http://169.254.169.254"],
                "backend_options": {"url": "http://169.254.169.254", "base_url": "http://evil"},
            },
            headers=seeded.auth,
        )
        assert seen == [f"{UPSTREAM_BASE}/search"]

    async def test_a_provider_whose_plugin_cannot_search_is_refused_clearly(
        self,
        client: httpx.AsyncClient,
        seeded: Seeded,
        session: AsyncSession,
        fake_upstream: FakeUpstream,
    ) -> None:
        """A row labelled `search` behind the generic plugin is a configuration
        mistake, and it should read as one rather than as a 500."""
        model = ModelDef(
            name="not-really-a-search-backend",
            upstream_model="standard",
            provider_id=seeded.provider.id,
            kind=ModelKind.SEARCH,
        )
        session.add(model)
        await session.flush()
        session.add(GroupModelAccess(group_id=seeded.group.id, model_id=model.id))
        await session.commit()

        response = await client.post(
            "/v1/search", json={"model": model.name, "query": "q"}, headers=seeded.auth
        )
        assert response.status_code == 400
        assert response.json()["error"]["code"] == "not_a_search_backend"
        assert fake_upstream.bodies == []

    async def test_a_search_model_is_refused_on_the_chat_route_and_the_reverse(
        self,
        client: httpx.AsyncClient,
        seeded: Seeded,
        session: AsyncSession,
        fake_upstream: FakeUpstream,
    ) -> None:
        model = await add_backend(session, seeded)

        wrong_route = await client.post(
            "/v1/chat/completions",
            json={"model": model.name, "messages": [{"role": "user", "content": "hi"}]},
            headers=seeded.auth,
        )
        assert wrong_route.status_code == 400
        assert wrong_route.json()["error"]["code"] == "wrong_model_kind"
        assert "/v1/search" in wrong_route.json()["error"]["message"]

        wrong_kind = await client.post(
            "/v1/search", json={"model": seeded.model.name, "query": "q"}, headers=seeded.auth
        )
        assert wrong_kind.status_code == 400
        assert wrong_kind.json()["error"]["code"] == "wrong_model_kind"

    async def test_a_model_the_caller_cannot_reach_is_a_404(
        self,
        client: httpx.AsyncClient,
        seeded: Seeded,
        session: AsyncSession,
        session_factory: Any,
    ) -> None:
        """Access control is the ordinary one, which is the point of a search
        backend being a provider: no second grant mechanism to get wrong."""
        model = await add_backend(session, seeded)
        grant = (
            await session.execute(
                select(GroupModelAccess).where(GroupModelAccess.model_id == model.id)
            )
        ).scalar_one()
        await session.delete(grant)
        await session.commit()

        response = await client.post(
            "/v1/search", json={"model": model.name, "query": "q"}, headers=seeded.auth
        )
        assert response.status_code == 404

    async def test_an_unknown_field_is_refused_rather_than_dropped(
        self,
        client: httpx.AsyncClient,
        seeded: Seeded,
        session: AsyncSession,
    ) -> None:
        """The one `/v1` request model that forbids extras, because nothing
        here is forwarded: an unrecognised field would vanish, and a caller who
        asked for a date range and did not get one would trust the results."""
        model = await add_backend(session, seeded)
        response = await client.post(
            "/v1/search",
            json={"model": model.name, "query": "q", "fromDate": "2026-01-01"},
            headers=seeded.auth,
        )
        # 400 rather than FastAPI's 422: this app rewrites validation errors
        # into the OpenAI error shape, which every `/v1` client already parses.
        assert response.status_code == 400
        assert "fromDate" in response.json()["error"]["message"]

    async def test_a_query_of_whitespace_is_refused_here_not_by_the_vendor(
        self,
        client: httpx.AsyncClient,
        seeded: Seeded,
        session: AsyncSession,
        fake_upstream: FakeUpstream,
    ) -> None:
        model = await add_backend(session, seeded)
        response = await client.post(
            "/v1/search", json={"model": model.name, "query": "   "}, headers=seeded.auth
        )
        assert response.status_code == 400
        assert response.json()["error"]["code"] == "empty_query"
        assert fake_upstream.bodies == []


# --------------------------------------------------------------------------
# redaction: a search query is a second egress
# --------------------------------------------------------------------------


class TestRedaction:
    async def test_the_query_is_redacted_before_it_reaches_the_vendor(
        self,
        app: Any,
        client: httpx.AsyncClient,
        seeded: Seeded,
        session: AsyncSession,
        fake_upstream: FakeUpstream,
    ) -> None:
        """The query is prompt-derived text sent to a third party who logs it,
        so it goes through the same policy as everything else — and the row
        records that it did."""
        from test_ocr_surface import install_detector

        install_detector(app, [("PERSON", "Paolo Viviani")])
        model = await add_backend(session, seeded)
        fake_upstream.set_json(linkup_body())

        await client.post(
            "/v1/search",
            json={"model": model.name, "query": "what has Paolo Viviani published"},
            headers=seeded.auth,
        )

        sent_query = fake_upstream.last_body["q"]
        assert "Paolo Viviani" not in sent_query
        assert "<PERSON_" in sent_query
        record = await latest_record(session)
        assert record.redacted_entity_count == 1
        # Still one search: redaction changes what was searched for, never how
        # many searches were made.
        assert record.own_search_requests == 1


# --------------------------------------------------------------------------
# the plugins as objects, without a request around them
# --------------------------------------------------------------------------


class TestPlugins:
    @pytest.mark.parametrize("name", ["linkup", "exa"])
    def test_a_search_plugin_satisfies_the_protocol_and_others_do_not(
        self, name: str
    ) -> None:
        """The `isinstance` the route relies on. If `SearchPlugin` ever loses a
        member, this is what notices before a caller gets a 500."""
        assert isinstance(plugin_registry.resolve(name), SearchPlugin)
        assert not isinstance(plugin_registry.resolve("generic"), SearchPlugin)
        assert not isinstance(plugin_registry.resolve("cortecs"), SearchPlugin)

    @pytest.mark.parametrize("name", ["linkup", "exa"])
    def test_a_search_backend_declares_itself_a_search_kind_and_no_cost(
        self, name: str
    ) -> None:
        """Pass-through billing must stay unselectable: there is no price row
        for a search to pass through *to*."""
        plugin = plugin_registry.resolve(name)
        # Compared by value, not identity: `ProviderKind` is deliberately
        # duplicated between `models` and `plugins.base` so the model layer
        # does not import the plugin package, and the two enums are different
        # classes. Asserting `is` here passed nothing and failed confusingly.
        assert plugin.kind.value == ProviderKind.SEARCH.value
        assert plugin.reports_authoritative_cost is False
        assert plugin.read_reported_cost({"cost": 1234}) is None

    @pytest.mark.parametrize("name", ["linkup", "exa"])
    def test_the_catalogue_is_answered_from_the_plugin_and_is_unpriced(
        self, name: str
    ) -> None:
        """A search backend serves no `/models`, so Discover against one would
        fetch a URL the operator never typed — the failure the local
        extractor's built-in catalogue exists to prevent."""
        plugin = plugin_registry.resolve(name)
        catalogue = plugin.builtin_catalogue()
        assert catalogue is not None
        offered = {entry["id"] for entry in catalogue["data"]}
        assert offered == set(plugin.tiers)  # type: ignore[attr-defined]
        for entry in catalogue["data"]:
            assert entry["tags"] == ["Search"]
            # No rate anywhere. A plausible-looking one is the failure this
            # whole feature was shaped to avoid.
            assert "pricing" not in entry

    def test_the_catalogue_parser_reads_a_search_tier_as_a_search_model(self) -> None:
        """Discovery has to label these `search`, or an operator adding one by
        hand gets a chat model that `/v1/search` then refuses.

        They arrive through `kinds_by_id` rather than through the price rows:
        an entry with no rate is reported by `parse_catalogue` as a bare
        unpriced id, which is exactly what a search tier is and why Discover
        shows them with a "add the model by hand" reason instead of an import
        button.
        """
        from gateway.pricing import kinds_by_id, parse_catalogue

        catalogue = plugin_registry.resolve("exa").builtin_catalogue()
        prices, unpriced = parse_catalogue(catalogue)
        assert prices == []
        assert set(unpriced) == set(plugin_registry.resolve("exa").tiers)  # type: ignore[attr-defined]

        kinds = kinds_by_id(catalogue)
        assert kinds
        assert all(kind is ModelKind.SEARCH for kind in kinds.values())
