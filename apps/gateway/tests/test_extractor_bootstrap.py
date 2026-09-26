"""The extractor is seeded by default, and hidden from every listing.

Two properties are pinned here, because they are the two ways this change
could quietly rot:

* **Serve by default, never resurrect.** Migration 0041 seeds the provider and
  its model rows when neither exists (`gateway.bootstrap.seed_extractor`), and
  what an operator did to the rows afterwards is never overridden — the
  migration runs once, so a model they deliberately deleted stays deleted no
  matter how many times the seed would "helpfully" re-create it. The tests
  pin the seed's contract directly: create when absent, write nothing when
  present, and never backfill a missing model beside a surviving provider,
  because that is exactly what a deliberate deletion leaves behind.

* **Hidden is presentation, not access.** A model whose provider is
  `internal` is absent from `/v1/models` and from the console's unpriced
  warning, and still resolves on `/v1/ocr` — usage meters to the ledger with
  the same zero cost an unpriced model always recorded. Hiding must not
  unaccount; if it ever does, one of these tests fails.
"""

from __future__ import annotations

import uuid
from decimal import Decimal
from typing import Any

import httpx
import pytest
from conftest import FakeUpstream, Seeded
from gateway.bootstrap import MODEL_NAME, normalize_extractor_kind, seed_extractor
from gateway.models import (
    Base,
    BillingMode,
    ModelDef,
    ModelKind,
    Provider,
    ProviderKind,
    UsageRecord,
)
from gateway.plugins.extractor import LocalExtractorPlugin
from sqlalchemy import StaticPool, create_engine, insert, select, update
from sqlalchemy.ext.asyncio import AsyncSession
from test_admin import as_user, make_admin
from test_ocr_surface import INLINE_DOCX, FakeExtractor

# --------------------------------------------------------------------------
# the seed, against a schema built from the ORM metadata
# --------------------------------------------------------------------------


@pytest.fixture
def db_connection() -> Any:
    """A schema-complete SQLite connection for the seed tests.

    In-memory, but through a StaticPool: a bare ``sqlite://`` hands every new
    connection an empty database, which would forget the schema between
    ``create_all`` and the first test statement. The seed commits nothing —
    the caller owns the transaction, which is why these tests commit by hand.
    """
    engine = create_engine("sqlite://", poolclass=StaticPool)
    Base.metadata.create_all(engine)
    with engine.connect() as connection:
        yield connection
    engine.dispose()


def _provider_row(connection: Any) -> Any:
    """The one provider row naming the extractor plugin, if any."""
    return (
        connection.execute(select(Provider).where(Provider.plugin == "extractor"))
    ).first()


def _model_row(connection: Any, name: str = MODEL_NAME) -> Any:
    return (connection.execute(select(ModelDef).where(ModelDef.name == name))).first()


def _live_shape(connection: Any, *, with_model: bool = True) -> None:
    """The manual path's rows, as this deployment has them today.

    The provider row names the plugin and carries its pre-0041 kind, because
    that is what the console's create flow wrote before the plugin declared
    `internal`.
    """
    provider_id = uuid.uuid4()
    connection.execute(
        insert(Provider).values(
            id=provider_id,
            name="extractor",
            base_url="http://elsewhere:8080",
            plugin="extractor",
            kind=ProviderKind.PROVIDER,
        )
    )
    if with_model:
        connection.execute(
            insert(ModelDef).values(
                id=uuid.uuid4(),
                name=MODEL_NAME,
                upstream_model=MODEL_NAME,
                provider_id=provider_id,
                kind=ModelKind.OCR,
                is_active=True,
                is_public=False,
            )
        )


