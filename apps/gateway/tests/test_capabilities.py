"""Model capabilities: imported, exposed, and editable (ADR 0031).

Three properties, each of which fails silently rather than loudly if it breaks:

* a caller can find out what a model does **before** calling it, instead of
  discovering it from a 400;
* discovery reports what the provider claims, so the choice to import is
  informed;
* an operator can correct a claim, because a catalogue is a claim and not a
  contract — and the correction must survive.
"""

from __future__ import annotations

import httpx
import pytest
from conftest import Seeded
from gateway.models import GroupModelAccess, ModelDef, ModelKind
from sqlalchemy.ext.asyncio import AsyncSession
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


async def add_model(
    session: AsyncSession,
    seeded: Seeded,
    *,
    name: str,
    kind: ModelKind = ModelKind.CHAT,
    **capabilities: object,
) -> ModelDef:
    model = ModelDef(
        name=name,
        upstream_model=f"vendor/{name}",
        provider_id=seeded.provider.id,
        kind=kind,
        **capabilities,  # type: ignore[arg-type]
    )
    session.add(model)
    await session.flush()
    session.add(GroupModelAccess(group_id=seeded.group.id, model_id=model.id))
    await session.commit()
    return model


class TestOnTheOpenAISurface:
    async def test_a_caller_can_see_what_a_model_does(
        self, client: httpx.AsyncClient, seeded: Seeded, session: AsyncSession
    ) -> None:
        """The point of the whole thing: no 400 needed to find out."""
        await add_model(
            session,
            seeded,
            name="vision-1",
            input_modalities=["image", "text"],
            output_modalities=["text"],
            supported_features=["reasoning", "tools"],
        )
        body = (await client.get("/v1/models", headers=seeded.auth)).json()
        card = next(entry for entry in body["data"] if entry["id"] == "vision-1")

        assert card["input_modalities"] == ["image", "text"]
        assert card["supported_features"] == ["reasoning", "tools"]
        assert card["kind"] == "chat"

    async def test_a_model_nobody_has_described_claims_nothing(
        self, client: httpx.AsyncClient, seeded: Seeded
    ) -> None:
        """Empty lists, not absent keys and not invented defaults.

        A model catalogued before capabilities existed genuinely has no claim
        attached, and guessing one from its name would be a fact we made up.
        """
        body = (await client.get("/v1/models", headers=seeded.auth)).json()
        card = next(entry for entry in body["data"] if entry["id"] == seeded.model.name)
        assert card["supported_features"] == []
        assert card["input_modalities"] == []

    async def test_the_single_model_route_agrees_with_the_listing(
        self, client: httpx.AsyncClient, seeded: Seeded, session: AsyncSession
    ) -> None:
        await add_model(session, seeded, name="tools-1", supported_features=["tools"])
        listed = (await client.get("/v1/models", headers=seeded.auth)).json()["data"]
        one = (await client.get("/v1/models/tools-1", headers=seeded.auth)).json()
        assert one == next(entry for entry in listed if entry["id"] == "tools-1")


class TestEditing:
    async def test_an_operator_can_correct_a_claim(
        self, admin_client: httpx.AsyncClient, seeded: Seeded, session: AsyncSession
    ) -> None:
        """A catalogue is a claim. Sometimes it is wrong."""
        model = await add_model(
            session, seeded, name="claims-tools", supported_features=["tools", "json_mode"]
        )
        response = await admin_client.patch(
            f"/api/admin/models/{model.id}", json={"supported_features": ["json_mode"]}
        )
        assert response.status_code == 200
        assert response.json()["supported_features"] == ["json_mode"]

    async def test_the_correction_survives_a_reload(
        self, admin_client: httpx.AsyncClient, seeded: Seeded, session: AsyncSession
    ) -> None:
        model = await add_model(session, seeded, name="persisted", supported_features=["tools"])
        await admin_client.patch(f"/api/admin/models/{model.id}", json={"supported_features": []})
        listing = (await admin_client.get("/api/admin/models?limit=200")).json()["items"]
        assert next(m for m in listing if m["name"] == "persisted")["supported_features"] == []

    async def test_an_edit_is_normalised_like_an_import(
        self, admin_client: httpx.AsyncClient, seeded: Seeded, session: AsyncSession
    ) -> None:
        """Otherwise a hand-typed "Tools" and an imported "tools" differ."""
        model = await add_model(session, seeded, name="messy")
        response = await admin_client.patch(
            f"/api/admin/models/{model.id}",
            json={"supported_features": ["Tools", " tools ", "REASONING"]},
        )
        assert response.json()["supported_features"] == ["reasoning", "tools"]

    async def test_an_unset_field_is_left_alone(
        self, admin_client: httpx.AsyncClient, seeded: Seeded, session: AsyncSession
    ) -> None:
        """The whole update contract: only what is sent changes."""
        model = await add_model(
            session,
            seeded,
            name="untouched",
            supported_features=["tools"],
            input_modalities=["text"],
        )
        response = await admin_client.patch(
            f"/api/admin/models/{model.id}", json={"display_name": "Untouched"}
        )
        assert response.json()["supported_features"] == ["tools"]
        assert response.json()["input_modalities"] == ["text"]

    async def test_the_kind_can_be_corrected(
        self, admin_client: httpx.AsyncClient, seeded: Seeded, session: AsyncSession
    ) -> None:
        """Discovery infers it from modality tags, and can infer it wrong.

        A mis-inferred kind takes a model off the only route that would serve
        it, and before this there was no way to fix that short of SQL.
        """
        model = await add_model(session, seeded, name="misfiled", kind=ModelKind.CHAT)
        response = await admin_client.patch(
            f"/api/admin/models/{model.id}", json={"kind": "embedding"}
        )
        assert response.status_code == 200
        assert response.json()["kind"] == "embedding"

        # And the routing follows immediately.
        refused = await admin_client.post(
            "/v1/chat/completions",
            json={"model": "misfiled", "messages": [{"role": "user", "content": "hi"}]},
            headers=seeded.auth,
        )
        assert refused.status_code == 400
        assert "/v1/embeddings" in refused.json()["error"]["message"]

    async def test_an_unknown_kind_is_refused(
        self, admin_client: httpx.AsyncClient, seeded: Seeded, session: AsyncSession
    ) -> None:
        model = await add_model(session, seeded, name="bad-kind")
        response = await admin_client.patch(f"/api/admin/models/{model.id}", json={"kind": "audio"})
        assert response.status_code == 400


class TestCreation:
    async def test_capabilities_can_be_set_when_cataloguing_by_hand(
        self, admin_client: httpx.AsyncClient, seeded: Seeded
    ) -> None:
        response = await admin_client.post(
            "/api/admin/models",
            json={
                "name": "hand-made",
                "upstream_model": "vendor/hand-made",
                "provider_id": str(seeded.provider.id),
                "supported_features": ["tools"],
                "input_modalities": ["text", "image"],
            },
        )
        assert response.status_code == 201
        assert response.json()["supported_features"] == ["tools"]
        assert response.json()["input_modalities"] == ["image", "text"]
