"""What /v1/models lists, and what stays off it.

The list is an OpenAI-compatible client's picture of the deployment, which is
why a search tier does not ride in it by default (ADR 0071): a tier answers
/v1/search, and a client that read one here would present it as a chat model
— the confusion the surface must not create. `?include=search` is the opt-in,
and grants still bound everything either way.
"""

from __future__ import annotations

from typing import Any

import pytest
from conftest import Seeded, make_token
from conftest import bearer_auth as auth
from fastapi import FastAPI
from gateway.models import GroupModelAccess, ModelKind, Provider, ProviderKind
from joserfc.jwk import RSAKey
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from test_admin import as_user, make_admin


async def _seed_search_tier(
    session_factory: async_sessionmaker[AsyncSession],
    seeded: Seeded,
    *,
    granted: bool,
) -> None:
    """A search backend with one tier, granted to the seeded group or not."""
    from gateway.secrets import SecretBox, hint_for

    async with session_factory() as session:
        key = "k"
        provider = Provider(
            name="linkup",
            base_url="https://api.linkup.so/v1",
            api_key_encrypted=SecretBox(["test-encryption-key-not-for-production"]).encrypt(key),
            api_key_hint=hint_for(key),
            plugin="linkup",
            kind=ProviderKind.SEARCH,
        )
        session.add(provider)
        await session.flush()
        model = ModelKind.SEARCH  # noqa: F841 — named for the reader
        from gateway.models import ModelDef

        tier = ModelDef(
            name="linkup-standard",
            upstream_model="standard",
            provider_id=provider.id,
            kind=ModelKind.SEARCH,
        )
        session.add(tier)
        await session.flush()
        if granted:
            session.add(GroupModelAccess(group_id=seeded.group.id, model_id=tier.id))
        await session.commit()


async def _seed_internal_ocr_model(
    session_factory: async_sessionmaker[AsyncSession],
    seeded: Seeded,
    *,
    kind: ModelKind = ModelKind.OCR,
    granted: bool = True,
) -> None:
    """An internal-provider model — the shape the local extractor seeds
    itself in, minus the migration — granted to the seeded group by default so
    a hidden-from-the-list assertion is about visibility, not access."""
    from gateway.models import ModelDef

    async with session_factory() as session:
        provider = Provider(
            name="extractor",
            base_url="http://extractor:8080",
            plugin="extractor",
            kind=ProviderKind.INTERNAL,
        )
        session.add(provider)
        await session.flush()
        model = ModelDef(
            name="markitdown",
            upstream_model="markitdown",
            provider_id=provider.id,
            kind=kind,
        )
        session.add(model)
        await session.flush()
        if granted:
            session.add(GroupModelAccess(group_id=seeded.group.id, model_id=model.id))
        await session.commit()


