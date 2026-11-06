"""``POST /v1/rerank``: shape, metering, limits, redaction and discovery.

The two provider shapes are the ones measured live on 2026-11-06: Cortecs
(vLLM) takes string documents only and reports Chat Completions ``usage``;
Melious takes either, reports ``usage: null`` and keeps the count in ``meta``.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any

import httpx
import pytest
from conftest import FakeUpstream, Seeded
from gateway import rerank as shape
from gateway.errors import BadRequestError
from gateway.models import (
    ApiSurface,
    GroupModelAccess,
    ModelDef,
    ModelKind,
    ModelPrice,
    UsageRecord,
    UsageSource,
)
from gateway.pricing import kinds_by_id, parse_catalogue
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from test_embeddings import _ShoutyRedactor

DOCS = ["Paris is the capital of France.", "Berlin is in Germany.", "Bananas are yellow."]


async def add_rerank_model(
    session: AsyncSession, seeded: Seeded, *, name: str = "rank-1", input_per_mtok: str = "0.10"
) -> ModelDef:
    model = ModelDef(
        name=name,
        upstream_model="vendor/rank-1",
        provider_id=seeded.provider.id,
        kind=ModelKind.RERANK,
    )
    session.add(model)
    await session.flush()
    session.add(
        ModelPrice(
            model_id=model.id,
            input_per_mtok=Decimal(input_per_mtok),
            output_per_mtok=Decimal(0),
            currency="EUR",
        )
    )
    session.add(GroupModelAccess(group_id=seeded.group.id, model_id=model.id))
    await session.commit()
    return model


def cortecs_reply(prompt_tokens: int = 50) -> dict[str, Any]:
    """vLLM's reply: documents always echoed, Chat Completions usage."""
    return {
        "id": "rerank-1",
        "results": [
            {"index": 1, "relevance_score": 0.0004, "document": {"text": "Berlin is in Germany."}},
            {"index": 0, "relevance_score": 1.0, "document": {"text": "Paris is the capital."}},
        ],
        "usage": {
            "prompt_tokens": prompt_tokens,
            "completion_tokens": 0,
            "total_tokens": prompt_tokens,
        },
    }


def melious_reply(tokens: int = 251) -> dict[str, Any]:
    return {
        "id": "score-1",
        "results": [
            {"index": 0, "relevance_score": 0.96, "document": None},
            {"index": 2, "relevance_score": 0.00005, "document": None},
        ],
        "meta": {"billed_units": {"total_tokens": tokens}, "tokens": {"input_tokens": tokens}},
        "usage": None,
    }


async def post(client: httpx.AsyncClient, seeded: Seeded, **body: Any) -> httpx.Response:
    payload = {"model": "rank-1", "query": "capital of France?", "documents": DOCS, **body}
    return await client.post("/v1/rerank", json=payload, headers=seeded.auth)


async def ledger(session: AsyncSession) -> UsageRecord:
    return (
        (await session.execute(select(UsageRecord).where(UsageRecord.model_name == "rank-1")))
        .scalars()
        .one()
    )


class TestShape:
    async def test_results_are_ordered_and_carry_no_documents_by_default(
        self,
        client: httpx.AsyncClient,
        seeded: Seeded,
        session: AsyncSession,
        fake_upstream: FakeUpstream,
    ) -> None:
        await add_rerank_model(session, seeded)
        fake_upstream.set_json(cortecs_reply())

        response = await post(client, seeded)

        assert response.status_code == 200, response.text
        body = response.json()
        assert body["model"] == "rank-1"
        assert body["results"] == [
            {"index": 0, "relevance_score": 1.0},
            {"index": 1, "relevance_score": 0.0004},
        ]
        assert body["usage"] == {"prompt_tokens": 50, "total_tokens": 50}

    async def test_return_documents_echoes_the_callers_text_not_the_providers(
        self,
        client: httpx.AsyncClient,
        seeded: Seeded,
        session: AsyncSession,
        fake_upstream: FakeUpstream,
    ) -> None:
        await add_rerank_model(session, seeded)
        fake_upstream.set_json(cortecs_reply())

        body = (await post(client, seeded, return_documents=True)).json()

        assert body["results"][0]["document"] == {"text": DOCS[0]}
        assert body["results"][1]["document"] == {"text": DOCS[1]}

    async def test_top_n_cuts_the_results_and_is_clamped_upstream(
        self,
        client: httpx.AsyncClient,
        seeded: Seeded,
        session: AsyncSession,
        fake_upstream: FakeUpstream,
    ) -> None:
        await add_rerank_model(session, seeded)
        fake_upstream.set_json(cortecs_reply())

        body = (await post(client, seeded, top_n=1)).json()
        assert [r["index"] for r in body["results"]] == [0]
        await post(client, seeded, top_n=99)
        assert fake_upstream.last_body["top_n"] == 3

    async def test_documents_go_upstream_as_plain_strings(
        self,
        client: httpx.AsyncClient,
        seeded: Seeded,
        session: AsyncSession,
        fake_upstream: FakeUpstream,
    ) -> None:
        """Cortecs answers 400 to {"text": ...}; the caller may send either."""
        await add_rerank_model(session, seeded)
        fake_upstream.set_json(cortecs_reply())

        await post(
            client, seeded, documents=["a", {"text": "b"}], return_documents=True, rank_fields=["x"]
        )

        sent = fake_upstream.last_body
        assert sent["documents"] == ["a", "b"]
        assert sent["model"] == "vendor/rank-1"
        assert sent["rank_fields"] == ["x"]  # unknown fields pass through
        assert "return_documents" not in sent
        assert fake_upstream.urls[-1].endswith("/rerank")

    async def test_melious_shape_with_null_usage_and_documents(
        self,
        client: httpx.AsyncClient,
        seeded: Seeded,
        session: AsyncSession,
        fake_upstream: FakeUpstream,
    ) -> None:
        await add_rerank_model(session, seeded)
        fake_upstream.set_json(melious_reply())

        body = (await post(client, seeded)).json()

        assert body["results"] == [
            {"index": 0, "relevance_score": 0.96},
            {"index": 2, "relevance_score": 0.00005},
        ]
        assert body["usage"] == {"prompt_tokens": 251, "total_tokens": 251}

    async def test_an_unusable_reply_is_a_502_not_a_crash(
        self,
        client: httpx.AsyncClient,
        seeded: Seeded,
        session: AsyncSession,
        fake_upstream: FakeUpstream,
    ) -> None:
        await add_rerank_model(session, seeded)
        fake_upstream.set_json({"results": [{"index": 99, "relevance_score": 1}]})

        response = await post(client, seeded)

        assert response.status_code == 502
        assert (await ledger(session)).cost == Decimal(0)


