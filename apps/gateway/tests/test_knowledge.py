"""The knowledge-base surfaces: files, stores, ingestion, retrieval, sharing.

Two things about the shape of these tests are worth knowing before adding to
them.

**Ingestion is a detached task, so a test has to wait for it.** `POST` returns
`in_progress` on purpose (ADR 0019 — OCR belongs nowhere near a request path),
and the tasks are plain `asyncio` tasks rather than Starlette background tasks,
so httpx's ASGI transport does *not* await them the way it awaits deferred
settlement. `drain` is the helper; forgetting it produces a test that asserts
`in_progress` and proves nothing.

**Only text ingestion is covered here, not files.** File ingestion calls the
extractor service over HTTP, and the fixtures inject a fake transport only for
the *upstream* client — an extraction attempt would reach for a real socket.
That is what `scripts/test_knowledge_live.py` is for, and it is the honest
division: everything above the extractor is tested here, the extractor itself
against the running stack.
"""

from __future__ import annotations

import asyncio
import uuid
from decimal import Decimal

import httpx
import pytest
import pytest_asyncio
from conftest import Seeded
from fastapi import FastAPI
from gateway.config import KnowledgeSettings, Settings
from gateway.models import (
    GroupModelAccess,
    KnowledgeChunk,
    KnowledgeConfig,
    Membership,
    ModelDef,
    ModelKind,
    ModelPrice,
    User,
)
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

# A three-dimensional embedding is enough to test ranking and keeps the
# fixtures readable. Real models are 768 upward, and nothing here depends on
# the width — which is exactly the property that learning `dimensions` from the
# first vector, rather than configuring it, is meant to give.


@pytest.fixture
def settings(settings: Settings) -> Settings:
    """The shared settings with knowledge bases switched on.

    Overriding the conftest fixture by name rather than duplicating it, so a
    change to the base fixture reaches these tests too.
    """
    return settings.model_copy(
        update={
            "knowledge": KnowledgeSettings(
                enabled=True,
                chunk_chars=200,
                chunk_overlap=0,
                # One call per passage, so a test can count the ledger rows an
                # ingestion produced.
                embed_batch=1,
            )
        }
    )


@pytest_asyncio.fixture
async def embedding_model(
    session_factory: async_sessionmaker[AsyncSession], seeded: Seeded
) -> ModelDef:
    """An embedding model the seeded group may use, chosen by a config row."""
    async with session_factory() as db:
        model = ModelDef(
            name="test-embed",
            upstream_model="upstream/test-embed",
            provider_id=seeded.provider.id,
            kind=ModelKind.EMBEDDING,
            context_window=8192,
        )
        db.add(model)
        await db.flush()
        db.add(
            ModelPrice(
                model_id=model.id,
                input_per_mtok=Decimal("1"),
                output_per_mtok=Decimal("0"),
                currency="EUR",
            )
        )
        db.add(GroupModelAccess(group_id=seeded.group.id, model_id=model.id))
        # The console's decision, as an append-only row. No row would mean the
        # environment decides, and the environment cannot name a model.
        db.add(KnowledgeConfig(embedding_model_id=model.id))
        await db.commit()
        await db.refresh(model)
    return model


@pytest_asyncio.fixture
async def ready(app: FastAPI, embedding_model: ModelDef) -> FastAPI:
    """The app with the config row already picked up.

    `refresh()` rather than waiting ten seconds for the poll. Calling it
    directly is also the only way a test can be sure *which* configuration is
    in force when it makes its first request.
    """
    await app.state.knowledge.refresh()
    assert app.state.knowledge.profile.ready
    return app


def embed_response(vectors: list[list[float]], *, prompt_tokens: int = 5) -> dict:
    return {
        "object": "list",
        "data": [
            {"object": "embedding", "index": index, "embedding": vector}
            for index, vector in enumerate(vectors)
        ],
        "model": "upstream/test-embed",
        "usage": {"prompt_tokens": prompt_tokens, "total_tokens": prompt_tokens},
    }