class TestSeed:
    def test_a_fresh_deployment_gets_the_pair(self, db_connection: Any) -> None:
        """One call on an empty schema: exactly the rows the manual path made."""
        seed_extractor(db_connection)
        db_connection.commit()

        provider = _provider_row(db_connection)
        assert provider is not None
        assert provider.name == "extractor"
        assert provider.plugin == "extractor"
        assert provider.kind == ProviderKind.INTERNAL
        assert provider.base_url == "http://extractor:8080"
        # The service has no credential, by the construction the plugin
        # documents — it is not reachable off the compose network.
        assert provider.api_key_encrypted == ""
        assert provider.is_active is True
        assert provider.billing_mode == BillingMode.OWN_PRICES

        model = _model_row(db_connection)
        assert model is not None
        assert model.provider_id == provider.id
        assert model.kind == ModelKind.OCR
        # Public access is the access-plane half of "serves by default":
        # without it the rows would exist and every extraction would 403.
        assert model.is_public is True
        assert model.is_active is True
        assert list(model.input_modalities) == ["file"]
        assert list(model.output_modalities) == ["text"]

    def test_seeding_twice_creates_one_pair(self, db_connection: Any) -> None:
        """Idempotency is the migration's whole contract: run it, then run it."""
        seed_extractor(db_connection)
        db_connection.commit()
        first = _provider_row(db_connection)

        seed_extractor(db_connection)
        db_connection.commit()

        providers = (
            db_connection.execute(select(Provider).where(Provider.plugin == "extractor"))
        ).all()
        models = (db_connection.execute(select(ModelDef).where(ModelDef.name == MODEL_NAME))).all()
        assert len(providers) == 1
        assert len(models) == 1
        assert providers[0].id == first.id

    def test_a_surviving_provider_is_left_entirely_alone(self, db_connection: Any) -> None:
        """The live shape: the manual path's rows pre-date the seed.

        Nothing about them may change — the seed must not re-address,
        re-activate, re-type or re-create anything beside rows that already
        exist; the one exception is `normalize_extractor_kind`, called
        deliberately beside it in the same revision.
        """
        _live_shape(db_connection)
        db_connection.commit()
        before = _provider_row(db_connection)
        model_before = _model_row(db_connection)

        seed_extractor(db_connection)
        db_connection.commit()

        assert _model_row(db_connection).id == model_before.id
        after = _provider_row(db_connection)
        assert after.id == before.id
        assert after.base_url == "http://elsewhere:8080"
        assert after.kind == ProviderKind.PROVIDER
        assert _model_row(db_connection).is_public is False

    def test_a_deactivated_provider_survives_a_reseed(self, db_connection: Any) -> None:
        """The provider row cannot be deleted (`routers/admin.py`'s 409), so
        deactivating it is the only way off — and this is what makes that
        stick: a re-run of the seed, exactly as a fresh boot would run it,
        must not read "provider present, inactive" as "provider absent" and
        reactivate what an administrator turned off."""
        seed_extractor(db_connection)
        db_connection.commit()
        db_connection.execute(
            update(Provider).where(Provider.plugin == "extractor").values(is_active=False)
        )
        db_connection.execute(
            update(ModelDef).where(ModelDef.name == MODEL_NAME).values(is_active=False)
        )
        db_connection.commit()

        seed_extractor(db_connection)
        db_connection.commit()

        assert _provider_row(db_connection).is_active is False
        assert _model_row(db_connection).is_active is False

    def test_a_deleted_model_is_never_backfilled(self, db_connection: Any) -> None:
        """The no-resurrection pin: provider present, model deliberately gone.

        Backfilling here is the resurrection the seed exists to prevent — an
        operator who deleted the model had a reason, and the seed must not
        decide it knows better on the next run.
        """
        _live_shape(db_connection, with_model=False)
        db_connection.commit()

        seed_extractor(db_connection)
        db_connection.commit()

        assert _model_row(db_connection) is None

    def test_a_name_collision_skips_the_seed(self, db_connection: Any) -> None:
        """An unlucky name on an unrelated row skips the seed, never fails it.

        A migration that aborts on an unlucky name turns an upgrade every
        deployment runs into a cliff only one deployment falls off; the
        operator who sees no extractor row can create one by hand, which is
        where they already were.
        """
        db_connection.execute(
            insert(Provider).values(
                id=uuid.uuid4(),
                name="extractor",
                base_url="http://unrelated.example/v1",
                plugin="generic",
            )
        )
        db_connection.commit()

        seed_extractor(db_connection)
        db_connection.commit()

        # Nothing was created under either name, and nothing crashed.
        assert _provider_row(db_connection) is None
        assert _model_row(db_connection) is None

    def test_the_seed_matches_what_the_manual_path_imported(self) -> None:
        """The drift guard: the seed creates what Discover would have imported.

        The values are pinned at seed time so the code reads as the rows it
        creates; this test is what notices when the plugin's own catalogue —
        the source the manual path imported from — moves under them.
        """
        plugin = LocalExtractorPlugin()
        catalogue = plugin.builtin_catalogue()
        assert catalogue is not None
        entries = catalogue["data"]
        assert len(entries) == 1
        entry = entries[0]
        assert entry["id"] == MODEL_NAME
        # The tag is what the importer reads the kind from; the modalities
        # are what it copies onto the row.
        assert "OCR" in entry["tags"]
        assert entry["input_modalities"] == ["file"]
        assert entry["output_modalities"] == ["text"]
        # No price, by the plugin's own claim, so the seed sets none either.
        assert "pricing" not in entry
        assert plugin.kind == ProviderKind.INTERNAL
        assert plugin.default_base_url == "http://extractor:8080"


