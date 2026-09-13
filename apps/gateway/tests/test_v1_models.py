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
from gateway.models import GroupModelAccess, ModelKind, Provider, ProviderKind
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
            session.add(
                GroupModelAccess(group_id=seeded.group.id, model_id=tier.id)
            )
        await session.commit()


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
