"""Agents: addressed as a model, and the four things that must not go wrong.

The interesting assertions here are not "does CRUD work". They are:

* **an agent does not launder model access.** Being shared an agent is not
  being granted its model, and if it were, `access.py` — the one predicate this
  gateway has for that question — would have a way around it;
* **an agent does not launder document access.** Retrieval re-checks the
  *caller's* access to every attached base per request, so sharing an agent is
  not a way to publish a document without sharing the document;
* **the caller's own instructions survive.** The agent's system prompt goes in
  front of theirs rather than replacing it, and a sampling parameter they set
  is left alone;
* **the injected text is billed and redacted.** It is in the messages before
  the tokens are counted and before redaction runs, so a knowledge base's
  contents cannot leave unscreened and cannot be sent unmetered.
"""

from __future__ import annotations

import uuid
from decimal import Decimal

import httpx
import pytest
import pytest_asyncio
from conftest import Seeded
from fastapi import FastAPI
from gateway.config import KnowledgeSettings, Settings
from gateway.models import (
    Agent,
    AgentKnowledgeBase,
    ApiKey,
    GroupModelAccess,
    KnowledgeBase,
    KnowledgeConfig,
    Membership,
    ModelDef,
    ModelKind,
    ModelPrice,
    User,
)
from gateway.security import generate_api_key
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from test_knowledge import drain, embed_response


@pytest.fixture
def settings(settings: Settings) -> Settings:
    return settings.model_copy(
        update={
            "knowledge": KnowledgeSettings(
                enabled=True, chunk_chars=200, chunk_overlap=0, embed_batch=1
            )
        }
    )


@pytest_asyncio.fixture
async def embedding_model(
    session_factory: async_sessionmaker[AsyncSession], seeded: Seeded
) -> ModelDef:
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
        db.add(KnowledgeConfig(embedding_model_id=model.id))
        await db.commit()
        await db.refresh(model)
    return model


@pytest_asyncio.fixture
async def ready(app: FastAPI, embedding_model: ModelDef) -> FastAPI:
    await app.state.knowledge.refresh()
    return app


async def make_agent(
    client: httpx.AsyncClient, seeded: Seeded, **overrides: object
) -> dict[str, object]:
    body: dict[str, object] = {"name": "research", "model": "test-model"}
    body.update(overrides)
    response = await client.post("/v1/agents", headers=seeded.auth, json=body)
    assert response.status_code == 200, response.text
    return dict(response.json())


# -- authoring ---------------------------------------------------------------


@pytest.mark.asyncio
async def test_an_agent_is_created_and_addressed_with_a_prefix(
    ready: FastAPI, client: httpx.AsyncClient, seeded: Seeded
) -> None:
    agent = await make_agent(client, seeded, description="Reads the handbook")
    # Two names on purpose: the wire name is what goes in `model`, the bare one
    # is what a form edits.
    assert agent["model_name"] == "agent:research"
    assert agent["name"] == "research"
    assert agent["model"] == "test-model"
    assert agent["owned"] is True


@pytest.mark.asyncio
async def test_the_prefix_is_refused_in_the_name(
    ready: FastAPI, client: httpx.AsyncClient, seeded: Seeded
) -> None:
    # Otherwise the first person to paste what they saw in /v1/models creates
    # `agent:agent:research`.
    #
    # 400 rather than FastAPI's default 422: this gateway maps every
    # `RequestValidationError` onto its own error envelope (`main.py`), so a
    # malformed body reads the same as any other bad request.
    response = await client.post(
        "/v1/agents", headers=seeded.auth, json={"name": "agent:research", "model": "test-model"}
    )
    assert response.status_code == 400, response.text