class TestKindNormalization:
    def test_the_live_row_is_retyped(self, db_connection: Any) -> None:
        """Pre-0041 rows say `provider`; the plugin now says `internal`.

        Leaving them would keep the model in every listing this change hides
        it from, and light the console's kind-mismatch badge on the row this
        change exists to quiet.
        """
        _live_shape(db_connection)
        db_connection.commit()

        normalize_extractor_kind(db_connection)
        db_connection.commit()

        assert _provider_row(db_connection).kind == ProviderKind.INTERNAL

    def test_rows_that_are_not_the_extractor_are_untouched(self, db_connection: Any) -> None:
        """The retyping keys on the plugin, never on the name."""
        db_connection.execute(
            insert(Provider).values(
                id=uuid.uuid4(),
                name="extractor",
                base_url="http://unrelated.example/v1",
                plugin="generic",
                kind=ProviderKind.PROVIDER,
            )
        )
        db_connection.commit()

        normalize_extractor_kind(db_connection)
        db_connection.commit()

        row = (db_connection.execute(select(Provider))).first()
        assert row.kind == ProviderKind.PROVIDER


# --------------------------------------------------------------------------
# the listing exclusion, against the real routes
# --------------------------------------------------------------------------


async def add_extractor_pair(
    session: AsyncSession,
    *,
    kind: ProviderKind = ProviderKind.INTERNAL,
    public: bool = True,
    name: str = "extractor",
) -> ModelDef:
    """The seeded shape, as a fresh deployment would have it after 0041."""
    provider = Provider(
        name=name,
        base_url="http://extractor:8080",
        plugin="extractor",
        kind=kind,
    )
    session.add(provider)
    await session.flush()
    model = ModelDef(
        name=MODEL_NAME,
        upstream_model=MODEL_NAME,
        provider_id=provider.id,
        kind=ModelKind.OCR,
        is_public=public,
        input_modalities=["file"],
        output_modalities=["text"],
    )
    session.add(model)
    await session.commit()
    return model


class TestListing:
    async def test_v1_models_leaves_the_extractor_out(
        self, client: httpx.AsyncClient, seeded: Seeded, session: AsyncSession
    ) -> None:
        await add_extractor_pair(session)

        response = await client.get("/v1/models", headers=seeded.auth)
        assert response.status_code == 200
        names = [model["id"] for model in response.json()["data"]]
        assert "test-model" in names, "the ordinary catalogue must survive"
        assert MODEL_NAME not in names

    async def test_asking_by_name_still_answers(
        self, client: httpx.AsyncClient, seeded: Seeded, session: AsyncSession
    ) -> None:
        """Hidden from the list, not gone: a caller whose grants reach the
        model — here, public access — may still ask for it by name."""
        await add_extractor_pair(session)

        response = await client.get("/v1/models/markitdown", headers=seeded.auth)
        assert response.status_code == 200
        assert response.json()["id"] == MODEL_NAME

    async def test_the_admin_model_list_stays_complete_and_names_the_kind(
        self,
        app: Any,
        client: httpx.AsyncClient,
        seeded: Seeded,
        session: AsyncSession,
        session_factory: Any,
    ) -> None:
        """The admin API is the complete record; the console's Models screen
        curates client-side, exactly as it already sets search tiers aside."""
        admin = await make_admin(session_factory, seeded)
        model = await add_extractor_pair(session)
        as_user(app, admin)

        response = await client.get("/api/admin/models")
        assert response.status_code == 200
        rows = {item["name"]: item for item in response.json()["items"]}
        assert rows[MODEL_NAME]["provider_kind"] == "internal"
        assert rows[MODEL_NAME]["id"] == str(model.id)

    async def test_the_providers_list_stops_warning_about_the_unpriced_chip(
        self,
        app: Any,
        client: httpx.AsyncClient,
        seeded: Seeded,
        session: AsyncSession,
        session_factory: Any,
    ) -> None:
        """The screenshot that started this: the extractor row wearing the
        "1 unpriced" warn chip that a billing hole wears. Its unpricedness is
        the design, so the warning does not apply."""
        admin = await make_admin(session_factory, seeded)
        await add_extractor_pair(session)
        as_user(app, admin)

        response = await client.get("/api/admin/providers")
        assert response.status_code == 200
        rows = {item["name"]: item for item in response.json()["items"]}
        assert rows["extractor"]["unpriced_model_count"] == 0
        # The model count stays complete on purpose: it is the blast radius
        # of deactivating or deleting the provider, and the extractor has one.
        assert rows["extractor"]["model_count"] == 1


