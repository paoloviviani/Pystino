"""``POST /v1/ocr``: one surface, metered by the page.

What is pinned here is the part that would otherwise be found on an invoice:
that the page count comes from the counterparty's own `usage_info` and not from
the length of the text it returned, that a page is charged as a unit, that an
OCR model is refused on the surfaces it does not belong to, and that the
document reaches the provider in the shape the caller sent it.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any

import httpx
from conftest import FakeUpstream, Seeded
from gateway.models import (
    ApiSurface,
    GroupModelAccess,
    ModelDef,
    ModelKind,
    ModelPrice,
    UsageRecord,
    UsageSource,
)
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

DOCUMENT = {"type": "document_url", "document_url": "https://example.org/invoice.pdf"}


async def add_ocr_model(
    session: AsyncSession,
    seeded: Seeded,
    *,
    name: str = "reader",
    per_page: str | None = "0.01",
    upstream: str = "mistral-ocr-4.1",
) -> ModelDef:
    model = ModelDef(
        name=name, upstream_model=upstream, provider_id=seeded.provider.id, kind=ModelKind.OCR
    )
    session.add(model)
    await session.flush()
    session.add(
        ModelPrice(
            model_id=model.id,
            # An OCR model that charges nothing per token is the normal case,
            # and zero rates here prove the page charge is not coming from them.
            input_per_mtok=Decimal(0),
            output_per_mtok=Decimal(0),
            per_page=Decimal(per_page) if per_page else None,
            currency="EUR",
        )
    )
    session.add(GroupModelAccess(group_id=seeded.group.id, model_id=model.id))
    await session.commit()
    return model


def ocr_response(pages: int = 3, **extra: Any) -> dict[str, Any]:
    """A response in the shape Cortecs and Mistral document."""
    body: dict[str, Any] = {
        "model": "mistral-ocr-4.1",
        "pages": [
            {"index": index, "markdown": f"page {index} text", "images": []}
            for index in range(pages)
        ],
        "usage_info": {"pages_processed": pages, "credits": 0.03},
    }
    body.update(extra)
    return body


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


class TestMetering:
    async def test_pages_are_read_from_usage_info_and_billed(
        self,
        client: httpx.AsyncClient,
        seeded: Seeded,
        session: AsyncSession,
        fake_upstream: FakeUpstream,
    ) -> None:
        """`usage_info.pages_processed`, not `usage`, and not the text length."""
        model = await add_ocr_model(session, seeded, per_page="0.01")
        fake_upstream.set_json(ocr_response(pages=7))

        response = await client.post(
            "/v1/ocr", json={"model": model.name, "document": DOCUMENT}, headers=seeded.auth
        )
        assert response.status_code == 200

        record = await latest_record(session)
        assert record.api_surface is ApiSurface.OCR
        assert record.cost == Decimal("0.07")
        # Exactly known, because the counterparty counted it.
        assert record.usage_source is UsageSource.UPSTREAM_EXACT
        # No tokens on this surface: reporting some would bill one request twice.
        assert record.total_tokens == 0

    async def test_an_unpriced_ocr_model_records_zero_rather_than_guessing(
        self,
        client: httpx.AsyncClient,
        seeded: Seeded,
        session: AsyncSession,
        fake_upstream: FakeUpstream,
    ) -> None:
        """The same rule as an unpriced chat model: no cost ceiling at all, and
        the console's unpriced count is what warns about it."""
        model = await add_ocr_model(session, seeded, name="unpriced", per_page=None)
        fake_upstream.set_json(ocr_response(pages=4))

        response = await client.post(
            "/v1/ocr", json={"model": model.name, "document": DOCUMENT}, headers=seeded.auth
        )
        assert response.status_code == 200
        record = await latest_record(session)
        assert record.cost == Decimal(0)

    async def test_the_document_reaches_the_provider_unchanged(
        self,
        client: httpx.AsyncClient,
        seeded: Seeded,
        session: AsyncSession,
        fake_upstream: FakeUpstream,
    ) -> None:
        """Options are forwarded rather than validated: the set is long,
        provider-specific and grows without us."""
        model = await add_ocr_model(session, seeded, name="passthrough")
        fake_upstream.set_json(ocr_response(pages=1))

        await client.post(
            "/v1/ocr",
            json={
                "model": model.name,
                "document": DOCUMENT,
                "table_format": "html",
                "eu_native": True,
                "confidence_scores_granularity": "page",
            },
            headers=seeded.auth,
        )
        sent = fake_upstream.last_body
        assert sent["document"] == DOCUMENT
        assert sent["table_format"] == "html"
        assert sent["eu_native"] is True
        # The upstream's own model id, not ours.
        assert sent["model"] == "mistral-ocr-4.1"

    async def test_our_model_name_is_echoed_not_the_upstreams(
        self,
        client: httpx.AsyncClient,
        seeded: Seeded,
        session: AsyncSession,
        fake_upstream: FakeUpstream,
    ) -> None:
        """Clients compare the echoed name with what they sent."""
        model = await add_ocr_model(session, seeded, name="ours")
        fake_upstream.set_json(ocr_response(pages=1))

        response = await client.post(
            "/v1/ocr", json={"model": model.name, "document": DOCUMENT}, headers=seeded.auth
        )
        assert response.json()["model"] == "ours"
        record = await latest_record(session)
        assert record.upstream_model == "mistral-ocr-4.1"

    async def test_the_pages_come_back_to_the_caller(
        self,
        client: httpx.AsyncClient,
        seeded: Seeded,
        session: AsyncSession,
        fake_upstream: FakeUpstream,
    ) -> None:
        model = await add_ocr_model(session, seeded, name="passthru2")
        fake_upstream.set_json(ocr_response(pages=2))

        body = (
            await client.post(
                "/v1/ocr", json={"model": model.name, "document": DOCUMENT}, headers=seeded.auth
            )
        ).json()
        assert [page["index"] for page in body["pages"]] == [0, 1]
        assert body["usage_info"]["pages_processed"] == 2


