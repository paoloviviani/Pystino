"""``GET /admin/users/{id}/delete-preview`` and the rewritten ``DELETE
/admin/users/{id}`` (ADR 0093 §9.2, §9.3): what the gateway shows before a
delete, the ``confirm_shared_loss`` gate, and the two post-commit actions
(the bound Authelia login, the inline chat call) neither of which may ever
turn a completed delete into a failed request.
"""

from __future__ import annotations

import json
import uuid
from pathlib import Path
from typing import Any

import httpx
import yaml
from fastapi import FastAPI
from gateway.models import DirectoryEntry, IdentityProvider, User
from gateway.secrets import SecretBox
from pydantic import SecretStr
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from test_admin import as_user, make_admin

BOX = SecretBox(["test-encryption-key-not-for-production"])
BUNDLED_ISSUER = "https://gw.example.org/authelia"

SEED_TWO_USERS = """users:
  victim-login:
    disabled: false
    displayname: Victim
    password: '$argon2id$v=19$m=65536,t=3,p=4$c2FsdA$aGFzaA'
    email: victim@example.org
    groups: [users]
  someone-else:
    disabled: false
    displayname: Someone Else
    password: '$argon2id$v=19$m=65536,t=3,p=4$c2FsdA$aGFzaA'
    email: else@example.org
    groups: [users]
"""

SEED_ONE_USER = """users:
  victim-login:
    disabled: false
    displayname: Victim
    password: '$argon2id$v=19$m=65536,t=3,p=4$c2FsdA$aGFzaA'
    email: victim@example.org
    groups: [users]
"""


def _fake_chat(counts: dict[str, int] | None, *, unattributed: int = 0) -> httpx.AsyncClient:
    def handler(request: httpx.Request) -> httpx.Response:
        if counts is None:
            return httpx.Response(503)
        if request.url.path.endswith("/preview"):
            return httpx.Response(
                200, json={"counts": counts, "unattributed_legacy_shares": unattributed}
            )
        body = json.loads(request.read())
        return httpx.Response(
            200, json={"erasure_id": body["erasure_id"], "counts": counts}
        )

    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


async def _victim(session_factory: async_sessionmaker[AsyncSession]) -> uuid.UUID:
    async with session_factory() as session:
        victim = User(issuer="https://idp.test", subject="victim", email="victim@example.org")
        session.add(victim)
        await session.commit()
        return victim.id