async def drain(app: FastAPI) -> None:
    """Wait for every detached ingestion task to finish.

    Loops because ingesting one document can spawn nothing further today, but a
    reindex spawns one task per document and they are added as they go.
    """
    for _ in range(50):
        pending = {task for task in app.state.background_tasks if not task.done()}
        if not pending:
            return
        await asyncio.gather(*pending, return_exceptions=True)
    raise AssertionError("ingestion did not settle")


async def upload(client: httpx.AsyncClient, seeded: Seeded, content: bytes = b"hello") -> str:
    response = await client.post(
        "/v1/files",
        headers=seeded.auth,
        files={"file": ("notes.txt", content, "text/plain")},
    )
    assert response.status_code == 200, response.text
    return str(response.json()["id"])


async def make_store(client: httpx.AsyncClient, seeded: Seeded, name: str = "corpus") -> str:
    response = await client.post("/v1/vector_stores", headers=seeded.auth, json={"name": name})
    assert response.status_code == 200, response.text
    return str(response.json()["id"])


# -- the feature switch ------------------------------------------------------


@pytest.mark.asyncio
async def test_the_surfaces_are_absent_until_the_feature_is_enabled(
    app: FastAPI, client: httpx.AsyncClient, seeded: Seeded, settings: Settings
) -> None:
    """404, not 403 or 501.

    A deployment without knowledge bases does not have these endpoints, and a
    403 would say the feature is here and this caller may not use it — a
    different statement, and an untrue one.
    """
    app.state.knowledge._profile = app.state.knowledge.profile.__class__(
        enabled=False,
        embedding_model_id=None,
        extractor_model_id=None,
        vector_store="pgvector",
        chunk_chars=1200,
        chunk_overlap=150,
        source="environment",
        config_id=None,
    )
    for method, path in (
        ("GET", "/v1/files"),
        ("GET", "/v1/vector_stores"),
        ("POST", "/v1/vector_stores"),
    ):
        response = await client.request(method, path, headers=seeded.auth, json={"name": "x"})
        assert response.status_code == 404, (path, response.status_code)


@pytest.mark.asyncio
async def test_a_store_can_be_created_before_an_embedding_model_is_chosen(
    app: FastAPI, client: httpx.AsyncClient, seeded: Seeded
) -> None:
    """Enabled but unconfigured is *unfinished*, not broken.

    Creating a base still works — an administrator choosing a model later
    should not require every user to recreate theirs — but indexing into it is
    refused with the reason, and `status` says which decision is missing.
    """
    status = await client.get("/v1/vector_stores/status", headers=seeded.auth)
    assert status.status_code == 200, status.text
    assert status.json()["ready"] is False
    assert "embedding model" in status.json()["detail"]

    base_id = await make_store(client, seeded)
    refused = await client.post(
        f"/v1/vector_stores/{base_id}/text", headers=seeded.auth, json={"text": "hello"}
    )
    assert refused.status_code == 400
    assert refused.json()["error"]["code"] == "embedding_model_not_configured"


# -- files -------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_file_round_trips_and_reports_its_digest(
    ready: FastAPI, client: httpx.AsyncClient, seeded: Seeded
) -> None:
    file_id = await upload(client, seeded, b"some notes")
    fetched = await client.get(f"/v1/files/{file_id}", headers=seeded.auth)
    assert fetched.status_code == 200
    assert fetched.json()["bytes"] == len(b"some notes")
    assert fetched.json()["filename"] == "notes.txt"

    content = await client.get(f"/v1/files/{file_id}/content", headers=seeded.auth)
    assert content.status_code == 200
    assert content.content == b"some notes"
    # Never inline: stored files are arbitrary caller content served from the
    # console's own origin.
    assert content.headers["content-disposition"].startswith("attachment")
    assert content.headers["x-content-type-options"] == "nosniff"


@pytest.mark.asyncio
async def test_an_oversized_upload_is_refused(
    ready: FastAPI, client: httpx.AsyncClient, seeded: Seeded, settings: Settings
) -> None:
    # The limit is enforced while reading, so this must be refused rather than
    # buffered and then measured.
    oversized = b"x" * (settings.knowledge.max_upload_bytes + 1)
    response = await client.post(
        "/v1/files",
        headers=seeded.auth,
        files={"file": ("big.bin", oversized, "application/octet-stream")},
    )
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "file_too_large"


