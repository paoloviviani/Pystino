"""The embeddings route, and recording what actually served a request.

Both come from reading the Cortecs API documentation (ADR 0028). What is tested
here is the part a provider cannot be trusted to make obvious: that an embedding
costs the input price and nothing else, that a substituted model is recorded and
disclosed rather than silently mispriced, and that text sent to be embedded is
redacted like any other text.
"""

from __future__ import annotations

from decimal import Decimal

import httpx
from conftest import FakeUpstream, Seeded
from gateway.models import (
    GroupModelAccess,
    ModelDef,
    ModelKind,
    ModelPrice,
    UsageRecord,
    UsageSource,
)
from gateway.redaction import RedactionOutcome
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession


async def add_embedding_model(
    session: AsyncSession,
    seeded: Seeded,
    *,
    name: str = "embed-1",
    input_per_mtok: str = "0.10",
) -> ModelDef:
    model = ModelDef(
        name=name,
        upstream_model="vendor/embed-1",
        provider_id=seeded.provider.id,
        kind=ModelKind.EMBEDDING,
    )
    session.add(model)
    await session.flush()
    session.add(
        ModelPrice(
            model_id=model.id,
            input_per_mtok=Decimal(input_per_mtok),
            # An embedding model with an output price is not an error; it is
            # simply never charged, because completion_tokens is always 0.
            output_per_mtok=Decimal("5"),
            currency="EUR",
        )
    )
    session.add(GroupModelAccess(group_id=seeded.group.id, model_id=model.id))
    await session.commit()
    return model


def embedding_body(*, prompt_tokens: int = 100, model: str = "vendor/embed-1") -> dict:
    return {
        "id": "emb-1",
        "object": "list",
        "created": 1_700_000_000,
        "provider": "acme-cloud",
        "model": model,
        "data": [{"index": 0, "object": "embedding", "embedding": [0.1, 0.2, 0.3]}],
        # Cortecs documents completion_tokens as always 0 for embeddings.
        "usage": {
            "prompt_tokens": prompt_tokens,
            "completion_tokens": 0,
            "total_tokens": prompt_tokens,
        },
    }


class TestEmbeddings:
    async def test_a_batch_is_embedded(
        self,
        client: httpx.AsyncClient,
        seeded: Seeded,
        session: AsyncSession,
        fake_upstream: FakeUpstream,
    ) -> None:
        await add_embedding_model(session, seeded)
        fake_upstream.set_json(embedding_body())

        response = await client.post(
            "/v1/embeddings",
            json={"model": "embed-1", "input": ["one", "two"]},
            headers=seeded.auth,
        )
        assert response.status_code == 200, response.text
        assert len(response.json()["data"]) == 1

    async def test_the_upstream_model_name_is_sent_and_ours_is_returned(
        self,
        client: httpx.AsyncClient,
        seeded: Seeded,
        session: AsyncSession,
        fake_upstream: FakeUpstream,
    ) -> None:
        """Same contract as the chat route: clients compare the echoed name."""
        await add_embedding_model(session, seeded)
        fake_upstream.set_json(embedding_body())

        response = await client.post(
            "/v1/embeddings", json={"model": "embed-1", "input": "x"}, headers=seeded.auth
        )
        assert fake_upstream.bodies[-1]["model"] == "vendor/embed-1"
        assert response.json()["model"] == "embed-1"

    async def test_a_single_string_stays_a_single_string(
        self,
        client: httpx.AsyncClient,
        seeded: Seeded,
        session: AsyncSession,
        fake_upstream: FakeUpstream,
    ) -> None:
        # A provider may index its response by position; turning one string into
        # a one-element list would change the shape the caller gets back.
        await add_embedding_model(session, seeded)
        fake_upstream.set_json(embedding_body())

        await client.post(
            "/v1/embeddings", json={"model": "embed-1", "input": "just one"}, headers=seeded.auth
        )
        assert fake_upstream.bodies[-1]["input"] == "just one"

    async def test_extra_parameters_are_forwarded(
        self,
        client: httpx.AsyncClient,
        seeded: Seeded,
        session: AsyncSession,
        fake_upstream: FakeUpstream,
    ) -> None:
        """The route is a proxy: it validates what it acts on and forwards the rest."""
        await add_embedding_model(session, seeded)
        fake_upstream.set_json(embedding_body())

        await client.post(
            "/v1/embeddings",
            json={"model": "embed-1", "input": "x", "encoding_format": "base64", "dimensions": 256},
            headers=seeded.auth,
        )
        sent = fake_upstream.bodies[-1]
        assert sent["encoding_format"] == "base64"
        assert sent["dimensions"] == 256

    async def test_an_empty_input_is_refused(
        self, client: httpx.AsyncClient, seeded: Seeded, session: AsyncSession
    ) -> None:
        await add_embedding_model(session, seeded)
        response = await client.post(
            "/v1/embeddings", json={"model": "embed-1", "input": ""}, headers=seeded.auth
        )
        assert response.status_code == 400

    async def test_an_inaccessible_model_is_404(
        self, client: httpx.AsyncClient, seeded: Seeded
    ) -> None:
        response = await client.post(
            "/v1/embeddings", json={"model": "nope", "input": "x"}, headers=seeded.auth
        )
        assert response.status_code == 404

    async def test_the_route_needs_an_api_key(self, client: httpx.AsyncClient) -> None:
        response = await client.post("/v1/embeddings", json={"model": "m", "input": "x"})
        assert response.status_code == 401


