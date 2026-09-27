"""``POST /v1/session/announce`` and ``GET /v1/me/identities`` (ADR 0093 §4.1).

Announce is the chat's own sign-in door: unlike the plain `/v1` bearer path,
it runs the full sign-in sequence and is refused, not merely unauthenticated,
for a token that was not minted for the chat client — the whole reason it
exists is that a login should provision, link and bootstrap, and no other
`/v1` credential is trusted to trigger any of that on this caller's say-so.
"""

from __future__ import annotations

import uuid
from datetime import timedelta
from typing import Any

from conftest import BEARER_ISSUER, Seeded, make_token
from conftest import bearer_auth as auth
from fastapi import FastAPI
from gateway.models import ApiKey, User, UserIdentity
from gateway.security import generate_api_key
from gateway.types import utcnow
from joserfc.jwk import RSAKey
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker


def _make_chat_aware(bearer_app: FastAPI, *, chat_client_id: str = "llm-chat") -> None:
    """`bearer_app` sets everything announce needs except which accepted
    client is the chat — added here rather than in the shared fixture, since
    every other bearer test deliberately has no opinion about it."""
    bearer_app.state.settings.oidc.chat_client_id = chat_client_id


class TestAnnounce:
    async def test_answers_the_signed_in_shape(
        self,
        bearer_app: FastAPI,
        client: Any,
        seeded: Seeded,
        signing_key: RSAKey,
    ) -> None:
        _make_chat_aware(bearer_app)
        response = await client.post(
            "/v1/session/announce", headers=auth(make_token(signing_key))
        )
        assert response.status_code == 200, response.text
        body = response.json()
        assert body == {
            "id": str(seeded.user.id),
            "identities": [{"issuer": BEARER_ISSUER, "subject": "subject-1"}],
            "merged_from": [],
            "is_active": True,
            "is_admin": False,
            "sessions_valid_after": None,
            "merged_at": None,
        }

    async def test_a_token_not_issued_to_the_chat_client_is_refused(
        self,
        bearer_app: FastAPI,
        client: Any,
        seeded: Seeded,
        signing_key: RSAKey,
    ) -> None:
        _make_chat_aware(bearer_app, chat_client_id="llm-chat")
        response = await client.post(
            "/v1/session/announce",
            headers=auth(make_token(signing_key, azp="pystino-console")),
        )
        assert response.status_code == 401, response.text

    async def test_an_invalid_token_is_refused(
        self, bearer_app: FastAPI, client: Any
    ) -> None:
        _make_chat_aware(bearer_app)
        response = await client.post(
            "/v1/session/announce", headers={"authorization": "Bearer not-a-jwt"}
        )
        assert response.status_code == 401, response.text

    async def test_no_credential_is_refused(self, bearer_app: FastAPI, client: Any) -> None:
        _make_chat_aware(bearer_app)
        response = await client.post("/v1/session/announce")
        assert response.status_code == 401, response.text

    async def test_a_disabled_account_gets_403(
        self,
        bearer_app: FastAPI,
        client: Any,
        seeded: Seeded,
        signing_key: RSAKey,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        _make_chat_aware(bearer_app)
        async with session_factory() as db:
            row = await db.get(User, seeded.user.id)
            assert row is not None
            row.is_active = False
            await db.commit()

        response = await client.post(
            "/v1/session/announce", headers=auth(make_token(signing_key))
        )
        assert response.status_code == 403, response.text

    async def test_runs_the_full_sign_in_sequence_not_the_plain_v1_path(
        self,
        bearer_app: FastAPI,
        client: Any,
        signing_key: RSAKey,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        """A brand-new identity, announced rather than sent through `/v1`
        directly: this must provision exactly like the console callback
        would, groups included — the property that makes it a door."""
        _make_chat_aware(bearer_app)
        token = make_token(
            signing_key, sub="a-new-subject", email="fresh@example.org", groups=["research"]
        )
        response = await client.post("/v1/session/announce", headers=auth(token))
        assert response.status_code == 200, response.text
        body = response.json()

        async with session_factory() as db:
            user = await db.get(User, uuid.UUID(body["id"]))
            assert user is not None
            assert user.email == "fresh@example.org"
            assert {m.group.name for m in user.memberships} == {"research"}

    async def test_the_tenth_call_is_fine_the_eleventh_is_rate_limited(
        self,
        bearer_app: FastAPI,
        client: Any,
        seeded: Seeded,
        signing_key: RSAKey,
    ) -> None:
        _make_chat_aware(bearer_app)
        headers = auth(make_token(signing_key))
        for _ in range(10):
            response = await client.post("/v1/session/announce", headers=headers)
            assert response.status_code == 200, response.text
        eleventh = await client.post("/v1/session/announce", headers=headers)
        assert eleventh.status_code == 429, eleventh.text

    async def test_userinfo_is_fetched_not_trusted_from_a_body(
        self,
        bearer_app: FastAPI,
        client: Any,
        seeded: Seeded,
        signing_key: RSAKey,
    ) -> None:
        """Announce takes no claims in its body at all — passing any is simply
        ignored, since the route reads nothing from the request but the
        bearer token."""
        _make_chat_aware(bearer_app)
        response = await client.post(
            "/v1/session/announce",
            headers=auth(make_token(signing_key)),
            json={"email": "attacker@example.org", "is_admin": True},
        )
        assert response.status_code == 200, response.text
        assert response.json()["is_admin"] is False


class TestMyIdentities:
    async def test_the_primary_pair_with_no_links_or_merges(
        self, bearer_app: FastAPI, client: Any, seeded: Seeded, signing_key: RSAKey
    ) -> None:
        response = await client.get(
            "/v1/me/identities", headers=auth(make_token(signing_key))
        )
        assert response.status_code == 200, response.text
        assert response.json() == {
            "id": str(seeded.user.id),
            "identities": [{"issuer": BEARER_ISSUER, "subject": "subject-1"}],
            "merged_from": [],
        }

    async def test_a_linked_identity_is_listed_alongside_the_primary(
        self,
        bearer_app: FastAPI,
        client: Any,
        seeded: Seeded,
        signing_key: RSAKey,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        async with session_factory() as db:
            db.add(
                UserIdentity(
                    user_id=seeded.user.id,
                    issuer="https://old-idp.test",
                    subject="old-subject",
                    matched_email="member@example.org",
                )
            )
            await db.commit()

        response = await client.get(
            "/v1/me/identities", headers=auth(make_token(signing_key))
        )
        assert response.status_code == 200, response.text
        pairs = {(i["issuer"], i["subject"]) for i in response.json()["identities"]}
        assert pairs == {
            (BEARER_ISSUER, "subject-1"),
            ("https://old-idp.test", "old-subject"),
        }

    async def test_it_needs_a_credential(self, client: Any) -> None:
        response = await client.get("/v1/me/identities")
        assert response.status_code == 401

    async def test_a_personal_api_key_is_refused(self, client: Any, seeded: Seeded) -> None:
        """A program's credential has no business listing its owner's
        identities (the same `credential` distinction `/v1/me` draws)."""
        response = await client.get("/v1/me/identities", headers=seeded.auth)
        assert response.status_code == 403, response.text

    async def test_a_minted_session_credential_answers_as_its_person(
        self,
        client: Any,
        seeded: Seeded,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        """The house IdP's own access tokens are `minted_by`-set rows in the
        same table, and count as the person, not as a program's key."""
        async with session_factory() as db:
            minted = generate_api_key(environment_prefix="gwa")
            db.add(
                ApiKey(
                    user_id=seeded.user.id,
                    prefix=minted.prefix,
                    key_hash=minted.key_hash,
                    name="idp:cerea",
                    minted_by="idp-cerea",
                    expires_at=utcnow() + timedelta(minutes=15),
                )
            )
            await db.commit()

        response = await client.get(
            "/v1/me/identities", headers={"authorization": f"Bearer {minted.secret}"}
        )
        assert response.status_code == 200, response.text
        assert response.json()["id"] == str(seeded.user.id)
