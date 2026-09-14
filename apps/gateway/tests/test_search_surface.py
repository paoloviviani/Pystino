"""``POST /v1/search/{backend}``: an authenticated, metering passthrough.

The gateway does not translate. The caller speaks the vendor's own API; the
gateway resolves the backend, checks the grant, counts the request, attaches
the backend's own credential, and forwards the body verbatim — returning the
vendor's answer verbatim (ADR 0071). What the tests hold, therefore, is that
the passthrough is honest in every direction: nothing added to the body,
nothing removed from the answer, the count taken before the call and kept on
failure, and the grants bounding everything.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any

import httpx
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
    name: str | None = None,
    key: str = "search-key",
    granted: bool = True,
) -> ModelDef:
    """A search backend: the provider, its credential, and its grant anchor.

    The anchor is the one model row a backend owns — named after it — built
    the way the admin API builds it, so the decrypt-and-send path and the
    access grant are both exercised.
    """
    box = SecretBox(["test-encryption-key-not-for-production"])
    provider = Provider(
        name=name or plugin,
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

    anchor = ModelDef(
        name=name or plugin,
        upstream_model="search",
        provider_id=provider.id,
        kind=ModelKind.SEARCH,
    )
    session.add(anchor)
    await session.flush()
    if granted:
        session.add(GroupModelAccess(group_id=seeded.group.id, model_id=anchor.id))
    await session.commit()
    return anchor


def linkup_request(query: str = "european cloud providers", **extra: Any) -> dict[str, Any]:
    """Linkup's own request shape, as its OpenAPI document defines it."""
    body: dict[str, Any] = {
        "q": query,
        "depth": "standard",
        "outputType": "searchResults",
    }
    body.update(extra)
    return body


def linkup_body(count: int = 2) -> dict[str, Any]:
    """Linkup's `searchResults` shape, returned verbatim by the passthrough.

    Note `name` rather than `title` — the caller reads the vendor's shape now,
    which is exactly why the gateway must not reshape it.
    """
    return {
        "results": [
            {
                "type": "text",
                "name": f"Result {index}",
                "url": f"https://example.org/{index}",
                "content": f"An extract from result {index}.",
                "favicon": "https://example.org/favicon.ico",
            }
            for index in range(count)
        ]
    }


def exa_request(query: str = "anything") -> dict[str, Any]:
    return {"query": query, "type": "auto"}


def exa_body(count: int = 2) -> dict[str, Any]:
    return {
        "requestId": "req-exa-1",
        "results": [
            {"title": f"Result {index}", "url": f"https://example.org/{index}"}
            for index in range(count)
        ],
        "costDollars": {"total": 0.007, "search": {"neural": 0.007}},
    }