class TestKindRouting:
    async def test_a_chat_model_is_refused_and_names_the_route(
        self, client: httpx.AsyncClient, seeded: Seeded
    ) -> None:
        response = await post(client, seeded, model="test-model")
        assert response.status_code == 400
        assert "/v1/chat/completions" in response.json()["error"]["message"]

    async def test_a_reranker_is_refused_by_other_routes(
        self, client: httpx.AsyncClient, seeded: Seeded, session: AsyncSession
    ) -> None:
        await add_rerank_model(session, seeded)
        for path, body in (
            ("/v1/chat/completions", {"messages": [{"role": "user", "content": "hi"}]}),
            ("/v1/embeddings", {"input": "x"}),
        ):
            response = await client.post(
                path, json={"model": "rank-1", **body}, headers=seeded.auth
            )
            assert response.status_code == 400
            assert "/v1/rerank" in response.json()["error"]["message"]

    async def test_the_route_needs_an_api_key(self, client: httpx.AsyncClient) -> None:
        response = await client.post(
            "/v1/rerank", json={"model": "m", "query": "q", "documents": ["d"]}
        )
        assert response.status_code == 401

    async def test_models_lists_the_reranker_with_its_kind(
        self, client: httpx.AsyncClient, seeded: Seeded, session: AsyncSession
    ) -> None:
        await add_rerank_model(session, seeded)
        cards = (await client.get("/v1/models", headers=seeded.auth)).json()["data"]
        assert {c["id"]: c["kind"] for c in cards}["rank-1"] == "rerank"
        only = (await client.get("/v1/models?include=rerank", headers=seeded.auth)).json()["data"]
        assert [c["id"] for c in only] == ["rank-1"]


class TestMetering:
    async def test_the_providers_usage_is_charged_per_input_token(
        self,
        client: httpx.AsyncClient,
        seeded: Seeded,
        session: AsyncSession,
        fake_upstream: FakeUpstream,
    ) -> None:
        await add_rerank_model(session, seeded, input_per_mtok="0.10")
        fake_upstream.set_json(cortecs_reply(prompt_tokens=1_000_000))

        await post(client, seeded)

        record = await ledger(session)
        assert record.api_surface is ApiSurface.RERANK
        assert record.cost == Decimal("0.10")
        assert record.completion_tokens == 0
        assert record.usage_source is UsageSource.UPSTREAM_EXACT

    async def test_melious_meta_tokens_are_what_is_charged(
        self,
        client: httpx.AsyncClient,
        seeded: Seeded,
        session: AsyncSession,
        fake_upstream: FakeUpstream,
    ) -> None:
        await add_rerank_model(session, seeded, input_per_mtok="0.15")
        fake_upstream.set_json(melious_reply(tokens=1_000_000))

        await post(client, seeded)

        record = await ledger(session)
        assert record.prompt_tokens == 1_000_000
        assert record.cost == Decimal("0.15")

    async def test_with_no_reported_usage_the_estimate_stands(
        self,
        client: httpx.AsyncClient,
        seeded: Seeded,
        session: AsyncSession,
        fake_upstream: FakeUpstream,
    ) -> None:
        await add_rerank_model(session, seeded)
        fake_upstream.set_json({"results": [{"index": 0, "relevance_score": 0.5}]})

        response = await post(client, seeded)

        record = await ledger(session)
        assert record.usage_source is not UsageSource.UPSTREAM_EXACT
        assert record.prompt_tokens > 0
        assert response.json()["usage"]["prompt_tokens"] == record.prompt_tokens

    async def test_an_upstream_error_is_passed_through_and_not_billed(
        self,
        client: httpx.AsyncClient,
        seeded: Seeded,
        session: AsyncSession,
        fake_upstream: FakeUpstream,
    ) -> None:
        await add_rerank_model(session, seeded)
        fake_upstream.set_json({"error": {"message": "nope"}}, status=400)

        response = await post(client, seeded)

        assert response.status_code == 400
        assert (await ledger(session)).cost == Decimal(0)


