"""`tool_images`: a model that can be shown images a tool returned.

The chat's `look_doc` hands a vision model document pages after a tool result.
Whether a given model uses them is a per-model capability. These tests pin the
three ways a model gets the default (migration, catalogue import) or loses it
(an administrator), and that the change is visible on `/v1/models` and audited.
"""

from __future__ import annotations

import importlib.util
import json
import sqlite3
import uuid
from contextlib import closing
from pathlib import Path

import httpx
import pytest
import sqlalchemy as sa
from alembic.migration import MigrationContext
from alembic.operations import Operations
from conftest import Seeded
from gateway.models import AdminAuditEvent, ModelDef
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from test_admin import as_user, make_admin
from test_migrations_sqlite import _alembic, _insert_model, _insert_provider

CATALOGUE = {
    "data": [
        {
            "id": "vendor/seer",
            "pricing": {"input_token": "1", "output_token": "2", "currency": "EUR"},
            "input_modalities": ["text", "image"],
            "output_modalities": ["text"],
            "supported_features": ["tools"],
        },
        {
            "id": "vendor/reader",
            "pricing": {"input_token": "1", "output_token": "2", "currency": "EUR"},
            "input_modalities": ["text"],
            "output_modalities": ["text"],
            "supported_features": ["tools"],
        },
    ]
}


@pytest.fixture
async def admin_client(
    app: object,
    client: httpx.AsyncClient,
    seeded: Seeded,
    session_factory: async_sessionmaker[AsyncSession],
) -> httpx.AsyncClient:
    as_user(app, await make_admin(session_factory, seeded))
    app.state.control_http = httpx.AsyncClient(  # type: ignore[attr-defined]
        transport=httpx.MockTransport(lambda request: httpx.Response(200, json=CATALOGUE))
    )
    return client


async def _import(client: httpx.AsyncClient, seeded: Seeded, *upstream: str) -> None:
    response = await client.post(
        "/api/admin/models/import?provider_id=" + str(seeded.provider.id),
        json={"models": [{"upstream_model": item} for item in upstream]},
    )
    assert response.status_code == 201, response.text
    assert all(result["imported"] for result in response.json()["results"]), response.text


async def _features(session: AsyncSession, upstream: str) -> list[str]:
    session.expire_all()
    model = (
        await session.execute(select(ModelDef).where(ModelDef.upstream_model == upstream))
    ).scalar_one()
    return list(model.supported_features)


class TestCatalogueImport:
    async def test_discovery_offers_it_for_an_image_model_only(
        self, admin_client: httpx.AsyncClient, seeded: Seeded
    ) -> None:
        body = (
            await admin_client.get(
                "/api/admin/models/discover?provider_id=" + str(seeded.provider.id)
            )
        ).json()
        offered = {row["upstream_model"]: row["supported_features"] for row in body["available"]}
        assert offered["vendor/seer"] == ["tool_images", "tools"]
        assert offered["vendor/reader"] == ["tools"]

    async def test_a_new_image_model_is_imported_with_it(
        self, admin_client: httpx.AsyncClient, seeded: Seeded, session: AsyncSession
    ) -> None:
        await _import(admin_client, seeded, "vendor/seer", "vendor/reader")
        assert await _features(session, "vendor/seer") == ["tool_images", "tools"]
        assert await _features(session, "vendor/reader") == ["tools"]

    async def test_a_removal_by_an_administrator_survives_the_next_import(
        self, admin_client: httpx.AsyncClient, seeded: Seeded, session: AsyncSession
    ) -> None:
        """The catalogue is read into a row once, when the row is created.

        Importing again skips a model that exists, so nothing re-adds a value an
        administrator took off; discovery stops offering the model too.
        """
        await _import(admin_client, seeded, "vendor/seer")
        model = (
            await session.execute(select(ModelDef).where(ModelDef.upstream_model == "vendor/seer"))
        ).scalar_one()
        patched = await admin_client.patch(
            f"/api/admin/models/{model.id}", json={"supported_features": ["tools"]}
        )
        assert patched.status_code == 200, patched.text

        again = await admin_client.post(
            "/api/admin/models/import?provider_id=" + str(seeded.provider.id),
            json={"models": [{"upstream_model": "vendor/seer"}]},
        )
        assert again.status_code == 201, again.text
        assert again.json()["results"][0]["imported"] is False
        assert await _features(session, "vendor/seer") == ["tools"]
        discovered = (
            await admin_client.get(
                "/api/admin/models/discover?provider_id=" + str(seeded.provider.id)
            )
        ).json()
        assert "vendor/seer" not in {row["upstream_model"] for row in discovered["available"]}