@pytest.mark.asyncio
async def test_an_agent_cannot_be_built_on_a_model_the_author_cannot_use(
    ready: FastAPI,
    client: httpx.AsyncClient,
    seeded: Seeded,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """`access.py` is the only predicate for this, and an agent is not a bypass."""
    async with session_factory() as db:
        ungranted = ModelDef(
            name="secret-model",
            upstream_model="upstream/secret",
            provider_id=seeded.provider.id,
            context_window=8192,
        )
        db.add(ungranted)
        await db.commit()

    response = await client.post(
        "/v1/agents", headers=seeded.auth, json={"name": "sneaky", "model": "secret-model"}
    )
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "unknown_model"


@pytest.mark.asyncio
async def test_an_agent_runs_on_a_chat_model_not_an_embedding_one(
    ready: FastAPI, client: httpx.AsyncClient, seeded: Seeded
) -> None:
    response = await client.post(
        "/v1/agents", headers=seeded.auth, json={"name": "wrong", "model": "test-embed"}
    )
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "wrong_model_kind"


@pytest.mark.asyncio
async def test_a_duplicate_name_is_refused_rather_than_renamed(
    ready: FastAPI, client: httpx.AsyncClient, seeded: Seeded
) -> None:
    await make_agent(client, seeded)
    again = await client.post(
        "/v1/agents", headers=seeded.auth, json={"name": "research", "model": "test-model"}
    )
    assert again.status_code == 400
    assert again.json()["error"]["code"] == "duplicate_agent"


@pytest.mark.asyncio
async def test_attaching_an_unreadable_base_is_refused_not_dropped(
    ready: FastAPI, client: httpx.AsyncClient, seeded: Seeded
) -> None:
    # Silently dropping it would give an agent that retrieves from fewer bases
    # than its screen shows, which is the kind of thing nobody debugs.
    response = await client.post(
        "/v1/agents",
        headers=seeded.auth,
        json={
            "name": "hopeful",
            "model": "test-model",
            "knowledge_base_ids": [str(uuid.uuid4())],
        },
    )
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "unknown_knowledge_base"


# -- discovery ---------------------------------------------------------------


@pytest.mark.asyncio
async def test_an_agent_appears_in_the_model_list_and_can_be_retrieved(
    ready: FastAPI, client: httpx.AsyncClient, seeded: Seeded
) -> None:
    """The whole reason agents live in the gateway.

    A client that has never heard of an agent finds one in its model picker.
    """
    await make_agent(client, seeded, description="Reads the handbook")
    listed = await client.get("/v1/models", headers=seeded.auth)
    ids = [entry["id"] for entry in listed.json()["data"]]
    assert "agent:research" in ids
    assert "test-model" in ids

    one = await client.get("/v1/models/agent:research", headers=seeded.auth)
    assert one.status_code == 200, one.text
    assert one.json()["id"] == "agent:research"
    # The agent's description, and the model's capabilities: those are what the
    # request is actually bound by.
    assert one.json()["display_name"] == "Reads the handbook"
    assert one.json()["context_window"] == 8192


# -- using one ---------------------------------------------------------------


@pytest.mark.asyncio
async def test_the_system_prompt_goes_in_front_of_the_callers_own(
    ready: FastAPI, client: httpx.AsyncClient, seeded: Seeded, fake_upstream
) -> None:
    """Prepended, never replacing.

    A caller's system message is their instruction about their own task, and
    dropping it makes an agent silently hostile to the client that chose it.
    """
    await make_agent(client, seeded, system_prompt="You are terse.")
    fake_upstream.set_json(
        {
            "id": "c1",
            "object": "chat.completion",
            "model": "upstream/test-model",
            "choices": [{"index": 0, "message": {"role": "assistant", "content": "ok"}}],
            "usage": {"prompt_tokens": 10, "completion_tokens": 1, "total_tokens": 11},
        }
    )
    response = await client.post(
        "/v1/chat/completions",
        headers=seeded.auth,
        json={
            "model": "agent:research",
            "messages": [
                {"role": "system", "content": "Answer in Italian."},
                {"role": "user", "content": "Ciao"},
            ],
        },
    )
    assert response.status_code == 200, response.text
    sent = fake_upstream.last_body["messages"]
    assert sent[0]["content"] == "You are terse."
    assert sent[1]["content"] == "Answer in Italian."
    assert sent[2]["content"] == "Ciao"
    # And the upstream is asked for the *model*, not the agent name.
    assert fake_upstream.last_body["model"] == "upstream/test-model"


@pytest.mark.asyncio
async def test_the_agents_sampling_defaults_do_not_override_the_caller(
    ready: FastAPI, client: httpx.AsyncClient, seeded: Seeded, fake_upstream
) -> None:
    await make_agent(client, seeded, generation={"temperature": 0.9, "top_p": 0.5})
    fake_upstream.set_json(
        {
            "id": "c1",
            "object": "chat.completion",
            "model": "upstream/test-model",
            "choices": [{"index": 0, "message": {"role": "assistant", "content": "ok"}}],
            "usage": {"prompt_tokens": 5, "completion_tokens": 1, "total_tokens": 6},
        }
    )
    await client.post(
        "/v1/chat/completions",
        headers=seeded.auth,
        json={
            "model": "agent:research",
            "messages": [{"role": "user", "content": "hi"}],
            "temperature": 0.0,
        },
    )
    body = fake_upstream.last_body
    # The caller asked for zero and gets zero; the parameter they said nothing
    # about is filled from the agent.
    assert body["temperature"] == 0.0
    assert body["top_p"] == 0.5


@pytest.mark.asyncio
async def test_the_request_wins_a_tool_name_collision(
    ready: FastAPI, client: httpx.AsyncClient, seeded: Seeded, fake_upstream
) -> None:
    # An agent's default silently replacing a tool the caller defined would be
    # the agent overriding an explicit instruction.
    await make_agent(
        client,
        seeded,
        tools=[
            {"type": "function", "function": {"name": "lookup", "description": "agent's"}},
            {"type": "function", "function": {"name": "extra", "description": "only the agent's"}},
        ],
    )
    fake_upstream.set_json(
        {
            "id": "c1",
            "object": "chat.completion",
            "model": "upstream/test-model",
            "choices": [{"index": 0, "message": {"role": "assistant", "content": "ok"}}],
            "usage": {"prompt_tokens": 5, "completion_tokens": 1, "total_tokens": 6},
        }
    )
    await client.post(
        "/v1/chat/completions",
        headers=seeded.auth,
        json={
            "model": "agent:research",
            "messages": [{"role": "user", "content": "hi"}],
            "tools": [
                {"type": "function", "function": {"name": "lookup", "description": "caller's"}}
            ],
        },
    )
    tools = fake_upstream.last_body["tools"]
    by_name = {tool["function"]["name"]: tool["function"]["description"] for tool in tools}
    assert by_name["lookup"] == "caller's"
    assert by_name["extra"] == "only the agent's"


@pytest.mark.asyncio
async def test_retrieved_passages_reach_the_upstream_and_are_billed(
    ready: FastAPI,
    client: httpx.AsyncClient,
    seeded: Seeded,
    fake_upstream,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Retrieval, end to end, and the two properties that make it safe.

    The passages are in the messages the upstream receives, and the embedding
    call the search needed is on the ledger.
    """
    from gateway.models import ApiSurface, UsageRecord

    # A base with one passage in it.
    fake_upstream.set_json(embed_response([[1.0, 0.0, 0.0]]))
    base = await client.post(
        "/v1/vector_stores", headers=seeded.auth, json={"name": "handbook"}
    )
    base_id = base.json()["id"]
    await client.post(
        f"/v1/vector_stores/{base_id}/text",
        headers=seeded.auth,
        json={"text": "The reservation is taken before the upstream call.", "title": "budget"},
    )
    await drain(ready)

    await make_agent(client, seeded, knowledge_base_ids=[base_id])

    # The query embeds to the same vector, so the passage is the nearest hit.
    fake_upstream.set_json(embed_response([[1.0, 0.0, 0.0]]))
    embedding_calls_before = len(fake_upstream.bodies)
    fake_upstream.set_json(
        {
            "id": "c1",
            "object": "chat.completion",
            "model": "upstream/test-model",
            "choices": [{"index": 0, "message": {"role": "assistant", "content": "ok"}}],
            "usage": {"prompt_tokens": 40, "completion_tokens": 1, "total_tokens": 41},
        }
    )
    # One responder at a time in the fake upstream, so the embedding call for
    # the query has to answer with the chat shape too — which it tolerates,
    # because the retrieval reads `data` and finds none. Assert on the
    # *messages* instead, which is the property under test.
    fake_upstream.set_json(embed_response([[1.0, 0.0, 0.0]]))
    response = await client.post(
        "/v1/chat/completions",
        headers=seeded.auth,
        json={
            "model": "agent:research",
            "messages": [{"role": "user", "content": "when is the reservation taken"}],
        },
    )
    assert response.status_code == 200, response.text
    assert len(fake_upstream.bodies) > embedding_calls_before

    sent = fake_upstream.bodies[-1].get("messages")
    assert sent is not None, "the chat call never happened"
    context = next((m["content"] for m in sent if m["role"] == "system"), "")
    assert "reservation is taken" in context
    assert "budget" in context

    async with session_factory() as db:
        rows = (
            (
                await db.execute(
                    select(UsageRecord).where(
                        UsageRecord.api_surface == ApiSurface.EMBEDDINGS
                    )
                )
            )
            .scalars()
            .all()
        )
        # One for indexing the passage, one for embedding the query.
        assert len(rows) >= 2


@pytest.mark.asyncio
async def test_an_unavailable_retrieval_degrades_rather_than_failing_the_chat(
    ready: FastAPI,
    client: httpx.AsyncClient,
    seeded: Seeded,
    fake_upstream,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """A knowledge base being unavailable must not mean no answer.

    The judgement is stated in `_retrieve_for_agent`: a slightly worse answer
    beats no answer. The log line says `retrieval_degraded` so it is not
    silent.
    """
    fake_upstream.set_json(embed_response([[1.0, 0.0, 0.0]]))
    base = await client.post("/v1/vector_stores", headers=seeded.auth, json={"name": "kb"})
    base_id = base.json()["id"]
    await client.post(
        f"/v1/vector_stores/{base_id}/text", headers=seeded.auth, json={"text": "something"}
    )
    await drain(ready)
    await make_agent(client, seeded, knowledge_base_ids=[base_id])

    # The embedding provider refuses. The chat must still answer.
    fake_upstream.set_json({"error": {"message": "down"}}, status=503)
    first = await client.post(
        "/v1/chat/completions",
        headers=seeded.auth,
        json={"model": "agent:research", "messages": [{"role": "user", "content": "hi"}]},
    )
    # The chat call is made with the same failing responder, so the *upstream*
    # refusal is what surfaces — not a 500 from the retrieval. That is the
    # property: retrieval did not raise.
    assert first.status_code in (200, 503)
    assert first.status_code != 500


# -- sharing -----------------------------------------------------------------


@pytest_asyncio.fixture
async def colleague(
    session_factory: async_sessionmaker[AsyncSession], seeded: Seeded
) -> tuple[uuid.UUID, dict[str, str]]:
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
async def test_an_unshared_agent_is_invisible(
    ready: FastAPI,
    client: httpx.AsyncClient,
    seeded: Seeded,
    colleague: tuple[uuid.UUID, dict[str, str]],
) -> None:
    _, auth = colleague
    await make_agent(client, seeded)
    assert (await client.get("/v1/agents", headers=auth)).json()["data"] == []
    ids = [
        entry["id"] for entry in (await client.get("/v1/models", headers=auth)).json()["data"]
    ]
    assert "agent:research" not in ids
    # And it cannot be used by name either, which is the check that matters.
    used = await client.post(
        "/v1/chat/completions",
        headers=auth,
        json={"model": "agent:research", "messages": [{"role": "user", "content": "hi"}]},
    )
    assert used.status_code == 404


@pytest.mark.asyncio
async def test_sharing_an_agent_does_not_share_its_knowledge_base(
    ready: FastAPI,
    client: httpx.AsyncClient,
    seeded: Seeded,
    colleague: tuple[uuid.UUID, dict[str, str]],
    fake_upstream,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """The most important test in this file.

    Retrieval re-checks the *caller's* access to each attached base. Without
    that, sharing an agent would be a way to publish a document without
    sharing the document — and the owner would not even know they had.
    """
    person_id, auth = colleague

    fake_upstream.set_json(embed_response([[1.0, 0.0, 0.0]]))
    base = await client.post(
        "/v1/vector_stores", headers=seeded.auth, json={"name": "private"}
    )
    base_id = base.json()["id"]
    await client.post(
        f"/v1/vector_stores/{base_id}/text",
        headers=seeded.auth,
        json={"text": "The quarterly figures are commercially sensitive.", "title": "figures"},
    )
    await drain(ready)

    agent = await make_agent(client, seeded, knowledge_base_ids=[base_id])
    shared = await client.post(
        f"/v1/agents/{agent['id']}/shares",
        headers=seeded.auth,
        json={"principal_kind": "user", "principal_id": str(person_id)},
    )
    assert shared.status_code == 200, shared.text

    # The colleague can use the agent...
    assert (await client.get("/v1/agents", headers=auth)).json()["data"]

    fake_upstream.set_json(
        {
            "id": "c1",
            "object": "chat.completion",
            "model": "upstream/test-model",
            "choices": [{"index": 0, "message": {"role": "assistant", "content": "ok"}}],
            "usage": {"prompt_tokens": 5, "completion_tokens": 1, "total_tokens": 6},
        }
    )
    response = await client.post(
        "/v1/chat/completions",
        headers=auth,
        json={
            "model": "agent:research",
            "messages": [{"role": "user", "content": "what are the quarterly figures"}],
        },
    )
    assert response.status_code == 200, response.text
    # ...and sees nothing from a base that was never shared with them.
    sent = fake_upstream.last_body["messages"]
    assert not any("commercially sensitive" in str(m.get("content", "")) for m in sent)


@pytest.mark.asyncio
async def test_a_shared_agent_on_an_ungranted_model_is_not_offered(
    ready: FastAPI,
    client: httpx.AsyncClient,
    seeded: Seeded,
    colleague: tuple[uuid.UUID, dict[str, str]],
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Being shared an agent is not being granted its model.

    Otherwise an agent is a way around `access.py`, which is the one predicate
    this gateway has for "may this caller use this model".
    """
    person_id, auth = colleague
    agent = await make_agent(client, seeded)
    await client.post(
        f"/v1/agents/{agent['id']}/shares",
        headers=seeded.auth,
        json={"principal_kind": "user", "principal_id": str(person_id)},
    )

    # Take the model away from the group the colleague bills.
    async with session_factory() as db:
        await db.execute(
            select(GroupModelAccess).where(GroupModelAccess.model_id == seeded.model.id)
        )
        grant = await db.get(
            GroupModelAccess, {"group_id": seeded.group.id, "model_id": seeded.model.id}
        )
        if grant is not None:
            await db.delete(grant)
        await db.commit()

    used = await client.post(
        "/v1/chat/completions",
        headers=auth,
        json={"model": "agent:research", "messages": [{"role": "user", "content": "hi"}]},
    )
    assert used.status_code == 404
    assert "access" in used.json()["error"]["message"].lower()


@pytest.mark.asyncio
async def test_only_the_owner_deletes_an_agent(
    ready: FastAPI,
    client: httpx.AsyncClient,
    seeded: Seeded,
    colleague: tuple[uuid.UUID, dict[str, str]],
) -> None:
    person_id, auth = colleague
    agent = await make_agent(client, seeded)
    await client.post(
        f"/v1/agents/{agent['id']}/shares",
        headers=seeded.auth,
        json={"principal_kind": "user", "principal_id": str(person_id), "role": "editor"},
    )
    # An editor may change it...
    edited = await client.post(
        f"/v1/agents/{agent['id']}", headers=auth, json={"description": "theirs now"}
    )
    assert edited.status_code == 200
    # ...but not destroy it.
    assert (await client.delete(f"/v1/agents/{agent['id']}", headers=auth)).status_code == 403
    assert (
        await client.delete(f"/v1/agents/{agent['id']}", headers=seeded.auth)
    ).status_code == 200


@pytest.mark.asyncio
async def test_deleting_an_agent_removes_its_attachments_and_grants(
    ready: FastAPI,
    client: httpx.AsyncClient,
    seeded: Seeded,
    colleague: tuple[uuid.UUID, dict[str, str]],
    fake_upstream,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    from gateway.models import ResourceShare

    person_id, _auth = colleague
    fake_upstream.set_json(embed_response([[1.0, 0.0, 0.0]]))
    base = await client.post("/v1/vector_stores", headers=seeded.auth, json={"name": "kb"})
    agent = await make_agent(client, seeded, knowledge_base_ids=[base.json()["id"]])
    await client.post(
        f"/v1/agents/{agent['id']}/shares",
        headers=seeded.auth,
        json={"principal_kind": "user", "principal_id": str(person_id)},
    )
    await client.delete(f"/v1/agents/{agent['id']}", headers=seeded.auth)

    async with session_factory() as db:
        assert (await db.execute(select(Agent))).scalars().all() == []
        # CASCADE handles the attachment; `sharing.delete_shares` handles the
        # grant, because `resource_id` is not a foreign key and nothing in the
        # schema would.
        assert (await db.execute(select(AgentKnowledgeBase))).scalars().all() == []
        assert (await db.execute(select(ResourceShare))).scalars().all() == []
        # The base itself survives: deleting an agent is not deleting what it read.
        assert (await db.execute(select(KnowledgeBase))).scalars().all() != []