class TestModelKind:
    async def test_a_chat_model_is_refused_by_the_embeddings_route(
        self, client: httpx.AsyncClient, seeded: Seeded
    ) -> None:
        """Named here rather than forwarded to fail upstream with a
        provider-specific error nobody can act on."""
        response = await client.post(
            "/v1/embeddings", json={"model": "test-model", "input": "x"}, headers=seeded.auth
        )
        assert response.status_code == 400
        assert "/v1/chat/completions" in response.json()["error"]["message"]

    async def test_an_embedding_model_is_refused_by_the_chat_route(
        self, client: httpx.AsyncClient, seeded: Seeded, session: AsyncSession
    ) -> None:
        await add_embedding_model(session, seeded)
        response = await client.post(
            "/v1/chat/completions",
            json={"model": "embed-1", "messages": [{"role": "user", "content": "hi"}]},
            headers=seeded.auth,
        )
        assert response.status_code == 400
        assert "/v1/embeddings" in response.json()["error"]["message"]


class TestEmbeddingAccounting:
    async def test_only_the_input_is_charged(
        self,
        client: httpx.AsyncClient,
        seeded: Seeded,
        session: AsyncSession,
        fake_upstream: FakeUpstream,
    ) -> None:
        """completion_tokens is always 0 for embeddings, so the output price —
        which this model deliberately has — must never be applied."""
        await add_embedding_model(session, seeded, input_per_mtok="0.10")
        fake_upstream.set_json(embedding_body(prompt_tokens=1_000_000))

        await client.post(
            "/v1/embeddings", json={"model": "embed-1", "input": "x"}, headers=seeded.auth
        )

        record = (
            (await session.execute(select(UsageRecord).where(UsageRecord.model_name == "embed-1")))
            .scalars()
            .one()
        )
        # 1M input tokens at EUR 0.10/Mtok, and nothing for output.
        assert record.cost == Decimal("0.10")
        assert record.completion_tokens == 0
        assert record.usage_source is UsageSource.UPSTREAM_EXACT

    async def test_the_provider_and_model_that_served_it_are_recorded(
        self,
        client: httpx.AsyncClient,
        seeded: Seeded,
        session: AsyncSession,
        fake_upstream: FakeUpstream,
    ) -> None:
        await add_embedding_model(session, seeded)
        fake_upstream.set_json(embedding_body())

        await client.post(
            "/v1/embeddings", json={"model": "embed-1", "input": "x"}, headers=seeded.auth
        )
        record = (
            (await session.execute(select(UsageRecord).where(UsageRecord.model_name == "embed-1")))
            .scalars()
            .one()
        )
        assert record.upstream_model == "vendor/embed-1"
        assert record.upstream_provider == "acme-cloud"
        # Same name we asked for, so not a substitution.
        assert record.model_substituted is False

    async def test_a_substituted_model_is_flagged(
        self,
        client: httpx.AsyncClient,
        seeded: Seeded,
        session: AsyncSession,
        fake_upstream: FakeUpstream,
    ) -> None:
        """Model fallback: the provider served something else and bills for it."""
        await add_embedding_model(session, seeded)
        fake_upstream.set_json(embedding_body(model="vendor/embed-1-mini"))

        await client.post(
            "/v1/embeddings", json={"model": "embed-1", "input": "x"}, headers=seeded.auth
        )
        record = (
            (await session.execute(select(UsageRecord).where(UsageRecord.model_name == "embed-1")))
            .scalars()
            .one()
        )
        assert record.upstream_model == "vendor/embed-1-mini"
        assert record.model_substituted is True

    async def test_our_own_naming_is_not_mistaken_for_a_substitution(
        self,
        client: httpx.AsyncClient,
        seeded: Seeded,
        session: AsyncSession,
        fake_upstream: FakeUpstream,
    ) -> None:
        """The false positive this flag exists to avoid: `model_name` is our
        client-facing name and `upstream_model` is the provider's, so those two
        always differ. Comparing them would mark every request."""
        await add_embedding_model(session, seeded)
        fake_upstream.set_json(embedding_body())

        await client.post(
            "/v1/embeddings", json={"model": "embed-1", "input": "x"}, headers=seeded.auth
        )
        record = (
            (await session.execute(select(UsageRecord).where(UsageRecord.model_name == "embed-1")))
            .scalars()
            .one()
        )
        assert record.model_name != record.upstream_model
        assert record.model_substituted is False

    async def test_an_upstream_error_is_not_billed(
        self,
        client: httpx.AsyncClient,
        seeded: Seeded,
        session: AsyncSession,
        fake_upstream: FakeUpstream,
    ) -> None:
        await add_embedding_model(session, seeded)
        fake_upstream.set_json({"error": {"message": "nope"}}, status=503)

        response = await client.post(
            "/v1/embeddings", json={"model": "embed-1", "input": "x"}, headers=seeded.auth
        )
        assert response.status_code == 503
        record = (
            (await session.execute(select(UsageRecord).where(UsageRecord.model_name == "embed-1")))
            .scalars()
            .one()
        )
        assert record.cost == Decimal(0)