class TestValidation:
    @pytest.mark.parametrize(
        ("body", "fragment"),
        [
            ({"query": "  "}, "'query'"),
            ({"documents": []}, "'documents'"),
            ({"documents": ["ok", ""]}, "documents[1]"),
            ({"documents": ["ok", {"nope": 1}]}, "documents[1]"),
            ({"top_n": 0}, "'top_n'"),
        ],
    )
    async def test_bad_requests_are_400_with_a_message(
        self,
        client: httpx.AsyncClient,
        seeded: Seeded,
        session: AsyncSession,
        fake_upstream: FakeUpstream,
        body: dict[str, Any],
        fragment: str,
    ) -> None:
        await add_rerank_model(session, seeded)
        response = await post(client, seeded, **body)
        assert response.status_code == 400
        assert fragment in response.json()["error"]["message"]
        assert fake_upstream.bodies == []

    async def test_a_missing_field_is_a_validation_error(
        self, client: httpx.AsyncClient, seeded: Seeded
    ) -> None:
        response = await client.post(
            "/v1/rerank", json={"model": "rank-1", "query": "q"}, headers=seeded.auth
        )
        assert response.status_code in (400, 422)

    async def test_the_document_and_size_limits_are_settings(
        self,
        app: Any,
        client: httpx.AsyncClient,
        seeded: Seeded,
        session: AsyncSession,
    ) -> None:
        await add_rerank_model(session, seeded)
        settings = app.state.settings
        settings.rerank.max_documents = 2
        response = await post(client, seeded)
        assert response.status_code == 400
        assert "at most 2" in response.json()["error"]["message"]

        settings.rerank.max_documents = 10
        settings.rerank.max_total_bytes = 20
        response = await post(client, seeded)
        assert response.status_code == 400
        assert "bytes" in response.json()["error"]["message"]

    def test_defaults_are_sensible(self) -> None:
        with pytest.raises(BadRequestError):
            shape.validate(
                query="q",
                documents=["d"] * 1001,
                top_n=None,
                max_documents=1000,
                max_total_bytes=10**9,
            )
        assert shape.validate(
            query="q",
            documents=["d", {"text": "e"}],
            top_n=None,
            max_documents=1000,
            max_total_bytes=10**9,
        ) == ["d", "e"]


class TestRedaction:
    async def test_the_query_and_every_document_are_redacted_before_they_leave(
        self,
        app: Any,
        client: httpx.AsyncClient,
        seeded: Seeded,
        session: AsyncSession,
        fake_upstream: FakeUpstream,
    ) -> None:
        await add_rerank_model(session, seeded)
        fake_upstream.set_json(cortecs_reply())
        app.state.redactor = _ShoutyRedactor()

        response = await post(
            client,
            seeded,
            query="where does Mario Rossi live?",
            documents=["Mario Rossi lives in Rome.", {"text": "Mario Rossi works here."}],
            return_documents=True,
        )

        sent = fake_upstream.last_body
        assert "Mario Rossi" not in str(sent)
        assert sent["query"] == "where does <PERSON_X> live?"
        assert all("<PERSON_X>" in text for text in sent["documents"])
        # The caller gets their own text back, not the placeholder.
        assert response.json()["results"][0]["document"]["text"] == "Mario Rossi lives in Rome."


class TestDiscovery:
    def test_cortecs_reranker_entries_are_a_rerank_kind(self) -> None:
        payload = {
            "data": [
                {
                    "id": "bge-reranker-v2-m3",
                    "pricing": {"currency": "EUR", "input_token": 0.1, "output_token": 0.0},
                    "tags": ["Rerank"],
                    "input_modalities": ["text"],
                    "output_modalities": ["rerank"],
                    "context_size": 8192,
                },
                {
                    "id": "no-tag-reranker",
                    "pricing": {"currency": "EUR", "input_token": 0.1, "output_token": 0.0},
                    "output_modalities": ["rerank"],
                },
            ]
        }
        assert kinds_by_id(payload) == {
            "bge-reranker-v2-m3": ModelKind.RERANK,
            "no-tag-reranker": ModelKind.RERANK,
        }
        prices, unpriced = parse_catalogue(payload)
        assert unpriced == [] and {p.model_id for p in prices} == set(kinds_by_id(payload))