class TestUnauthenticatedListing:
    """ADR 0081: the list is public, a card's detail is not."""

    async def test_no_credential_gets_the_full_catalogue(self, client: Any, seeded: Seeded) -> None:
        """No group, no key at all — still 200, still the seeded model."""
        response = await client.get("/v1/models")
        assert response.status_code == 200, response.text
        names = [entry["id"] for entry in response.json()["data"]]
        assert names == ["test-model"]

    async def test_the_card_carries_no_price_or_grant_information(
        self, client: Any, seeded: Seeded
    ) -> None:
        response = await client.get("/v1/models")
        assert response.status_code == 200, response.text
        card = response.json()["data"][0]
        assert set(card) == {
            "id",
            "object",
            "created",
            "owned_by",
            "context_window",
            "max_output_tokens",
            "max_input_tokens",
            "display_name",
            "kind",
            "input_modalities",
            "output_modalities",
            "supported_features",
            "local",
        }

    async def test_a_caller_with_no_grant_sees_nothing_once_authenticated(
        self,
        client: Any,
        seeded: Seeded,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        """The narrowing is real: authenticated-with-no-grant is not the same
        request as no principal at all, and must not become the same response."""
        from gateway.models import ApiKey, Group, Membership, User
        from gateway.security import generate_api_key

        async with session_factory() as db:
            group = Group(name="ungranted", description="no grants on purpose")
            user = User(issuer="https://idp.test", subject="ungranted-lister")
            db.add_all([group, user])
            await db.flush()
            db.add(Membership(user_id=user.id, group_id=group.id))
            user.default_billing_group_id = group.id
            generated = generate_api_key()
            db.add(
                ApiKey(
                    user_id=user.id,
                    prefix=generated.prefix,
                    key_hash=generated.key_hash,
                    name="ungranted lister key",
                    billing_group_id=group.id,
                )
            )
            await db.commit()
            secret = generated.secret

        response = await client.get("/v1/models", headers={"authorization": f"Bearer {secret}"})
        assert response.status_code == 200, response.text
        assert response.json()["data"] == []

    async def test_an_invalid_credential_still_401s(self, client: Any, seeded: Seeded) -> None:
        """Presenting a bad key is not the same as presenting none."""
        response = await client.get(
            "/v1/models", headers={"authorization": "Bearer gwk_deadbeef_nope"}
        )
        assert response.status_code == 401
        assert response.json()["error"]["code"] == "invalid_api_key"

    async def test_the_authenticated_listing_is_still_grant_filtered(
        self,
        bearer_app: FastAPI,
        client: Any,
        seeded: Seeded,
        signing_key: RSAKey,
    ) -> None:
        """The exemption widens the anonymous case; it must not widen this one."""
        response = await client.get("/v1/models", headers=auth(make_token(signing_key)))
        assert response.status_code == 200, response.text
        names = [entry["id"] for entry in response.json()["data"]]
        assert names == ["test-model"]

    async def test_the_detail_route_stays_authenticated(self, client: Any, seeded: Seeded) -> None:
        response = await client.get("/v1/models/test-model")
        assert response.status_code == 401


class TestSearchStaysOffTheList:
    @pytest.mark.asyncio
    async def test_a_tier_does_not_ride_the_default_list(
        self,
        bearer_app: FastAPI,
        client: Any,
        seeded: Seeded,
        signing_key: RSAKey,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        """Granted or not, a tier is not a chat model and is not listed as one."""
        await _seed_search_tier(session_factory, seeded, granted=True)
        response = await client.get("/v1/models", headers=auth(make_token(signing_key)))
        assert response.status_code == 200, response.text
        names = [entry["id"] for entry in response.json()["data"]]
        assert "linkup-standard" not in names

    @pytest.mark.asyncio
    async def test_the_opt_in_lists_granted_tiers(
        self,
        bearer_app: FastAPI,
        client: Any,
        seeded: Seeded,
        signing_key: RSAKey,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        await _seed_search_tier(session_factory, seeded, granted=True)
        response = await client.get(
            "/v1/models?include=search", headers=auth(make_token(signing_key))
        )
        assert response.status_code == 200, response.text
        names = [entry["id"] for entry in response.json()["data"]]
        assert "linkup-standard" in names

    @pytest.mark.asyncio
    async def test_the_opt_in_is_still_bounded_by_grants(
        self,
        bearer_app: FastAPI,
        client: Any,
        seeded: Seeded,
        signing_key: RSAKey,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        """Asking by name is visibility, not access."""
        await _seed_search_tier(session_factory, seeded, granted=False)
        response = await client.get(
            "/v1/models?include=search", headers=auth(make_token(signing_key))
        )
        assert response.status_code == 200, response.text
        names = [entry["id"] for entry in response.json()["data"]]
        assert "linkup-standard" not in names

    @pytest.mark.asyncio
    async def test_the_admin_listing_shows_them_for_the_screen_that_owns_them(
        self,
        app: FastAPI,
        client: Any,
        seeded: Seeded,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        """The console's search screen reads the admin listing, which has no
        reason to hide anything: it is the screen that decides visibility."""
        as_user(app, await make_admin(session_factory, seeded))
        await _seed_search_tier(session_factory, seeded, granted=False)
        response = await client.get("/api/admin/models")
        assert response.status_code == 200, response.text
        names = [entry["name"] for entry in response.json()["items"]]
        assert "linkup-standard" in names


class TestInternalOcrListing:
    """The local extractor is plumbing (`ProviderKind.INTERNAL`): invisible by
    default, and offered — flagged — only to a caller that names OCR
    explicitly. Pystino a940516 hid it from every listing to keep it out of a
    chat picker; this is the opt-in a document-reader picker such as Cerea's
    Knowledge screen needs, without undoing that."""

    @pytest.mark.asyncio
    async def test_the_default_listing_still_hides_it(
        self,
        bearer_app: FastAPI,
        client: Any,
        seeded: Seeded,
        signing_key: RSAKey,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        await _seed_internal_ocr_model(session_factory, seeded)
        response = await client.get("/v1/models", headers=auth(make_token(signing_key)))
        assert response.status_code == 200, response.text
        names = [entry["id"] for entry in response.json()["data"]]
        assert "markitdown" not in names

    @pytest.mark.asyncio
    async def test_include_ocr_shows_it_flagged(
        self,
        bearer_app: FastAPI,
        client: Any,
        seeded: Seeded,
        signing_key: RSAKey,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        await _seed_internal_ocr_model(session_factory, seeded)
        response = await client.get("/v1/models?include=ocr", headers=auth(make_token(signing_key)))
        assert response.status_code == 200, response.text
        by_id = {entry["id"]: entry for entry in response.json()["data"]}
        assert by_id["markitdown"]["local"] is True

    @pytest.mark.asyncio
    async def test_include_ocr_is_still_bounded_by_grants(
        self,
        bearer_app: FastAPI,
        client: Any,
        seeded: Seeded,
        signing_key: RSAKey,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        """Asking by kind is visibility, not access — the same rule the search
        opt-in follows."""
        await _seed_internal_ocr_model(session_factory, seeded, granted=False)
        response = await client.get("/v1/models?include=ocr", headers=auth(make_token(signing_key)))
        assert response.status_code == 200, response.text
        names = [entry["id"] for entry in response.json()["data"]]
        assert "markitdown" not in names

    @pytest.mark.asyncio
    async def test_a_non_ocr_internal_model_stays_hidden_even_with_include_ocr(
        self,
        bearer_app: FastAPI,
        client: Any,
        seeded: Seeded,
        signing_key: RSAKey,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        """The opt-in is per kind, not per provider: an internal model of some
        other kind must not ride along just because the request asked for OCR."""
        await _seed_internal_ocr_model(session_factory, seeded, kind=ModelKind.CHAT)
        response = await client.get(
            "/v1/models?include=ocr,chat", headers=auth(make_token(signing_key))
        )
        assert response.status_code == 200, response.text
        names = [entry["id"] for entry in response.json()["data"]]
        assert "markitdown" not in names

    @pytest.mark.asyncio
    async def test_ordinary_ocr_models_are_not_flagged_local(
        self,
        bearer_app: FastAPI,
        client: Any,
        seeded: Seeded,
        signing_key: RSAKey,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        """``local`` names the internal provider, not the OCR kind — an
        upstream OCR model rides the default list exactly as before, unflagged."""
        from gateway.models import ModelDef
        from gateway.secrets import SecretBox, hint_for

        async with session_factory() as session:
            key = "k"
            provider = Provider(
                name="cortecs-ocr",
                base_url="https://api.cortecs.ai/v1",
                api_key_encrypted=SecretBox(["test-encryption-key-not-for-production"]).encrypt(
                    key
                ),
                api_key_hint=hint_for(key),
                plugin="cortecs",
                kind=ProviderKind.ROUTER,
            )
            session.add(provider)
            await session.flush()
            model = ModelDef(
                name="mistral-ocr-4.1",
                upstream_model="mistral-ocr-4.1",
                provider_id=provider.id,
                kind=ModelKind.OCR,
            )
            session.add(model)
            await session.flush()
            session.add(GroupModelAccess(group_id=seeded.group.id, model_id=model.id))
            await session.commit()

        response = await client.get("/v1/models", headers=auth(make_token(signing_key)))
        assert response.status_code == 200, response.text
        by_id = {entry["id"]: entry for entry in response.json()["data"]}
        assert by_id["mistral-ocr-4.1"]["local"] is False