class TestOnTheOpenAISurface:
    async def test_v1_models_lists_it_until_it_is_turned_off(
        self, admin_client: httpx.AsyncClient, seeded: Seeded, session: AsyncSession
    ) -> None:
        await _import(admin_client, seeded, "vendor/seer")
        model = (
            await session.execute(select(ModelDef).where(ModelDef.upstream_model == "vendor/seer"))
        ).scalar_one()
        grant = await admin_client.put(f"/api/admin/groups/{seeded.group.id}/models/{model.id}")
        assert grant.status_code in (200, 201, 204), grant.text

        def card(body: dict[str, object]) -> dict[str, object]:
            return next(entry for entry in body["data"] if entry["id"] == model.name)  # type: ignore[attr-defined,union-attr]

        listed = (await admin_client.get("/v1/models", headers=seeded.auth)).json()
        assert "tool_images" in card(listed)["supported_features"]  # type: ignore[operator]

        await admin_client.patch(
            f"/api/admin/models/{model.id}", json={"supported_features": ["tools"]}
        )
        listed = (await admin_client.get("/v1/models", headers=seeded.auth)).json()
        assert "tool_images" not in card(listed)["supported_features"]  # type: ignore[operator]


class TestAudit:
    async def test_turning_it_off_is_recorded(
        self,
        admin_client: httpx.AsyncClient,
        seeded: Seeded,
        session: AsyncSession,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        await _import(admin_client, seeded, "vendor/seer")
        model = (
            await session.execute(select(ModelDef).where(ModelDef.upstream_model == "vendor/seer"))
        ).scalar_one()
        await admin_client.patch(
            f"/api/admin/models/{model.id}", json={"supported_features": ["tools"]}
        )
        async with session_factory() as fresh:
            events = (
                (
                    await fresh.execute(
                        select(AdminAuditEvent).where(AdminAuditEvent.action == "model.update")
                    )
                )
                .scalars()
                .all()
            )
        assert len(events) == 1
        assert events[0].changes == {
            "supported_features": {
                "before": ["tool_images", "tools"],
                "after": ["tools"],
            }
        }


def _read(database: Path) -> dict[str, list[str]]:
    with closing(sqlite3.connect(database)) as connection:
        return {
            name: json.loads(features)
            for name, features in connection.execute(
                "SELECT name, supported_features FROM models WHERE name LIKE 'tm-%'"
            )
        }


def test_0073_adds_the_feature_to_vision_models_only_and_is_idempotent(tmp_path: Path) -> None:
    database = tmp_path / "tool-images.db"
    url = f"url=sqlite+aiosqlite:///{database}"
    assert _alembic("-x", url, "upgrade", "0072").returncode == 0
    with closing(sqlite3.connect(database)) as connection:
        _insert_provider(connection, "p1")
        for name, inputs, features in (
            ("tm-vision", ["image", "text"], ["tools"]),
            ("tm-text", ["text"], ["tools"]),
            ("tm-none", [], []),
            ("tm-has", ["image"], ["tool_images"]),
        ):
            _insert_model(connection, uuid.uuid4().hex, name, upstream=f"up/{name}", provider="p1")
            connection.execute(
                "UPDATE models SET input_modalities = ?, supported_features = ? WHERE name = ?",
                (json.dumps(inputs), json.dumps(features), name),
            )
        connection.commit()

    assert _alembic("-x", url, "upgrade", "0073").returncode == 0
    once = _read(database)
    assert once == {
        "tm-vision": ["tool_images", "tools"],
        "tm-text": ["tools"],
        "tm-none": [],
        "tm-has": ["tool_images"],
    }

    # Idempotent: the revision's body run again over the migrated rows changes
    # nothing (an operator may also have added the value before upgrading, as
    # tm-has did).
    spec = importlib.util.spec_from_file_location(
        "tool_images_0073",
        Path(__file__).resolve().parents[1] / "migrations/versions/0073_tool_images_feature.py",
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    engine = sa.create_engine(f"sqlite:///{database}")
    with engine.begin() as connection, Operations.context(MigrationContext.configure(connection)):
        module.upgrade()
        module.upgrade()
    engine.dispose()
    assert _read(database) == once

    # Down and up again lands in the same place.
    assert _alembic("-x", url, "downgrade", "0072").returncode == 0
    assert _read(database)["tm-vision"] == ["tools"]
    assert _alembic("-x", url, "upgrade", "head").returncode == 0
    assert _read(database) == once