class TestDeletePreview:
    async def test_unreachable_chat_reports_unavailable_not_an_error(
        self,
        app: FastAPI,
        client: Any,
        seeded: Any,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        admin = await make_admin(session_factory, seeded)
        as_user(app, admin)
        app.state.control_http = _fake_chat(None)
        app.state.settings.chat.erasure_url = "http://chat/internal/erasure"
        app.state.settings.chat.erasure_token = SecretStr("test-erasure-token")
        victim_id = await _victim(session_factory)

        response = await client.get(f"/api/admin/users/{victim_id}/delete-preview")
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["chat_reachable"] is False
        assert body["chat_counts"] is None
        assert body["shared_with_others"] is False

    async def test_shared_conversations_flip_shared_with_others(
        self,
        app: FastAPI,
        client: Any,
        seeded: Any,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        admin = await make_admin(session_factory, seeded)
        as_user(app, admin)
        app.state.control_http = _fake_chat({"conversations": 5, "sharedConversations": 2})
        app.state.settings.chat.erasure_url = "http://chat/internal/erasure"
        app.state.settings.chat.erasure_token = SecretStr("test-erasure-token")
        victim_id = await _victim(session_factory)

        response = await client.get(f"/api/admin/users/{victim_id}/delete-preview")
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["chat_reachable"] is True
        assert body["chat_counts"] == {"conversations": 5, "sharedConversations": 2}
        assert body["shared_with_others"] is True


class TestDeleteRefusals:
    async def test_no_chat_configured_refuses(
        self,
        app: FastAPI,
        client: Any,
        seeded: Any,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        admin = await make_admin(session_factory, seeded)
        as_user(app, admin)
        app.state.settings.chat.erasure_url = ""
        victim_id = await _victim(session_factory)

        response = await client.request(
            "DELETE", f"/api/admin/users/{victim_id}", json={"confirm_shared_loss": False}
        )
        assert response.status_code == 400, response.text
        assert "not reachable" in response.json()["error"]["message"]

    async def test_shared_content_without_the_tick_box_is_refused(
        self,
        app: FastAPI,
        client: Any,
        seeded: Any,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        admin = await make_admin(session_factory, seeded)
        as_user(app, admin)
        app.state.control_http = _fake_chat({"sharedConversations": 1})
        app.state.settings.chat.erasure_url = "http://chat/internal/erasure"
        app.state.settings.chat.erasure_token = SecretStr("test-erasure-token")
        victim_id = await _victim(session_factory)

        response = await client.request(
            "DELETE", f"/api/admin/users/{victim_id}", json={"confirm_shared_loss": False}
        )
        assert response.status_code == 400, response.text

        async with session_factory() as session:
            assert await session.get(User, victim_id) is not None, "refused: nothing deleted"

    async def test_shared_content_with_the_tick_box_proceeds(
        self,
        app: FastAPI,
        client: Any,
        seeded: Any,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        admin = await make_admin(session_factory, seeded)
        as_user(app, admin)
        app.state.control_http = _fake_chat({"sharedConversations": 1})
        app.state.settings.chat.erasure_url = "http://chat/internal/erasure"
        app.state.settings.chat.erasure_token = SecretStr("test-erasure-token")
        victim_id = await _victim(session_factory)

        response = await client.request(
            "DELETE", f"/api/admin/users/{victim_id}", json={"confirm_shared_loss": True}
        )
        assert response.status_code == 200, response.text


class TestDeleteSucceeds:
    async def test_the_inline_call_succeeding_marks_the_erasure_done(
        self,
        app: FastAPI,
        client: Any,
        seeded: Any,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        admin = await make_admin(session_factory, seeded)
        as_user(app, admin)
        app.state.control_http = _fake_chat({"conversations": 2})
        app.state.settings.chat.erasure_url = "http://chat/internal/erasure"
        app.state.settings.chat.erasure_token = SecretStr("test-erasure-token")
        victim_id = await _victim(session_factory)

        response = await client.request(
            "DELETE", f"/api/admin/users/{victim_id}", json={"confirm_shared_loss": False}
        )
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["chat_erasure_done"] is True

        async with session_factory() as session:
            assert await session.get(User, victim_id) is None

    async def test_the_inline_call_failing_still_returns_200_pending(
        self,
        app: FastAPI,
        client: Any,
        seeded: Any,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        admin = await make_admin(session_factory, seeded)
        as_user(app, admin)
        app.state.settings.chat.erasure_url = "http://chat/internal/erasure"
        app.state.settings.chat.erasure_token = SecretStr("test-erasure-token")
        # Reachable for the pre-delete preview call this test does not make,
        # but the inline post-commit attempt below uses this same client and
        # fails every request -- the point of the test.
        app.state.control_http = httpx.AsyncClient(
            transport=httpx.MockTransport(lambda r: httpx.Response(503))
        )
        victim_id = await _victim(session_factory)

        response = await client.request(
            "DELETE", f"/api/admin/users/{victim_id}", json={"confirm_shared_loss": False}
        )
        assert response.status_code == 200, response.text
        assert response.json()["chat_erasure_done"] is False

        async with session_factory() as session:
            # Deleted here regardless: the chat's own failure never rolls
            # back the gateway side (§9.3).
            assert await session.get(User, victim_id) is None


class TestBundledLoginRemoval:
    async def _provider(
        self, session_factory: async_sessionmaker[AsyncSession], path: Path
    ) -> IdentityProvider:
        async with session_factory() as session:
            row = IdentityProvider(
                name="authelia",
                issuer=BUNDLED_ISSUER,
                client_id="pystino-console",
                client_secret_encrypted=BOX.encrypt("s"),
                scopes=["openid"],
                kind="authelia",
                sync_config_encrypted=BOX.encrypt(json.dumps({"path": str(path)})),
            )
            session.add(row)
            await session.commit()
            await session.refresh(row)
            return row

    async def test_the_login_is_removed_when_others_remain(
        self,
        app: FastAPI,
        client: Any,
        seeded: Any,
        session_factory: async_sessionmaker[AsyncSession],
        tmp_path: Path,
    ) -> None:
        path = tmp_path / "users_database.yml"
        path.write_text(SEED_TWO_USERS)
        provider = await self._provider(session_factory, path)

        admin = await make_admin(session_factory, seeded)
        as_user(app, admin)
        app.state.control_http = _fake_chat({"conversations": 0})
        app.state.settings.chat.erasure_url = "http://chat/internal/erasure"
        app.state.settings.chat.erasure_token = SecretStr("test-erasure-token")

        async with session_factory() as session:
            victim = User(issuer=BUNDLED_ISSUER, subject="victim-sub", email="victim@example.org")
            session.add(victim)
            await session.flush()
            session.add(
                DirectoryEntry(
                    provider_id=provider.id,
                    external_id="victim-login",
                    username="victim-login",
                    user_id=victim.id,
                )
            )
            await session.commit()
            victim_id = victim.id

        response = await client.request(
            "DELETE", f"/api/admin/users/{victim_id}", json={"confirm_shared_loss": False}
        )
        assert response.status_code == 200, response.text

        data = yaml.safe_load(path.read_text())["users"]
        assert "victim-login" not in data
        assert "someone-else" in data

    async def test_the_last_login_is_disabled_not_removed(
        self,
        app: FastAPI,
        client: Any,
        seeded: Any,
        session_factory: async_sessionmaker[AsyncSession],
        tmp_path: Path,
    ) -> None:
        path = tmp_path / "users_database.yml"
        path.write_text(SEED_ONE_USER)
        provider = await self._provider(session_factory, path)

        admin = await make_admin(session_factory, seeded)
        as_user(app, admin)
        app.state.control_http = _fake_chat({"conversations": 0})
        app.state.settings.chat.erasure_url = "http://chat/internal/erasure"
        app.state.settings.chat.erasure_token = SecretStr("test-erasure-token")

        async with session_factory() as session:
            victim = User(issuer=BUNDLED_ISSUER, subject="victim-sub", email="victim@example.org")
            session.add(victim)
            await session.flush()
            session.add(
                DirectoryEntry(
                    provider_id=provider.id,
                    external_id="victim-login",
                    username="victim-login",
                    user_id=victim.id,
                )
            )
            await session.commit()
            victim_id = victim.id

        response = await client.request(
            "DELETE", f"/api/admin/users/{victim_id}", json={"confirm_shared_loss": False}
        )
        assert response.status_code == 200, response.text

        data = yaml.safe_load(path.read_text())["users"]
        assert data["victim-login"]["disabled"] is True


class TestPendingErasures:
    async def test_zero_when_nothing_is_queued(
        self,
        app: FastAPI,
        client: Any,
        seeded: Any,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        admin = await make_admin(session_factory, seeded)
        as_user(app, admin)
        response = await client.get("/api/admin/erasures/pending")
        assert response.status_code == 200, response.text
        assert response.json() == {"pending": 0}

    async def test_counts_only_pending_not_done(
        self,
        app: FastAPI,
        client: Any,
        seeded: Any,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        admin = await make_admin(session_factory, seeded)
        as_user(app, admin)
        app.state.control_http = _fake_chat({"conversations": 1})
        app.state.settings.chat.erasure_url = "http://chat/internal/erasure"
        app.state.settings.chat.erasure_token = SecretStr("test-erasure-token")

        # One that will succeed inline (done) and one whose chat call fails
        # (stays pending), so the count is a real filter, not just a row count.
        done_victim = await _victim(session_factory)
        await client.request(
            "DELETE", f"/api/admin/users/{done_victim}", json={"confirm_shared_loss": False}
        )

        app.state.control_http = httpx.AsyncClient(
            transport=httpx.MockTransport(lambda r: httpx.Response(503))
        )
        pending_victim = await _victim(session_factory)
        await client.request(
            "DELETE", f"/api/admin/users/{pending_victim}", json={"confirm_shared_loss": False}
        )

        response = await client.get("/api/admin/erasures/pending")
        assert response.status_code == 200, response.text
        assert response.json() == {"pending": 1}