class TestEmbeddingRedaction:
    async def test_input_text_is_redacted_before_it_leaves(
        self,
        app: object,
        client: httpx.AsyncClient,
        seeded: Seeded,
        session: AsyncSession,
        fake_upstream: FakeUpstream,
    ) -> None:
        """An embeddings route that forwarded raw text would let anyone bypass
        redaction by embedding the document instead of asking about it."""
        await add_embedding_model(session, seeded)
        fake_upstream.set_json(embedding_body())
        app.state.redactor = _ShoutyRedactor()  # type: ignore[attr-defined]

        await client.post(
            "/v1/embeddings",
            json={"model": "embed-1", "input": ["contact Mario Rossi", "and again Mario Rossi"]},
            headers=seeded.auth,
        )
        sent = fake_upstream.bodies[-1]["input"]
        assert all("Mario Rossi" not in text for text in sent)
        assert all("<PERSON_X>" in text for text in sent)

    async def test_the_same_entity_gets_the_same_placeholder_across_the_batch(
        self,
        app: object,
        client: httpx.AsyncClient,
        seeded: Seeded,
        session: AsyncSession,
        fake_upstream: FakeUpstream,
    ) -> None:
        """What makes a redacted corpus internally consistent: an indexed
        document and a later query agree with each other (ADR 0028)."""
        await add_embedding_model(session, seeded)
        fake_upstream.set_json(embedding_body())
        app.state.redactor = _ShoutyRedactor()  # type: ignore[attr-defined]

        await client.post(
            "/v1/embeddings",
            json={"model": "embed-1", "input": ["Mario Rossi wrote it", "ask Mario Rossi"]},
            headers=seeded.auth,
        )
        first, second = fake_upstream.bodies[-1]["input"]
        assert "<PERSON_X>" in first and "<PERSON_X>" in second


class _ShoutyRedactor:
    """Replaces one fixed name, deterministically, in whatever it is given."""

    name = "test"

    async def redact_request(self, messages: list[dict]) -> RedactionOutcome:
        rewritten = []
        count = 0
        for message in messages:
            text = str(message.get("content") or "")
            if "Mario Rossi" in text:
                count += text.count("Mario Rossi")
                text = text.replace("Mario Rossi", "<PERSON_X>")
            rewritten.append({**message, "content": text})
        return RedactionOutcome(messages=rewritten, entity_count=count, engine="test")

    def response_stage(self, outcome: RedactionOutcome):  # type: ignore[no-untyped-def]
        from gateway.sse.pipeline import passthrough

        return passthrough

    async def redact_response_text(self, text: str, outcome: RedactionOutcome) -> str:
        return text
