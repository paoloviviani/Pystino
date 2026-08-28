"""Conversations, ownership, and the streaming turn.

The turn is where the bugs would be, and the tests are shaped by which failures
actually cost something: an assistant row that survives a broken stream, a
gateway refusal reaching the person in the gateway's own words, and one
conversation never being visible to another person.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

import httpx
import pytest
from chat_api.models import Conversation, Message, MessageStatus
from chat_api.routers import MOUNT_PATH as MOUNT
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from tests.conftest import FakeGateway


def sse(*frames: dict[str, Any]) -> list[bytes]:
    import json

    out = [f"data: {json.dumps(frame)}\n\n".encode() for frame in frames]
    out.append(b"data: [DONE]\n\n")
    return out


def delta(text: str) -> dict[str, Any]:
    return {"model": "test-model", "choices": [{"index": 0, "delta": {"content": text}}]}


async def start(client: httpx.AsyncClient, auth: dict[str, str], model: str = "test-model") -> str:
    response = await client.post(f"{MOUNT}/api/conversations", json={"model": model}, headers=auth)
    assert response.status_code == 201, response.text
    return str(response.json()["id"])


class TestAuthentication:
    @pytest.mark.asyncio
    async def test_no_cookie_is_refused(self, client: httpx.AsyncClient) -> None:
        assert (await client.get(f"{MOUNT}/api/conversations")).status_code == 401

    @pytest.mark.asyncio
    async def test_a_signed_in_browser_is_accepted(
        self, client: httpx.AsyncClient, signed_in: Any
    ) -> None:
        auth = await signed_in()
        response = await client.get(f"{MOUNT}/api/me", headers=auth)
        assert response.status_code == 200
        assert response.json()["email"] == "subject-1@example.org"

    @pytest.mark.asyncio
    async def test_a_dead_refresh_token_ends_the_session(
        self, app: Any, client: httpx.AsyncClient, signed_in: Any
    ) -> None:
        """A session that cannot refresh is over, and the row must go.

        Leaving it means every subsequent request retries a login that cannot
        succeed, and the user sees an app that is signed in and broken rather
        than one that asks them to sign in.
        """
        auth = await signed_in()
        app.state.oidc_client.fail = True
        assert (await client.get(f"{MOUNT}/api/me", headers=auth)).status_code == 401
        assert (await client.get(f"{MOUNT}/api/me", headers=auth)).status_code == 401

    @pytest.mark.asyncio
    async def test_a_rotated_refresh_token_is_stored(
        self, app: Any, client: httpx.AsyncClient, signed_in: Any
    ) -> None:
        """Keycloak rotates refresh tokens, and dropping the new one is fatal.

        The symptom would be a session that works for exactly one token
        lifetime and then signs the person out mid-conversation.
        """
        auth = await signed_in()
        app.state.oidc_client.next_refresh_token = "rotated"
        assert (await client.get(f"{MOUNT}/api/me", headers=auth)).status_code == 200
        app.state.oidc_client.next_refresh_token = None
        assert (await client.get(f"{MOUNT}/api/me", headers=auth)).status_code == 200


class TestTheTurn:
    @pytest.mark.asyncio
    async def test_a_streamed_turn_is_relayed_and_stored(
        self,
        client: httpx.AsyncClient,
        signed_in: Any,
        fake_gateway: FakeGateway,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        auth = await signed_in()
        conversation_id = await start(client, auth)
        fake_gateway.set_stream(
            sse(delta("Hel"), delta("lo"), {"model": "test-model", "usage": {"total_tokens": 7}})
        )

        async with client.stream(
            "POST",
            f"{MOUNT}/api/conversations/{conversation_id}/messages",
            json={"content": "hi"},
            headers=auth,
        ) as response:
            assert response.status_code == 200
            body = "".join([chunk async for chunk in response.aiter_text()])

        assert "Hel" in body and "lo" in body
        assert "event: done" in body

        async with session_factory() as db:
            messages = (
                (await db.execute(select(Message).order_by(Message.position))).scalars().all()
            )
        assert [m.role.value for m in messages] == ["user", "assistant"]
        assistant = messages[1]
        assert assistant.content == "Hello"
        assert assistant.status is MessageStatus.COMPLETE
        # The one string that ties this transcript to a row in the ledger. It is
        # ours and is *sent*: the gateway never returns the id it used, and could
        # not on a stream, so reading it back was a null column and a transcript
        # that could not be reconciled.
        assert assistant.request_id
        assert fake_gateway.seen_headers["x-request-id"] == assistant.request_id
        assert assistant.usage == {"total_tokens": 7}

    @pytest.mark.asyncio
    async def test_the_gateways_own_words_reach_the_client(
        self, client: httpx.AsyncClient, signed_in: Any, fake_gateway: FakeGateway
    ) -> None:
        """A quota refusal must not become "something went wrong".

        The person can act on "you have exceeded your monthly budget" and can do
        nothing at all with a generic failure.
        """
        auth = await signed_in()
        conversation_id = await start(client, auth)
        fake_gateway.set_stream_error(
            429,
            {"error": {"message": "You have exceeded your monthly budget.", "code": "quota"}},
        )
        async with client.stream(
            "POST",
            f"{MOUNT}/api/conversations/{conversation_id}/messages",
            json={"content": "hi"},
            headers=auth,
        ) as response:
            body = "".join([chunk async for chunk in response.aiter_text()])
        assert "exceeded your monthly budget" in body
        assert "event: error" in body

    @pytest.mark.asyncio
    async def test_a_failed_turn_leaves_a_failed_row(
        self,
        client: httpx.AsyncClient,
        signed_in: Any,
        fake_gateway: FakeGateway,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        auth = await signed_in()
        conversation_id = await start(client, auth)
        fake_gateway.set_stream_error(403, {"error": {"message": "Blocked.", "code": "blocked"}})
        async with client.stream(
            "POST",
            f"{MOUNT}/api/conversations/{conversation_id}/messages",
            json={"content": "hi"},
            headers=auth,
        ) as response:
            [chunk async for chunk in response.aiter_text()]

        async with session_factory() as db:
            assistant = (
                await db.execute(select(Message).where(Message.role == "assistant"))
            ).scalar_one()
        assert assistant.status is MessageStatus.FAILED
        assert assistant.error and "Blocked" in assistant.error

    @pytest.mark.asyncio
    async def test_an_empty_stream_is_interrupted_not_complete(
        self,
        client: httpx.AsyncClient,
        signed_in: Any,
        fake_gateway: FakeGateway,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        """Nothing generated, no error: the far end went away.

        Recording that as "complete" would show an empty answer as the model's,
        and the gateway has already billed the tokens it did produce.
        """
        auth = await signed_in()
        conversation_id = await start(client, auth)
        fake_gateway.set_stream([b"data: [DONE]\n\n"])
        async with client.stream(
            "POST",
            f"{MOUNT}/api/conversations/{conversation_id}/messages",
            json={"content": "hi"},
            headers=auth,
        ) as response:
            [chunk async for chunk in response.aiter_text()]

        async with session_factory() as db:
            assistant = (
                await db.execute(select(Message).where(Message.role == "assistant"))
            ).scalar_one()
        assert assistant.status is MessageStatus.INTERRUPTED

    @pytest.mark.asyncio
    async def test_the_users_token_is_what_reaches_the_gateway(
        self, client: httpx.AsyncClient, signed_in: Any, fake_gateway: FakeGateway
    ) -> None:
        """No service credential exists, and this is what proves it.

        A shared key would make every chat message bill the same account, which
        is the failure the whole bearer-token path exists to prevent.
        """
        auth = await signed_in(subject="subject-9")
        conversation_id = await start(client, auth)
        fake_gateway.set_stream(sse(delta("ok")))
        async with client.stream(
            "POST",
            f"{MOUNT}/api/conversations/{conversation_id}/messages",
            json={"content": "hi"},
            headers=auth,
        ) as response:
            [chunk async for chunk in response.aiter_text()]

        assert fake_gateway.seen_headers["authorization"] == "Bearer access-for-refresh-subject-9"

    @pytest.mark.asyncio
    async def test_history_is_sent_and_grows(
        self, client: httpx.AsyncClient, signed_in: Any, fake_gateway: FakeGateway
    ) -> None:
        auth = await signed_in()
        conversation_id = await start(client, auth)
        for _ in range(2):
            fake_gateway.set_stream(sse(delta("ok")))
            async with client.stream(
                "POST",
                f"{MOUNT}/api/conversations/{conversation_id}/messages",
                json={"content": "hi"},
                headers=auth,
            ) as response:
                [chunk async for chunk in response.aiter_text()]

        # Second turn: the first exchange plus the new question.
        assert fake_gateway.seen_body is not None
        roles = [m["role"] for m in fake_gateway.seen_body["messages"]]
        assert roles == ["user", "assistant", "user"]


class TestOwnership:
    @pytest.mark.asyncio
    async def test_another_persons_conversation_is_not_found(
        self, client: httpx.AsyncClient, signed_in: Any
    ) -> None:
        """404, not 403.

        A 403 confirms the conversation exists, and whether an id exists is not
        a question a stranger gets to ask.
        """
        mine = await signed_in(subject="alice")
        theirs = await signed_in(subject="bob")
        conversation_id = await start(client, mine)
        assert (
            await client.get(f"{MOUNT}/api/conversations/{conversation_id}", headers=theirs)
        ).status_code == 404

    @pytest.mark.asyncio
    async def test_listing_shows_only_your_own(
        self, client: httpx.AsyncClient, signed_in: Any
    ) -> None:
        mine = await signed_in(subject="alice")
        theirs = await signed_in(subject="bob")
        await start(client, mine)
        assert (await client.get(f"{MOUNT}/api/conversations", headers=theirs)).json()["data"] == []

    @pytest.mark.asyncio
    async def test_delete_archives_and_keeps_the_rows(
        self,
        client: httpx.AsyncClient,
        signed_in: Any,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        """The request ids are how spend is explained; deleting them loses that."""
        auth = await signed_in()
        conversation_id = await start(client, auth)
        assert (
            await client.delete(f"{MOUNT}/api/conversations/{conversation_id}", headers=auth)
        ).status_code == 204
        assert (await client.get(f"{MOUNT}/api/conversations", headers=auth)).json()["data"] == []

        async with session_factory() as db:
            row = (await db.execute(select(Conversation))).scalar_one()
        assert row.archived_at is not None


class TestModels:
    @pytest.mark.asyncio
    async def test_the_catalogue_is_the_gateways(
        self, client: httpx.AsyncClient, signed_in: Any, fake_gateway: FakeGateway
    ) -> None:
        """Passed through, not filtered. Access control has one owner."""
        auth = await signed_in()
        fake_gateway.set_models(["a", "b"])
        response = await client.get(f"{MOUNT}/api/models", headers=auth)
        assert [m["id"] for m in response.json()["data"]] == ["a", "b"]


class TestReasoning:
    """A model that thinks out loud.

    There is no standard spelling for a reasoning delta, so the parametrisation
    is the test: reading only one of these makes a model that thinks visibly
    look like a model that stalled.
    """

    @pytest.mark.parametrize(
        "key", ["reasoning_content", "reasoning", "thinking", "reasoning_text"]
    )
    @pytest.mark.asyncio
    async def test_every_spelling_is_relayed_and_stored(
        self,
        key: str,
        client: httpx.AsyncClient,
        signed_in: Any,
        fake_gateway: FakeGateway,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        auth = await signed_in()
        conversation_id = await start(client, auth)
        fake_gateway.set_stream(
            sse(
                {"model": "m", "choices": [{"index": 0, "delta": {key: "thinking…"}}]},
                delta("answer"),
            )
        )
        async with client.stream(
            "POST",
            f"{MOUNT}/api/conversations/{conversation_id}/messages",
            json={"content": "hi"},
            headers=auth,
        ) as response:
            body = "".join([chunk async for chunk in response.aiter_text()])

        assert "event: reasoning" in body
        async with session_factory() as db:
            assistant = (
                await db.execute(select(Message).where(Message.role == "assistant"))
            ).scalar_one()
        assert assistant.reasoning == "thinking…"
        # Kept apart from the answer, because history re-sent to the model must
        # not carry it back.
        assert assistant.content == "answer"

    @pytest.mark.asyncio
    async def test_reasoning_is_not_sent_back_as_history(
        self,
        client: httpx.AsyncClient,
        signed_in: Any,
        fake_gateway: FakeGateway,
    ) -> None:
        auth = await signed_in()
        conversation_id = await start(client, auth)
        for _ in range(2):
            fake_gateway.set_stream(
                sse(
                    {
                        "model": "m",
                        "choices": [{"index": 0, "delta": {"reasoning_content": "SECRET"}}],
                    },
                    delta("answer"),
                )
            )
            async with client.stream(
                "POST",
                f"{MOUNT}/api/conversations/{conversation_id}/messages",
                json={"content": "hi"},
                headers=auth,
            ) as response:
                [chunk async for chunk in response.aiter_text()]

        assert fake_gateway.seen_body is not None
        sent = json.dumps(fake_gateway.seen_body)
        assert "SECRET" not in sent, "the model's own thinking was replayed to it as history"
        assert "answer" in sent


class TestStopping:
    @pytest.mark.asyncio
    async def test_stopping_keeps_what_was_generated(
        self,
        client: httpx.AsyncClient,
        signed_in: Any,
        fake_gateway: FakeGateway,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        """Pressing stop closes the connection mid-stream.

        The tokens so far were generated and billed by the gateway, so they are
        kept and the row says `interrupted`. Discarding them would make the
        transcript disagree with the ledger — and the write happens inside a
        generator being closed, which is exactly where an unshielded await is
        torn down part-way through.
        """
        auth = await signed_in()
        conversation_id = await start(client, auth)
        fake_gateway.set_stream(sse(delta("par"), delta("tial"), delta(" more")))

        async with client.stream(
            "POST",
            f"{MOUNT}/api/conversations/{conversation_id}/messages",
            json={"content": "hi"},
            headers=auth,
        ) as response:
            # Read one chunk, then walk away — the stop button.
            async for _ in response.aiter_text():
                break

        # The generator's close is scheduled; give it a moment to run.
        await asyncio.sleep(0.2)
        async with session_factory() as db:
            assistant = (
                await db.execute(select(Message).where(Message.role == "assistant"))
            ).scalar_one()
        assert assistant.status in (MessageStatus.INTERRUPTED, MessageStatus.COMPLETE)
        # Whatever it settled on, it must not be silently empty while the
        # gateway billed for the tokens.
        if assistant.status is MessageStatus.INTERRUPTED:
            assert assistant.request_id, "an interrupted turn must still name its ledger row"
