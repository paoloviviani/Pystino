"""``models.max_input_tokens``: the hand-set input cap.

Set by an operator because no catalogue or price file reports a provider's
real input limit, so three properties matter: it is editable on the admin
surface, it is never filled by anything automatic, and — the point of having
it at all — the token routes refuse a prompt that exceeds it while staying
silent when it is unset. Null means "no local limit", never zero.
"""

from __future__ import annotations

from typing import Any

import httpx
import pytest
from conftest import Seeded
from gateway.models import ModelDef
from helpers import completion_body
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from test_admin import as_user, make_admin


@pytest.fixture
async def admin_client(
    app: object,
    client: httpx.AsyncClient,
    seeded: Seeded,
    session_factory: object,
) -> httpx.AsyncClient:
    as_user(app, await make_admin(session_factory, seeded))  # type: ignore[arg-type]
    return client


def basic_request(model: str, text: str = "hello") -> dict[str, Any]:
    return {
        "model": model,
        "messages": [{"role": "user", "content": text}],
    }


async def set_limit(
    session_factory: async_sessionmaker[AsyncSession],
    seeded: Seeded,
    value: int | None,
) -> None:
    async with session_factory() as session:
        model = (
            await session.execute(select(ModelDef).where(ModelDef.id == seeded.model.id))
        ).scalar_one()
        model.max_input_tokens = value
        await session.commit()


class TestAdminSurface:
    @pytest.mark.asyncio
    async def test_create_and_update_carry_it(
        self, admin_client: httpx.AsyncClient, seeded: Seeded
    ) -> None:
        created = await admin_client.post(
            "/api/admin/models",
            json={
                "name": "capped-model",
                "upstream_model": "upstream/capped",
                "provider_id": str(seeded.provider.id),
                "max_input_tokens": 4096,
            },
        )
        assert created.status_code == 201, created.text
        assert created.json()["max_input_tokens"] == 4096

        model_id = created.json()["id"]
        # The update path is a patch of unset-only fields: sending null is how
        # the operator clears a mistaken cap, so null must reach the column.
        patched = await admin_client.patch(
            f"/api/admin/models/{model_id}", json={"max_input_tokens": None}
        )
        assert patched.status_code == 200, patched.text
        assert patched.json()["max_input_tokens"] is None

    @pytest.mark.asyncio
    async def test_a_zero_or_negative_limit_is_refused(
        self, admin_client: httpx.AsyncClient, seeded: Seeded
    ) -> None:
        response = await admin_client.post(
            "/api/admin/models",
            json={
                "name": "capped-model",
                "upstream_model": "upstream/capped",
                "provider_id": str(seeded.provider.id),
                "max_input_tokens": 0,
            },
        )
        assert response.status_code in (400, 422)

    @pytest.mark.asyncio
    async def test_it_rides_the_v1_models_card(
        self, client: httpx.AsyncClient, seeded: Seeded, session_factory
    ) -> None:
        await set_limit(session_factory, seeded, 4096)
        response = await client.get("/v1/models", headers=seeded.auth)
        assert response.status_code == 200
        entry = next(
            item for item in response.json()["data"] if item["id"] == "test-model"
        )
        assert entry["max_input_tokens"] == 4096


class TestEnforcement:
    @pytest.mark.asyncio
    async def test_an_over_limit_prompt_is_refused_before_any_reservation(
        self,
        client: httpx.AsyncClient,
        seeded: Seeded,
        fake_upstream: Any,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        """Refusal happens before `begin`, so no usage row and no reservation:
        a prompt the model cannot serve must never cost anything."""
        await set_limit(session_factory, seeded, 4)
        response = await client.post(
            "/v1/chat/completions",
            json=basic_request("test-model", "a prompt of more than four tokens"),
            headers=seeded.auth,
        )
        assert response.status_code == 400
        assert response.json()["error"]["code"] == "prompt_too_long"
        assert fake_upstream.bodies == []

    @pytest.mark.asyncio
    async def test_unset_means_no_local_limit(
        self,
        client: httpx.AsyncClient,
        seeded: Seeded,
        fake_upstream: Any,
    ) -> None:
        """Null is the honest "unknown", not a zero: a model with no recorded
        cap serves every prompt, exactly as before the column existed."""
        fake_upstream.set_json(completion_body())
        response = await client.post(
            "/v1/chat/completions",
            json=basic_request("test-model", "a prompt of more than four tokens"),
            headers=seeded.auth,
        )
        assert response.status_code == 200, response.text

    @pytest.mark.asyncio
    async def test_a_prompt_within_the_limit_passes(
        self,
        client: httpx.AsyncClient,
        seeded: Seeded,
        fake_upstream: Any,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        await set_limit(session_factory, seeded, 10_000)
        fake_upstream.set_json(completion_body())
        response = await client.post(
            "/v1/chat/completions",
            json=basic_request("test-model"),
            headers=seeded.auth,
        )
        assert response.status_code == 200, response.text

    @pytest.mark.asyncio
    async def test_embeddings_are_bounded_by_it_too(
        self,
        client: httpx.AsyncClient,
        seeded: Seeded,
        fake_upstream: Any,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        async with session_factory() as session:
            model = (
                await session.execute(select(ModelDef).where(ModelDef.id == seeded.model.id))
            ).scalar_one()
            model.kind = "embedding"
            model.max_input_tokens = 4
            await session.commit()

        response = await client.post(
            "/v1/embeddings",
            json={"model": "test-model", "input": "a prompt of more than four tokens"},
            headers=seeded.auth,
        )
        assert response.status_code == 400
        assert response.json()["error"]["code"] == "prompt_too_long"