class TestServing:
    async def test_extraction_serves_out_of_the_box(
        self,
        app: Any,
        client: httpx.AsyncClient,
        seeded: Seeded,
        session: AsyncSession,
        fake_upstream: FakeUpstream,
    ) -> None:
        """The whole point: public access + the seeded rows means a caller
        needs no administrator to have done anything by hand."""
        extractor = FakeExtractor()
        app.state.control_http = extractor.client()
        await add_extractor_pair(session)

        response = await client.post(
            "/v1/ocr",
            json={
                "model": MODEL_NAME,
                "document": {"type": "document_url", "document_url": INLINE_DOCX},
            },
            headers=seeded.auth,
        )
        assert response.status_code == 200
        assert extractor.bodies, "the extraction service was not called"
        assert not fake_upstream.bodies

        # Hidden is not unaccounted: the ledger carries the request exactly as
        # it always did for this surface — unpriced, so a zero cost, with the
        # model named and no substitution invented.
        record = (
            (
                await session.execute(
                    select(UsageRecord).order_by(UsageRecord.created_at.desc()).limit(1)
                )
            )
            .scalars()
            .one()
        )
        assert record.model_name == MODEL_NAME
        assert record.cost == Decimal(0)
        assert record.model_substituted is False


class TestDeletionProtection:
    """The local extractor can be deactivated, never deleted.

    It is this deployment's own infrastructure, seeded once by migration 0041
    and never re-created afterwards (`TestSeed`) — so "delete" would mean
    nothing but a confusing 404 until the next fresh install, never a real
    removal. Deactivation is the real off switch, and it must still work.
    """

    async def test_deleting_the_provider_is_409(
        self,
        app: Any,
        client: httpx.AsyncClient,
        seeded: Seeded,
        session: AsyncSession,
        session_factory: Any,
    ) -> None:
        admin = await make_admin(session_factory, seeded)
        model = await add_extractor_pair(session)
        as_user(app, admin)

        response = await client.delete(f"/api/admin/providers/{model.provider_id}")
        assert response.status_code == 409
        assert "deactivated" in response.json()["error"]["message"]

        # Refused, not half-done: the row is still there afterwards.
        assert (await client.get("/api/admin/providers")).status_code == 200
        listing = (await client.get("/api/admin/providers")).json()["items"]
        assert any(item["name"] == "extractor" for item in listing)

    async def test_deleting_the_model_row_is_409(
        self,
        app: Any,
        client: httpx.AsyncClient,
        seeded: Seeded,
        session: AsyncSession,
        session_factory: Any,
    ) -> None:
        admin = await make_admin(session_factory, seeded)
        model = await add_extractor_pair(session)
        as_user(app, admin)

        response = await client.delete(f"/api/admin/models/{model.id}")
        assert response.status_code == 409
        assert "deactivated" in response.json()["error"]["message"]

    async def test_deactivating_the_provider_still_works(
        self,
        app: Any,
        client: httpx.AsyncClient,
        seeded: Seeded,
        session: AsyncSession,
        session_factory: Any,
    ) -> None:
        admin = await make_admin(session_factory, seeded)
        model = await add_extractor_pair(session)
        as_user(app, admin)

        response = await client.patch(
            f"/api/admin/providers/{model.provider_id}", json={"is_active": False}
        )
        assert response.status_code == 200, response.text
        assert response.json()["is_active"] is False

    async def test_reactivating_the_provider_works(
        self,
        app: Any,
        client: httpx.AsyncClient,
        seeded: Seeded,
        session: AsyncSession,
        session_factory: Any,
    ) -> None:
        admin = await make_admin(session_factory, seeded)
        model = await add_extractor_pair(session)
        as_user(app, admin)

        await client.patch(f"/api/admin/providers/{model.provider_id}", json={"is_active": False})
        response = await client.patch(
            f"/api/admin/providers/{model.provider_id}", json={"is_active": True}
        )
        assert response.status_code == 200, response.text
        assert response.json()["is_active"] is True

    async def test_an_ordinary_provider_is_unaffected(
        self,
        app: Any,
        client: httpx.AsyncClient,
        seeded: Seeded,
        session_factory: Any,
    ) -> None:
        """The protection is scoped to `kind == internal`, not a blanket rule."""
        as_user(app, await make_admin(session_factory, seeded))
        created = (
            await client.post(
                "/api/admin/providers",
                json={"name": "unrelated", "base_url": "https://x.test/v1"},
            )
        ).json()
        assert (await client.delete(f"/api/admin/providers/{created['id']}")).status_code == 204