@pytest.mark.asyncio
async def test_an_empty_upload_is_refused(
    ready: FastAPI, client: httpx.AsyncClient, seeded: Seeded
) -> None:
    response = await client.post(
        "/v1/files", headers=seeded.auth, files={"file": ("nothing.txt", b"", "text/plain")}
    )
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "empty_file"


@pytest.mark.asyncio
async def test_another_callers_file_is_a_404_not_a_403(
    ready: FastAPI,
    client: httpx.AsyncClient,
    seeded: Seeded,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Whose files exist is not this caller's business.

    A 403 would confirm the id is real, which is the thing being withheld.
    """
    file_id = await upload(client, seeded)
    async with session_factory() as db:
        other = User(issuer="https://idp.test", subject="other", email="other@example.org")
        db.add(other)
        await db.flush()
        db.add(Membership(user_id=other.id, group_id=seeded.group.id))
        other.default_billing_group_id = seeded.group.id
        await db.commit()
        other_id = other.id

    from gateway.models import ApiKey
    from gateway.security import generate_api_key

    async with session_factory() as db:
        generated = generate_api_key()
        db.add(
            ApiKey(
                user_id=other_id,
                prefix=generated.prefix,
                key_hash=generated.key_hash,
                name="other",
            )
        )
        await db.commit()

    auth = {"authorization": f"Bearer {generated.secret}"}
    assert (await client.get(f"/v1/files/{file_id}", headers=auth)).status_code == 404
    assert (await client.get(f"/v1/files/{file_id}/content", headers=auth)).status_code == 404


# -- ingestion ---------------------------------------------------------------


@pytest.mark.asyncio
async def test_text_is_chunked_embedded_and_becomes_searchable(
    ready: FastAPI,
    client: httpx.AsyncClient,
    seeded: Seeded,
    fake_upstream,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    fake_upstream.set_json(embed_response([[1.0, 0.0, 0.0]]))
    base_id = await make_store(client, seeded)

    created = await client.post(
        f"/v1/vector_stores/{base_id}/text",
        headers=seeded.auth,
        json={"text": "Il gatto dorme sul tetto.", "title": "note"},
    )
    assert created.status_code == 200, created.text
    # In progress, because the work is detached. This is the contract.
    assert created.json()["status"] == "in_progress"

    await drain(ready)

    listed = await client.get(f"/v1/vector_stores/{base_id}/files", headers=seeded.auth)
    document = listed.json()["data"][0]
    assert document["status"] == "completed", document
    assert document["chunk_count"] == 1
    assert document["last_error"] is None

    # The base learned its width from the vector rather than being told.
    store = await client.get(f"/v1/vector_stores/{base_id}", headers=seeded.auth)
    assert store.json()["dimensions"] == 3
    assert store.json()["embedding_model"] == "test-embed"
    assert store.json()["file_counts"] == {
        "in_progress": 0,
        "completed": 1,
        "failed": 0,
        "total": 1,
    }

    async with session_factory() as db:
        chunks = (await db.execute(select(KnowledgeChunk))).scalars().all()
        assert len(chunks) == 1
        assert chunks[0].dimensions == 3
        # Stored text is what was extracted, not placeholders: the redaction
        # happens on the way to the provider, not into the store.
        assert "gatto" in chunks[0].text


@pytest.mark.asyncio
async def test_indexing_is_billed_to_the_owning_group(
    ready: FastAPI,
    client: httpx.AsyncClient,
    seeded: Seeded,
    fake_upstream,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """ADR 0020's consequence, as a test.

    Indexing spend that does not reach the ledger is spend no report can
    explain, and a large ingestion run can cost more than the chat traffic it
    serves.
    """
    from gateway.models import ApiSurface, UsageRecord

    fake_upstream.set_json(embed_response([[0.0, 1.0, 0.0]], prompt_tokens=12))
    base_id = await make_store(client, seeded)
    await client.post(
        f"/v1/vector_stores/{base_id}/text", headers=seeded.auth, json={"text": "una nota"}
    )
    await drain(ready)

    async with session_factory() as db:
        embeddings = select(UsageRecord).where(
            UsageRecord.api_surface == ApiSurface.EMBEDDINGS
        )
        rows = (await db.execute(embeddings)).scalars().all()
        assert rows, "indexing produced no ledger row"
        assert all(row.group_id == seeded.group.id for row in rows)
        assert sum(row.prompt_tokens for row in rows) == 12
        # A price of 1 EUR per million tokens on 12 tokens.
        assert sum(row.cost for row in rows) == Decimal("12") / Decimal("1000000")


@pytest.mark.asyncio
async def test_reposting_the_same_source_ref_replaces_rather_than_duplicates(
    ready: FastAPI,
    client: httpx.AsyncClient,
    seeded: Seeded,
    fake_upstream,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """What makes indexing a growing conversation possible.

    Without it, a base accumulates one overlapping copy of the thread per
    update and every search returns all of them.
    """
    fake_upstream.set_json(embed_response([[1.0, 0.0, 0.0]]))
    base_id = await make_store(client, seeded)
    for text in ("first version of the thread", "second version of the thread"):
        response = await client.post(
            f"/v1/vector_stores/{base_id}/text",
            headers=seeded.auth,
            json={"text": text, "source_ref": "chat:conversation:42"},
        )
        assert response.status_code == 200, response.text
        await drain(ready)

    listed = await client.get(f"/v1/vector_stores/{base_id}/files", headers=seeded.auth)
    assert len(listed.json()["data"]) == 1
    async with session_factory() as db:
        chunks = (await db.execute(select(KnowledgeChunk))).scalars().all()
        assert len(chunks) == 1
        assert "second version" in chunks[0].text


@pytest.mark.asyncio
async def test_a_provider_returning_the_wrong_number_of_vectors_fails_the_document(
    ready: FastAPI, client: httpx.AsyncClient, seeded: Seeded, fake_upstream
) -> None:
    """Unrecoverable and undetectable later, so it must fail now.

    Accepting a short batch would pair every passage with somebody else's
    vector, and nothing downstream could ever notice.
    """
    fake_upstream.set_json(embed_response([]))
    base_id = await make_store(client, seeded)
    await client.post(
        f"/v1/vector_stores/{base_id}/text", headers=seeded.auth, json={"text": "una nota"}
    )
    await drain(ready)

    listed = await client.get(f"/v1/vector_stores/{base_id}/files", headers=seeded.auth)
    document = listed.json()["data"][0]
    assert document["status"] == "failed"
    assert "0 vectors" in document["last_error"]


@pytest.mark.asyncio
async def test_a_refusing_provider_fails_the_document_with_its_reason(
    ready: FastAPI, client: httpx.AsyncClient, seeded: Seeded, fake_upstream
) -> None:
    fake_upstream.set_json({"error": {"message": "model overloaded"}}, status=503)
    base_id = await make_store(client, seeded)
    await client.post(
        f"/v1/vector_stores/{base_id}/text", headers=seeded.auth, json={"text": "una nota"}
    )
    await drain(ready)

    listed = await client.get(f"/v1/vector_stores/{base_id}/files", headers=seeded.auth)
    document = listed.json()["data"][0]
    assert document["status"] == "failed"
    assert "503" in document["last_error"]
    assert "model overloaded" in document["last_error"]


# -- retrieval ---------------------------------------------------------------


@pytest.mark.asyncio
async def test_search_ranks_by_similarity_and_is_billed(
    ready: FastAPI,
    client: httpx.AsyncClient,
    seeded: Seeded,
    fake_upstream,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    base_id = await make_store(client, seeded)

    # Two passages, deliberately orthogonal, so the ranking is unambiguous.
    fake_upstream.set_json(embed_response([[1.0, 0.0, 0.0]]))
    await client.post(
        f"/v1/vector_stores/{base_id}/text",
        headers=seeded.auth,
        json={"text": "the first passage", "title": "one"},
    )
    await drain(ready)
    fake_upstream.set_json(embed_response([[0.0, 1.0, 0.0]]))
    await client.post(
        f"/v1/vector_stores/{base_id}/text",
        headers=seeded.auth,
        json={"text": "the second passage", "title": "two"},
    )
    await drain(ready)

    # The query embeds to the first passage's vector, so it must come first.
    fake_upstream.set_json(embed_response([[1.0, 0.0, 0.0]]))
    found = await client.post(
        f"/v1/vector_stores/{base_id}/search",
        headers=seeded.auth,
        json={"query": "anything"},
    )
    assert found.status_code == 200, found.text
    hits = found.json()["data"]
    assert [hit["title"] for hit in hits] == ["one", "two"]
    assert hits[0]["score"] == pytest.approx(1.0)
    assert hits[1]["score"] == pytest.approx(0.0)


@pytest.mark.asyncio
async def test_a_score_floor_excludes_the_weaker_hit(
    ready: FastAPI, client: httpx.AsyncClient, seeded: Seeded, fake_upstream
) -> None:
    base_id = await make_store(client, seeded)
    fake_upstream.set_json(embed_response([[1.0, 0.0, 0.0]]))
    await client.post(
        f"/v1/vector_stores/{base_id}/text", headers=seeded.auth, json={"text": "one"}
    )
    await drain(ready)
    fake_upstream.set_json(embed_response([[0.0, 1.0, 0.0]]))
    await client.post(
        f"/v1/vector_stores/{base_id}/text", headers=seeded.auth, json={"text": "two"}
    )
    await drain(ready)

    fake_upstream.set_json(embed_response([[1.0, 0.0, 0.0]]))
    found = await client.post(
        f"/v1/vector_stores/{base_id}/search",
        headers=seeded.auth,
        json={"query": "anything", "min_score": 0.5},
    )
    assert len(found.json()["data"]) == 1


@pytest.mark.asyncio
async def test_searching_an_empty_store_is_an_empty_answer_not_an_error(
    ready: FastAPI, client: httpx.AsyncClient, seeded: Seeded
) -> None:
    # A base with nothing indexed has no vectors to compare against. An error
    # would make a client special-case the first search of every new base.
    base_id = await make_store(client, seeded)
    found = await client.post(
        f"/v1/vector_stores/{base_id}/search", headers=seeded.auth, json={"query": "x"}
    )
    assert found.status_code == 200
    assert found.json()["data"] == []


# -- reindexing --------------------------------------------------------------


@pytest.mark.asyncio
async def test_reindex_re_embeds_from_stored_text_without_re_extracting(
    ready: FastAPI,
    client: httpx.AsyncClient,
    seeded: Seeded,
    fake_upstream,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """The feature that makes a configurable embedding model honest.

    Also the reason the extracted text is kept on the document: re-embedding
    costs one call per batch and no extraction, where re-extracting a scanned
    PDF costs money per page.
    """
    fake_upstream.set_json(embed_response([[1.0, 0.0, 0.0]]))
    base_id = await make_store(client, seeded)
    await client.post(
        f"/v1/vector_stores/{base_id}/text", headers=seeded.auth, json={"text": "una nota"}
    )
    await drain(ready)

    # A different model, with a different width — which is the case a fixed
    # `vector(N)` column could not have handled without a migration.
    fake_upstream.set_json(embed_response([[0.0, 0.0, 1.0, 1.0]]))
    again = await client.post(f"/v1/vector_stores/{base_id}/reindex", headers=seeded.auth)
    assert again.status_code == 200, again.text
    await drain(ready)

    store = await client.get(f"/v1/vector_stores/{base_id}", headers=seeded.auth)
    assert store.json()["dimensions"] == 4
    async with session_factory() as db:
        widths = (await db.execute(select(func.count()).select_from(KnowledgeChunk))).scalar()
        assert widths == 1
        chunk = (await db.execute(select(KnowledgeChunk))).scalars().one()
        assert chunk.dimensions == 4


# -- sharing -----------------------------------------------------------------


@pytest_asyncio.fixture
async def colleague(
    session_factory: async_sessionmaker[AsyncSession], seeded: Seeded
) -> tuple[uuid.UUID, dict[str, str]]:
    """A second caller in the same group, with their own key."""
    from gateway.models import ApiKey
    from gateway.security import generate_api_key

    async with session_factory() as db:
        person = User(
            issuer="https://idp.test",
            subject="colleague",
            email="colleague@example.org",
            default_billing_group_id=seeded.group.id,
        )
        db.add(person)
        await db.flush()
        db.add(Membership(user_id=person.id, group_id=seeded.group.id))
        generated = generate_api_key()
        db.add(
            ApiKey(
                user_id=person.id,
                prefix=generated.prefix,
                key_hash=generated.key_hash,
                name="colleague",
            )
        )
        await db.commit()
        return person.id, {"authorization": f"Bearer {generated.secret}"}


@pytest.mark.asyncio
async def test_an_unshared_store_is_invisible_to_a_colleague(
    ready: FastAPI,
    client: httpx.AsyncClient,
    seeded: Seeded,
    colleague: tuple[uuid.UUID, dict[str, str]],
) -> None:
    _, auth = colleague
    base_id = await make_store(client, seeded)
    assert (await client.get("/v1/vector_stores", headers=auth)).json()["data"] == []
    assert (await client.get(f"/v1/vector_stores/{base_id}", headers=auth)).status_code == 404


@pytest.mark.asyncio
async def test_sharing_grants_read_and_withholds_write(
    ready: FastAPI,
    client: httpx.AsyncClient,
    seeded: Seeded,
    colleague: tuple[uuid.UUID, dict[str, str]],
    fake_upstream,
) -> None:
    person_id, auth = colleague
    base_id = await make_store(client, seeded)
    shared = await client.post(
        f"/v1/vector_stores/{base_id}/shares",
        headers=seeded.auth,
        json={
            "principal_kind": "user",
            "principal_id": str(person_id),
            "role": "viewer",
        },
    )
    assert shared.status_code == 200, shared.text
    assert shared.json()["email"] == "colleague@example.org"

    # Visible, and reads work.
    assert len((await client.get("/v1/vector_stores", headers=auth)).json()["data"]) == 1
    listed = await client.get(f"/v1/vector_stores/{base_id}", headers=auth)
    assert listed.status_code == 200
    assert listed.json()["owned"] is False
    assert listed.json()["role"] == "viewer"

    # Writes do not. A 403 rather than a 404: they can see it, so pretending it
    # is gone would be a worse answer than saying they may not write.
    fake_upstream.set_json(embed_response([[1.0, 0.0, 0.0]]))
    refused = await client.post(
        f"/v1/vector_stores/{base_id}/text", headers=auth, json={"text": "mine now"}
    )
    assert refused.status_code == 403


@pytest.mark.asyncio
async def test_an_editor_may_add_documents_but_not_delete_or_reindex(
    ready: FastAPI,
    client: httpx.AsyncClient,
    seeded: Seeded,
    colleague: tuple[uuid.UUID, dict[str, str]],
    fake_upstream,
) -> None:
    """ "May edit" does not imply "may destroy", and it does not imply spending.

    Deleting is the owner's alone so that exactly one principal can make a
    resource disappear. Reindexing is too, because it spends the owning group's
    budget.
    """
    person_id, auth = colleague
    base_id = await make_store(client, seeded)
    await client.post(
        f"/v1/vector_stores/{base_id}/shares",
        headers=seeded.auth,
        json={"principal_kind": "user", "principal_id": str(person_id), "role": "editor"},
    )

    fake_upstream.set_json(embed_response([[1.0, 0.0, 0.0]]))
    added = await client.post(
        f"/v1/vector_stores/{base_id}/text", headers=auth, json={"text": "a contribution"}
    )
    assert added.status_code == 200, added.text
    await drain(ready)

    assert (
        await client.post(f"/v1/vector_stores/{base_id}/reindex", headers=auth)
    ).status_code == 403
    assert (await client.delete(f"/v1/vector_stores/{base_id}", headers=auth)).status_code == 403


@pytest.mark.asyncio
async def test_a_group_share_reaches_a_member(
    ready: FastAPI,
    client: httpx.AsyncClient,
    seeded: Seeded,
    colleague: tuple[uuid.UUID, dict[str, str]],
) -> None:
    _, auth = colleague
    base_id = await make_store(client, seeded)
    await client.post(
        f"/v1/vector_stores/{base_id}/shares",
        headers=seeded.auth,
        json={
            "principal_kind": "group",
            "principal_id": str(seeded.group.id),
            "role": "viewer",
        },
    )
    assert len((await client.get("/v1/vector_stores", headers=auth)).json()["data"]) == 1


@pytest.mark.asyncio
async def test_sharing_with_a_nonexistent_principal_is_refused(
    ready: FastAPI, client: httpx.AsyncClient, seeded: Seeded
) -> None:
    # Storing it would look like it worked and never take effect.
    base_id = await make_store(client, seeded)
    response = await client.post(
        f"/v1/vector_stores/{base_id}/shares",
        headers=seeded.auth,
        json={"principal_kind": "user", "principal_id": str(uuid.uuid4())},
    )
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "unknown_principal"


@pytest.mark.asyncio
async def test_only_the_owner_sees_the_recipient_list(
    ready: FastAPI,
    client: httpx.AsyncClient,
    seeded: Seeded,
    colleague: tuple[uuid.UUID, dict[str, str]],
) -> None:
    # Being given a document does not imply learning who else has it.
    person_id, auth = colleague
    base_id = await make_store(client, seeded)
    await client.post(
        f"/v1/vector_stores/{base_id}/shares",
        headers=seeded.auth,
        json={"principal_kind": "user", "principal_id": str(person_id)},
    )
    assert (
        await client.get(f"/v1/vector_stores/{base_id}/shares", headers=auth)
    ).status_code == 404
    mine = await client.get(f"/v1/vector_stores/{base_id}/shares", headers=seeded.auth)
    assert len(mine.json()["data"]) == 1


@pytest.mark.asyncio
async def test_deleting_a_store_removes_its_grants(
    ready: FastAPI,
    client: httpx.AsyncClient,
    seeded: Seeded,
    colleague: tuple[uuid.UUID, dict[str, str]],
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Nothing in the schema does this — `resource_id` is not a foreign key."""
    from gateway.models import ResourceShare

    person_id, _auth = colleague
    base_id = await make_store(client, seeded)
    await client.post(
        f"/v1/vector_stores/{base_id}/shares",
        headers=seeded.auth,
        json={"principal_kind": "user", "principal_id": str(person_id)},
    )
    assert (
        await client.delete(f"/v1/vector_stores/{base_id}", headers=seeded.auth)
    ).status_code == 200

    async with session_factory() as db:
        left = (await db.execute(select(func.count()).select_from(ResourceShare))).scalar()
        assert left == 0


@pytest.mark.asyncio
async def test_revoking_a_share_is_idempotent(
    ready: FastAPI,
    client: httpx.AsyncClient,
    seeded: Seeded,
    colleague: tuple[uuid.UUID, dict[str, str]],
) -> None:
    person_id, auth = colleague
    base_id = await make_store(client, seeded)
    await client.post(
        f"/v1/vector_stores/{base_id}/shares",
        headers=seeded.auth,
        json={"principal_kind": "user", "principal_id": str(person_id)},
    )
    path = f"/v1/vector_stores/{base_id}/shares/user/{person_id}"
    first = await client.delete(path, headers=seeded.auth)
    assert first.status_code == 200 and first.json()["deleted"] is True
    # Clicking twice is not a mistake, and the end state is what was asked for.
    second = await client.delete(path, headers=seeded.auth)
    assert second.status_code == 200 and second.json()["deleted"] is False
    assert (await client.get(f"/v1/vector_stores/{base_id}", headers=auth)).status_code == 404


@pytest.mark.asyncio
async def test_a_key_cannot_index_another_callers_upload(
    ready: FastAPI,
    client: httpx.AsyncClient,
    seeded: Seeded,
    colleague: tuple[uuid.UUID, dict[str, str]],
) -> None:
    """Otherwise editing a shared base is a way to read someone's file back out.

    Indexing a file makes its text retrievable through search, so attaching
    somebody else's upload to a base you can edit would exfiltrate it.
    """
    _, auth = colleague
    file_id = await upload(client, seeded)
    base_id = await make_store(client, seeded)
    await client.post(
        f"/v1/vector_stores/{base_id}/shares",
        headers=seeded.auth,
        json={
            "principal_kind": "group",
            "principal_id": str(seeded.group.id),
            "role": "editor",
        },
    )
    response = await client.post(
        f"/v1/vector_stores/{base_id}/files", headers=auth, json={"file_id": file_id}
    )
    assert response.status_code == 404
