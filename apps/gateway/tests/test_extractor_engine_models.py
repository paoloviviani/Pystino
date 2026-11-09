"""The built-in extractor's engines are OCR models, offered when the extractor runs them.

`markitdown` is the extractor's default engine (no engine header, as it always was),
`markitdown-ocr` and `docling` send `X-Extractor-Engine`. The extractor reports what it
can run in `/healthz`; a model whose engine it does not report is left out of the model
listing and refused on a call, never read by another engine under its name.
"""

from __future__ import annotations

from typing import Any

import httpx
import pytest
from conftest import Seeded
from gateway import extraction
from sqlalchemy.ext.asyncio import AsyncSession
from test_ocr_surface import INLINE_DOCX, FakeExtractor, add_local_model


async def add_engine_model(session: AsyncSession, seeded: Seeded, name: str) -> None:
    model = await add_local_model(session, seeded, name=name)
    model.upstream_model = name
    await session.commit()


async def read(client: httpx.AsyncClient, seeded: Seeded, model: str) -> httpx.Response:
    return await client.post(
        "/v1/ocr",
        json={"model": model, "document": {"type": "document_url", "document_url": INLINE_DOCX}},
        headers=seeded.auth,
    )


class TestEngineOfModel:
    @pytest.mark.parametrize(
        ("name", "engine"),
        [("markitdown", None), ("markitdown-ocr", "ocr"), ("docling", "docling"), ("mine", None)],
    )
    def test_each_model_means_one_engine(self, name: str, engine: str | None) -> None:
        assert extraction.engine_of_model(name) == engine

    def test_availability_asks_the_engines_the_extractor_reports(self) -> None:
        both = frozenset({"markitdown", "ocr"})
        assert extraction.model_availability("markitdown", None) is True
        assert extraction.model_availability("markitdown-ocr", both) is True
        assert extraction.model_availability("docling", both) is False
        # Cannot be asked: unknown, so listings leave the non-default models out.
        assert extraction.model_availability("docling", None) is None


class TestReadingWithAnEngineModel:
    async def test_each_model_sends_its_engine(
        self, app: Any, client: httpx.AsyncClient, seeded: Seeded, session: AsyncSession
    ) -> None:
        extractor = FakeExtractor()
        app.state.control_http = extractor.client()
        await add_local_model(session, seeded, name="markitdown")
        await add_engine_model(session, seeded, "markitdown-ocr")
        await add_engine_model(session, seeded, "docling")

        for name in ("markitdown", "markitdown-ocr", "docling"):
            assert (await read(client, seeded, name)).status_code == 200
        assert [h.get("x-extractor-engine") for h in extractor.headers] == [None, "ocr", "docling"]

    async def test_a_model_the_extractor_cannot_run_is_refused_not_read_by_another_engine(
        self, app: Any, client: httpx.AsyncClient, seeded: Seeded, session: AsyncSession
    ) -> None:
        extractor = FakeExtractor(engines=("markitdown",))
        app.state.control_http = extractor.client()
        await add_engine_model(session, seeded, "docling")
        response = await read(client, seeded, "docling")
        assert response.status_code == 400, response.text
        assert response.json()["error"]["code"] == "extractor_engine_unavailable"
        assert extractor.bodies == [], "nothing was read"

    async def test_an_extractor_that_cannot_be_asked_does_not_block_a_call(
        self, app: Any, client: httpx.AsyncClient, seeded: Seeded, session: AsyncSession
    ) -> None:
        """The call itself then fails the way an unreachable extractor always does."""

        def down(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("no route", request=request)

        app.state.control_http = httpx.AsyncClient(transport=httpx.MockTransport(down))
        await add_engine_model(session, seeded, "docling")
        response = await read(client, seeded, "docling")
        assert response.status_code == 502, response.text


class TestTheModelListing:
    async def names(self, client: httpx.AsyncClient, seeded: Seeded) -> list[str]:
        response = await client.get("/v1/models?include=ocr", headers=seeded.auth)
        assert response.status_code == 200, response.text
        return sorted(entry["id"] for entry in response.json()["data"])

    async def test_only_the_engines_the_extractor_reports_are_listed(
        self, app: Any, client: httpx.AsyncClient, seeded: Seeded, session: AsyncSession
    ) -> None:
        app.state.control_http = FakeExtractor(engines=("markitdown", "ocr")).client()
        await add_local_model(session, seeded, name="markitdown")
        await add_engine_model(session, seeded, "markitdown-ocr")
        await add_engine_model(session, seeded, "docling")
        assert await self.names(client, seeded) == ["markitdown", "markitdown-ocr"]

    async def test_all_of_them_with_the_docling_image(
        self, app: Any, client: httpx.AsyncClient, seeded: Seeded, session: AsyncSession
    ) -> None:
        app.state.control_http = FakeExtractor().client()
        await add_local_model(session, seeded, name="markitdown")
        await add_engine_model(session, seeded, "markitdown-ocr")
        await add_engine_model(session, seeded, "docling")
        assert await self.names(client, seeded) == ["docling", "markitdown", "markitdown-ocr"]

    async def test_an_extractor_that_predates_the_field_offers_only_markitdown(
        self, app: Any, client: httpx.AsyncClient, seeded: Seeded, session: AsyncSession
    ) -> None:
        def old(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={"status": "ok"})

        app.state.control_http = httpx.AsyncClient(transport=httpx.MockTransport(old))
        await add_local_model(session, seeded, name="markitdown")
        await add_engine_model(session, seeded, "docling")
        assert await self.names(client, seeded) == ["markitdown"]