class TestRefusals:
    async def test_a_chat_model_is_refused_here(
        self, client: httpx.AsyncClient, seeded: Seeded
    ) -> None:
        """Sent to the right route rather than forwarded to fail upstream with a
        provider-specific error nobody can act on."""
        response = await client.post(
            "/v1/ocr", json={"model": seeded.model.name, "document": DOCUMENT}, headers=seeded.auth
        )
        assert response.status_code == 400
        assert "/v1/chat/completions" in response.text

    async def test_an_ocr_model_is_refused_on_the_chat_route(
        self,
        client: httpx.AsyncClient,
        seeded: Seeded,
        session: AsyncSession,
    ) -> None:
        model = await add_ocr_model(session, seeded, name="reader2")
        response = await client.post(
            "/v1/chat/completions",
            json={"model": model.name, "messages": [{"role": "user", "content": "hi"}]},
            headers=seeded.auth,
        )
        assert response.status_code == 400
        assert "/v1/ocr" in response.text

    async def test_a_document_with_no_location_is_refused(
        self, client: httpx.AsyncClient, seeded: Seeded, session: AsyncSession
    ) -> None:
        model = await add_ocr_model(session, seeded, name="reader3")
        response = await client.post(
            "/v1/ocr",
            json={"model": model.name, "document": {"type": "document_url"}},
            headers=seeded.auth,
        )
        assert response.status_code == 400

    async def test_a_model_nobody_granted_is_invisible(
        self,
        client: httpx.AsyncClient,
        seeded: Seeded,
        session: AsyncSession,
    ) -> None:
        """The allowlist holds on this surface too: absence of a grant means no
        access, and the answer must not distinguish "exists" from "yours"."""
        model = ModelDef(
            name="ungranted",
            upstream_model="mistral-ocr-4.1",
            provider_id=seeded.provider.id,
            kind=ModelKind.OCR,
        )
        session.add(model)
        await session.commit()

        response = await client.post(
            "/v1/ocr", json={"model": "ungranted", "document": DOCUMENT}, headers=seeded.auth
        )
        assert response.status_code == 404