def jina_body(count: int = 2) -> dict[str, Any]:
    """Jina's search envelope, as its API guide documents it.

    Note ``data`` rather than ``results``, and ``description`` rather than a
    snippet field — the entry the unified reader turns into title/URL/snippet.
    """
    return {
        "code": 200,
        "status": 20000,
        "data": [
            {
                "title": f"Result {index}",
                "description": f"An extract from result {index}.",
                "url": f"https://example.org/{index}",
                "content": "",
                "usage": {"tokens": 10475},
            }
            for index in range(count)
        ],
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
# the passthrough itself
# --------------------------------------------------------------------------


class TestPassthrough:
    async def test_the_body_reaches_the_vendor_untouched(
        self,
        client: httpx.AsyncClient,
        seeded: Seeded,
        session: AsyncSession,
        fake_upstream: FakeUpstream,
    ) -> None:
        """Every field the caller sent arrives; nothing is added or renamed."""
        await add_backend(session, seeded)
        body = linkup_request("european cloud providers", maxResults=7)

        fake_upstream.set_json(linkup_body())
        response = await client.post(
            "/v1/search/linkup", json=body, headers=seeded.auth
        )
        assert response.status_code == 200, response.text
        assert fake_upstream.last_body == body

    async def test_the_answer_is_the_vendors_own(
        self,
        client: httpx.AsyncClient,
        seeded: Seeded,
        session: AsyncSession,
        fake_upstream: FakeUpstream,
    ) -> None:
        """Not normalised: Linkup's `name`, verbatim, is what comes back."""
        await add_backend(session, seeded)
        vendor_answer = linkup_body(count=3)
        fake_upstream.set_json(vendor_answer)

        response = await client.post(
            "/v1/search/linkup", json=linkup_request(), headers=seeded.auth
        )
        assert response.status_code == 200, response.text
        assert response.json() == vendor_answer

    async def test_the_credential_rides_the_vendors_scheme(
        self,
        client: httpx.AsyncClient,
        seeded: Seeded,
        session: AsyncSession,
        fake_upstream: FakeUpstream,
    ) -> None:
        """Linkup takes a bearer; the stored key goes out as one."""
        await add_backend(session, seeded, key="vendor-key-1")
        await client.post("/v1/search/linkup", json=linkup_request(), headers=seeded.auth)
        authorization = fake_upstream.headers[-1].get("authorization")
        assert authorization == "Bearer vendor-key-1"

    async def test_exas_credential_rides_its_own_header(
        self,
        client: httpx.AsyncClient,
        seeded: Seeded,
        session: AsyncSession,
        fake_upstream: FakeUpstream,
    ) -> None:
        await add_backend(session, seeded, plugin="exa", key="vendor-key-2")
        fake_upstream.set_json(exa_body())
        await client.post(
            "/v1/search/exa", json=exa_request("capital of france"), headers=seeded.auth
        )
        assert fake_upstream.last_body == exa_request("capital of france")
        assert fake_upstream.headers[-1].get("x-api-key") == "vendor-key-2"

    async def test_jina_passes_through_without_the_unified_content_header(
        self,
        client: httpx.AsyncClient,
        seeded: Seeded,
        session: AsyncSession,
        fake_upstream: FakeUpstream,
    ) -> None:
        """Jina's ``X-Respond-With: no-content`` belongs to the unified route
        only. The passthrough forwards the caller's body verbatim and returns
        the vendor's answer verbatim — narrowing it here would silently
        reshape what the caller asked for."""
        await add_backend(session, seeded, plugin="jina", key="vendor-key-3")
        fake_upstream.set_json(jina_body())
        body = {"q": "capital of france", "num": 5}

        response = await client.post("/v1/search/jina", json=body, headers=seeded.auth)
        assert response.status_code == 200, response.text
        assert fake_upstream.last_body == body
        assert response.json() == jina_body()
        assert fake_upstream.headers[-1].get("authorization") == "Bearer vendor-key-3"
        assert "x-respond-with" not in fake_upstream.headers[-1]


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
        await add_backend(session, seeded)
        fake_upstream.set_json(linkup_body())

        response = await client.post(
            "/v1/search/linkup", json=linkup_request(), headers=seeded.auth
        )
        assert response.status_code == 200

        record = await latest_record(session)
        assert record.api_surface is ApiSurface.SEARCH
        assert record.status is UsageStatus.COMPLETED
        assert record.own_search_requests == 1
        assert record.own_search_backend == "linkup"
        # A passthrough never reads the body, so no tier is known to it.
        assert record.own_search_tier is None
        # Never priced. Not "priced at zero because the model has no price row"
        # — there is no rate anywhere for this to multiply.
        assert record.cost == Decimal(0)
        assert record.total_tokens == 0

    async def test_two_searches_are_two_rows_and_two_counts(
        self,
        client: httpx.AsyncClient,
        seeded: Seeded,
        session: AsyncSession,
        fake_upstream: FakeUpstream,
    ) -> None:
        """The count is per request, not per result: five results is one search."""
        await add_backend(session, seeded)
        fake_upstream.set_json(linkup_body(count=5))

        for _ in range(2):
            assert (
                await client.post(
                    "/v1/search/linkup", json=linkup_request(), headers=seeded.auth
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


class TestFailuresStillCount:
    async def test_a_refused_search_keeps_its_usage_row_and_its_count(
        self,
        client: httpx.AsyncClient,
        seeded: Seeded,
        session: AsyncSession,
        fake_upstream: FakeUpstream,
    ) -> None:
        await add_backend(session, seeded)
        fake_upstream.set_json({"error": {"message": "insufficient credits"}}, status=402)

        response = await client.post(
            "/v1/search/linkup", json=linkup_request(), headers=seeded.auth
        )
        assert response.status_code == 402
        # The vendor's own error, verbatim — the caller that assembled the
        # body is the one who can act on it.
        assert response.json() == {"error": {"message": "insufficient credits"}}

        record = await latest_record(session)
        assert record.own_search_requests == 1
        assert record.status is not UsageStatus.COMPLETED

    async def test_an_unreachable_backend_keeps_its_usage_row_and_its_count(
        self,
        client: httpx.AsyncClient,
        seeded: Seeded,
        session: AsyncSession,
        fake_upstream: FakeUpstream,
    ) -> None:
        # A backend pointed at a port nothing listens on: the transport
        # failure the upstream client turns into UpstreamError.
        async with session.begin():
            provider = Provider(
                name="dead",
                base_url="http://127.0.0.1:9/v1",
                api_key_encrypted=SecretBox(
                    ["test-encryption-key-not-for-production"]
                ).encrypt("k"),
                plugin="linkup",
                kind=ProviderKind.SEARCH,
            )
            session.add(provider)
            await session.flush()
            anchor = ModelDef(
                name="dead",
                upstream_model="search",
                provider_id=provider.id,
                kind=ModelKind.SEARCH,
            )
            session.add(anchor)
            await session.flush()
            session.add(GroupModelAccess(group_id=seeded.group.id, model_id=anchor.id))

        response = await client.post(
            "/v1/search/dead", json=linkup_request(), headers=seeded.auth
        )
        assert response.status_code == 502
        record = await latest_record(session)
        assert record.own_search_requests == 1


class TestQuota:
    async def test_a_search_rule_refuses_the_request_before_the_backend_is_called(
        self,
        client: httpx.AsyncClient,
        seeded: Seeded,
        session: AsyncSession,
        fake_upstream: FakeUpstream,
    ) -> None:
        await add_backend(session, seeded)
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
            await client.post("/v1/search/linkup", json=linkup_request(), headers=seeded.auth)
        ).status_code == 200
        # The second search is refused before the upstream is called, so the
        # fake upstream's call count never moves.
        calls_before = len(fake_upstream.bodies)
        response = await client.post(
            "/v1/search/linkup", json=linkup_request(), headers=seeded.auth
        )
        assert response.status_code == 429
        assert len(fake_upstream.bodies) == calls_before


# --------------------------------------------------------------------------
# the gate
# --------------------------------------------------------------------------


class TestAccess:
    async def test_an_ungranted_backend_is_a_404(
        self,
        client: httpx.AsyncClient,
        seeded: Seeded,
        session: AsyncSession,
    ) -> None:
        """Which backends exist is not the caller's business."""
        await add_backend(session, seeded, granted=False)
        response = await client.post(
            "/v1/search/linkup", json=linkup_request(), headers=seeded.auth
        )
        assert response.status_code == 404

    async def test_an_unknown_backend_is_a_404(
        self, client: httpx.AsyncClient, seeded: Seeded
    ) -> None:
        response = await client.post(
            "/v1/search/no-such-backend", json=linkup_request(), headers=seeded.auth
        )
        assert response.status_code == 404

    async def test_a_model_provider_cannot_be_searched(
        self,
        client: httpx.AsyncClient,
        seeded: Seeded,
    ) -> None:
        """The path names a backend; an inference provider's name resolving to
        a non-search plugin is a configuration mistake, and says so."""
        response = await client.post(
            f"/v1/search/{seeded.model.name}", json=linkup_request(), headers=seeded.auth
        )
        assert response.status_code in (400, 404)

    async def test_an_empty_body_is_refused_here(
        self, client: httpx.AsyncClient, seeded: Seeded, session: AsyncSession
    ) -> None:
        """Nothing to forward is nothing to count, and the vendor would read
        the absence as its own 400."""
        await add_backend(session, seeded)
        response = await client.post(
            "/v1/search/linkup", headers=seeded.auth, content=b""
        )
        assert response.status_code == 400
