"""Model aliases end to end: resolution, access, billing, and unavailability.

ADR 0101. The whole feature rests on one asymmetry — access is decided on the
name the caller sent, while the billing follows the **target** — so most of what
is here is a case where those two disagree and only one of them is allowed to
win.

These drive the real ASGI app against the fake upstream, so they cover the
wiring (which surface, which ledger column, which status) and not only the
resolver.
"""

from __future__ import annotations

import uuid
from decimal import Decimal
from typing import ClassVar

import httpx
import pytest
from conftest import FakeUpstream, Seeded
from gateway.models import (
    ApiKey,
    Group,
    GroupModelAccess,
    LimitMetric,
    LimitRule,
    LimitScope,
    Membership,
    ModelDef,
    ModelKind,
    ModelRouteTarget,
    Provider,
    UsageRecord,
    User,
)
from gateway.security import generate_api_key
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

#: The counterparty's answer. It carries a `model` on purpose: every surface
#: rewrites the echoed name only when the upstream reported one, so a fixture
#: without it would quietly skip the very line these tests are about.
COMPLETION = {
    "id": "c1",
    "object": "chat.completion",
    "model": "upstream/whatever",
    "choices": [{"index": 0, "message": {"role": "assistant", "content": "ok"}}],
    "usage": {"prompt_tokens": 10, "completion_tokens": 2, "total_tokens": 12},
}


def chat(model: str) -> dict:
    return {"model": model, "messages": [{"role": "user", "content": "hello there"}]}


async def make_alias(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    name: str = "fast",
    target: ModelDef | None = None,
    public: bool = False,
    active: bool = True,
    group_ids: list[uuid.UUID] | None = None,
) -> ModelDef:
    """An alias row plus its one route target, written the way the save path does.

    Builds the rows directly rather than through the API, so a test about
    *resolution* is not also a test about the admin routes.
    """
    async with session_factory() as session:
        target_id = target.id if target is not None else await _seeded_model_id(session)
        alias = ModelDef(
            name=name,
            provider_id=None,
            upstream_model=None,
            is_alias=True,
            kind=ModelKind.CHAT,
            display_name="Fast",
            description="the fast one",
            is_active=active,
            is_public=public,
        )
        session.add(alias)
        await session.flush()
        session.add(ModelRouteTarget(alias_id=alias.id, target_model_id=target_id, position=0))
        for group_id in group_ids or []:
            session.add(GroupModelAccess(group_id=group_id, model_id=alias.id))
        await session.commit()
        return alias


async def _seeded_model_id(session: AsyncSession) -> uuid.UUID:
    return (
        (await session.execute(select(ModelDef).where(ModelDef.name == "test-model")))
        .scalar_one()
        .id
    )


async def latest_usage(session_factory: async_sessionmaker[AsyncSession]) -> UsageRecord:
    async with session_factory() as session:
        row = (
            await session.execute(
                select(UsageRecord).order_by(UsageRecord.created_at.desc()).limit(1)
            )
        ).scalar_one_or_none()
        assert row is not None, "no usage record was written"
        return row


async def grant_only_the_alias(
    session_factory: async_sessionmaker[AsyncSession], seeded: Seeded, alias: ModelDef
) -> None:
    """Take away the seeded group's grant of the target, granting the alias.

    This is what makes "access is independent of the target" a claim rather
    than a coincidence: without it, every alias test would pass on an
    implementation that simply granted everything.
    """
    async with session_factory() as session:
        model_id = await _seeded_model_id(session)
        await session.execute(
            delete(GroupModelAccess).where(
                GroupModelAccess.model_id == model_id,
                GroupModelAccess.group_id == seeded.group.id,
            )
        )
        await session.commit()


@pytest.fixture(autouse=True)
def _completion(fake_upstream: FakeUpstream) -> None:
    fake_upstream.set_json(COMPLETION)


# --------------------------------------------------------------------------
# access independence — the reason the feature exists
# --------------------------------------------------------------------------


class TestAccessIsIndependentOfTheTarget:
    async def test_granted_the_alias_but_not_the_target_can_chat(
        self,
        client: httpx.AsyncClient,
        seeded: Seeded,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        alias = await make_alias(session_factory, public=True, group_ids=[seeded.group.id])
        await grant_only_the_alias(session_factory, seeded, alias)

        response = await client.post("/v1/chat/completions", json=chat("fast"), headers=seeded.auth)
        assert response.status_code == 200, response.text

        # And the same caller, naming the target directly, gets nothing. Asserted
        # in the same test on purpose: an implementation that granted everything,
        # or one that granted nothing, passes either half alone.
        direct = await client.post(
            "/v1/chat/completions", json=chat("test-model"), headers=seeded.auth
        )
        assert direct.status_code == 404
        assert direct.json()["error"]["code"] == "model_not_found"

    async def test_a_user_granted_the_alias_only(
        self,
        client: httpx.AsyncClient,
        seeded: Seeded,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        """A second group, granted the alias and nothing else.

        Worth a separate person rather than the seeded group because the seeded
        group is granted the target by the fixture: reusing it means a test
        that forgot to strip that grant still passes.
        """
        alias = await make_alias(session_factory, public=False)
        async with session_factory() as session:
            group = Group(name="alias-only", description="granted the alias", api_keys_allowed=True)
            session.add(group)
            await session.flush()
            user = User(
                issuer="https://idp.test",
                subject="alias-only-1",
                email="alias-only@example.org",
                default_billing_group_id=group.id,
            )
            session.add(user)
            await session.flush()
            session.add(Membership(user_id=user.id, group_id=group.id))
            session.add(GroupModelAccess(group_id=group.id, model_id=alias.id))
            await session.commit()
            generated = generate_api_key()
            session.add(
                ApiKey(
                    user_id=user.id,
                    prefix=generated.prefix,
                    key_hash=generated.key_hash,
                    name="alias-only key",
                    billing_group_id=group.id,
                )
            )
            await session.commit()
            secret = generated.secret

        auth = {"authorization": f"Bearer {secret}"}
        assert (
            await client.post("/v1/chat/completions", json=chat("fast"), headers=auth)
        ).status_code == 200
        assert (
            await client.post("/v1/chat/completions", json=chat("test-model"), headers=auth)
        ).status_code == 404
        # Nor may they use the alias on the wrong surface.
        assert (
            await client.post("/v1/embeddings", json={"model": "fast", "input": "hi"}, headers=auth)
        ).status_code == 400

    async def test_a_public_alias_works_for_a_caller_with_no_grants(
        self,
        client: httpx.AsyncClient,
        seeded: Seeded,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        """ADR 0045's flag, applied to an alias row: the same mechanism, no new one."""
        await make_alias(session_factory, public=True)

        async with session_factory() as session:
            group = Group(name="outsiders", description="no grants", api_keys_allowed=True)
            session.add(group)
            await session.flush()
            user = User(
                issuer="https://idp.test",
                subject="outsider-1",
                email="outsider@example.org",
                default_billing_group_id=group.id,
            )
            session.add(user)
            await session.flush()
            session.add(Membership(user_id=user.id, group_id=group.id))
            generated = generate_api_key()
            session.add(
                ApiKey(
                    user_id=user.id,
                    prefix=generated.prefix,
                    key_hash=generated.key_hash,
                    name="outsider key",
                    billing_group_id=group.id,
                )
            )
            await session.commit()
            secret = generated.secret

        assert (
            await client.post(
                "/v1/chat/completions",
                json=chat("fast"),
                headers={"authorization": f"Bearer {secret}"},
            )
        ).status_code == 200

    async def test_an_inactive_alias_is_a_404_like_any_inactive_model(
        self,
        client: httpx.AsyncClient,
        seeded: Seeded,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        """Not "unavailable" — an alias an administrator switched off is off."""
        await make_alias(session_factory, public=True, active=False)
        response = await client.post("/v1/chat/completions", json=chat("fast"), headers=seeded.auth)
        assert response.status_code == 404


# --------------------------------------------------------------------------
# billing follows the target; the ledger remembers the name
# --------------------------------------------------------------------------


class TestBillingFollowsTheTarget:
    async def test_the_ledger_records_the_target_and_the_alias_name(
        self,
        client: httpx.AsyncClient,
        seeded: Seeded,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        await make_alias(session_factory, group_ids=[seeded.group.id])
        assert (
            await client.post("/v1/chat/completions", json=chat("fast"), headers=seeded.auth)
        ).status_code == 200

        row = await latest_usage(session_factory)
        # The target's id and name, because prices, reports and quotas all read
        # these and must behave as if the target had been called.
        assert row.model_name == "test-model"
        async with session_factory() as session:
            assert row.model_id == await _seeded_model_id(session)
        # And the name the caller sent, beside it.
        assert row.requested_model == "fast"

    async def test_a_direct_call_records_no_requested_model(
        self,
        client: httpx.AsyncClient,
        seeded: Seeded,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        """Null, not the model's own name: the column means "via an alias"."""
        assert (
            await client.post("/v1/chat/completions", json=chat("test-model"), headers=seeded.auth)
        ).status_code == 200
        assert (await latest_usage(session_factory)).requested_model is None

    async def test_the_cost_uses_the_targets_price(
        self,
        client: httpx.AsyncClient,
        seeded: Seeded,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        """The seeded model is 1 EUR/M input and 2 EUR/M output.

        Ten prompt tokens and two completion tokens: 10e-6 + 4e-6. The point is
        that this is *the target's* price and not anything an alias could have
        carried — an alias has no price row at all, so a per-alias figure would
        have to be either zero or a copy of this one.
        """
        await make_alias(session_factory, group_ids=[seeded.group.id])
        assert (
            await client.post("/v1/chat/completions", json=chat("fast"), headers=seeded.auth)
        ).status_code == 200
        assert (await latest_usage(session_factory)).cost == Decimal("0.000014")

    async def test_a_quota_rule_on_the_target_counts_the_alias_call(
        self,
        client: httpx.AsyncClient,
        seeded: Seeded,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        """Rules name the target, and the usage is the target's, so it applies."""
        await make_alias(session_factory, group_ids=[seeded.group.id])
        async with session_factory() as session:
            session.add(
                LimitRule(
                    name="requests on the target",
                    scope=LimitScope.GROUP,
                    scope_id=seeded.group.id,
                    metric=LimitMetric.REQUESTS,
                    window_seconds=3600,
                    limit_value=Decimal("1"),
                )
            )
            await session.commit()

        assert (
            await client.post("/v1/chat/completions", json=chat("fast"), headers=seeded.auth)
        ).status_code == 200
        # The alias call used the target's only request.
        second = await client.post("/v1/chat/completions", json=chat("fast"), headers=seeded.auth)
        assert second.status_code == 429
        assert second.json()["error"]["code"] == "quota_exceeded"


# --------------------------------------------------------------------------
# retargeting
# --------------------------------------------------------------------------


class TestRetarget:
    async def test_the_next_request_goes_to_the_new_targets_upstream_model(
        self,
        client: httpx.AsyncClient,
        seeded: Seeded,
        fake_upstream: FakeUpstream,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        await make_alias(session_factory, group_ids=[seeded.group.id])
        async with session_factory() as session:
            provider_id = (
                (await session.execute(select(Provider).where(Provider.name == "fake")))
                .scalar_one()
                .id
            )
            other = ModelDef(
                name="second-model",
                upstream_model="upstream/second-model",
                provider_id=provider_id,
                kind=ModelKind.CHAT,
                context_window=8192,
            )
            session.add(other)
            await session.commit()
            other_id = other.id

        assert (
            await client.post("/v1/chat/completions", json=chat("fast"), headers=seeded.auth)
        ).status_code == 200
        assert fake_upstream.last_body["model"] == "upstream/test-model"

        async with session_factory() as session:
            route = (
                (
                    await session.execute(
                        select(ModelRouteTarget)
                        .join(ModelDef, ModelDef.id == ModelRouteTarget.alias_id)
                        .where(ModelDef.name == "fast")
                    )
                )
                .scalars()
                .first()
            )
            route.target_model_id = other_id
            await session.commit()

        # No cache to invalidate: the next request re-reads the route row.
        response = await client.post("/v1/chat/completions", json=chat("fast"), headers=seeded.auth)
        assert response.status_code == 200, response.text
        assert fake_upstream.last_body["model"] == "upstream/second-model"


# --------------------------------------------------------------------------
# unavailability (ADR 0101, decision 5)
# --------------------------------------------------------------------------


async def assert_unavailable(
    client: httpx.AsyncClient,
    seeded: Seeded,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    response = await client.post("/v1/chat/completions", json=chat("fast"), headers=seeded.auth)
    assert response.status_code == 503, response.text
    assert response.json()["error"]["code"] == "alias_unavailable"
    assert response.json()["error"]["message"] == (
        "The model alias 'fast' has no available target."
    )
    # And left out of the catalogue, which is a promise that the name works.
    listing = await client.get("/v1/models", headers=seeded.auth)
    assert listing.status_code == 200
    assert "fast" not in {card["id"] for card in listing.json()["data"]}
    # A card's detail refuses it too.
    assert (await client.get("/v1/models/fast", headers=seeded.auth)).status_code == 404


class TestUnavailability:
    async def test_a_deleted_target(
        self,
        client: httpx.AsyncClient,
        seeded: Seeded,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        await make_alias(session_factory, group_ids=[seeded.group.id])
        # What the cascade leaves behind when an operator deletes the target.
        async with session_factory() as session:
            await session.execute(
                delete(ModelRouteTarget).where(ModelRouteTarget.target_model_id == seeded.model.id)
            )
            await session.commit()
        await assert_unavailable(client, seeded, session_factory)

    async def test_an_inactive_target(
        self,
        client: httpx.AsyncClient,
        seeded: Seeded,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        await make_alias(session_factory, group_ids=[seeded.group.id])
        async with session_factory() as session:
            model = await session.get(ModelDef, seeded.model.id)
            model.is_active = False
            await session.commit()
        await assert_unavailable(client, seeded, session_factory)

    async def test_an_inactive_target_provider(
        self,
        client: httpx.AsyncClient,
        seeded: Seeded,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        await make_alias(session_factory, group_ids=[seeded.group.id])
        async with session_factory() as session:
            provider = await session.get(Provider, seeded.provider.id)
            provider.is_active = False
            await session.commit()
        await assert_unavailable(client, seeded, session_factory)


# --------------------------------------------------------------------------
# no chains
# --------------------------------------------------------------------------


class TestNoChains:
    async def test_a_forced_alias_to_alias_is_refused_at_resolution(
        self,
        client: httpx.AsyncClient,
        seeded: Seeded,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        """The save path refuses this; a row written directly is refused here.

        Which is why there are two checks: the one that matters is the one that
        still holds when the first is bypassed.
        """
        middle = await make_alias(session_factory, public=True)
        async with session_factory() as session:
            outer = ModelDef(
                name="outer",
                provider_id=None,
                upstream_model=None,
                is_alias=True,
                kind=ModelKind.CHAT,
                is_active=True,
                is_public=True,
            )
            session.add(outer)
            await session.flush()
            session.add(ModelRouteTarget(alias_id=outer.id, target_model_id=middle.id, position=0))
            await session.commit()

        response = await client.post(
            "/v1/chat/completions", json=chat("outer"), headers=seeded.auth
        )
        assert response.status_code == 503
        assert response.json()["error"]["code"] == "alias_unavailable"


# --------------------------------------------------------------------------
# kind (ADR 0101: interchangeability starts now)
# --------------------------------------------------------------------------


class TestKind:
    async def test_an_embedding_alias_on_chat_is_a_400_naming_the_alias(
        self,
        client: httpx.AsyncClient,
        seeded: Seeded,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        async with session_factory() as session:
            embedder = ModelDef(
                name="embedder",
                upstream_model="upstream/embedder",
                provider_id=seeded.provider.id,
                kind=ModelKind.EMBEDDING,
                context_window=8192,
            )
            session.add(embedder)
            await session.commit()
            embedder_id = embedder.id
        await make_alias(
            session_factory,
            name="fast-embed",
            target=await _get(session_factory, embedder_id),
            public=True,
        )

        response = await client.post(
            "/v1/chat/completions", json=chat("fast-embed"), headers=seeded.auth
        )
        assert response.status_code == 400
        body = response.json()["error"]
        assert body["code"] == "wrong_model_kind"
        # The alias, not `embedder`: the caller never sent that name, and
        # telling them to fix a request that was already right sends them off to
        # change a model they may not even be allowed to see.
        assert "fast-embed" in body["message"]
        assert "embedder" not in body["message"]


async def _get(session_factory: async_sessionmaker[AsyncSession], model_id: uuid.UUID) -> ModelDef:
    async with session_factory() as session:
        return await session.get(ModelDef, model_id)


# --------------------------------------------------------------------------
# the catalogue (ADR 0101, decision 6)
# --------------------------------------------------------------------------


class TestTheCatalogue:
    async def test_the_card_carries_the_targets_capabilities_and_the_aliass_own_text(
        self,
        client: httpx.AsyncClient,
        seeded: Seeded,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        async with session_factory() as session:
            model = await session.get(ModelDef, seeded.model.id)
            model.context_window = 4096
            model.supported_features = ["tools"]
            await session.commit()
        await make_alias(session_factory, public=True)

        card = (await client.get("/v1/models/fast", headers=seeded.auth)).json()
        assert card["id"] == "fast"
        # The alias's own text — what an administrator wrote for this name.
        assert card["display_name"] == "Fast"
        assert card["description"] == "the fast one"
        # The target's capabilities.
        assert card["context_window"] == 4096
        assert card["supported_features"] == ["tools"]
        assert card["kind"] == "chat"

    async def test_the_target_appears_nowhere_in_the_card(
        self,
        client: httpx.AsyncClient,
        seeded: Seeded,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        """The whole of decision 6: retargeting is invisible, and the target may
        be a model this caller cannot see at all."""
        await make_alias(session_factory, public=True)
        body = (await client.get("/v1/models/fast", headers=seeded.auth)).text
        assert "test-model" not in body
        assert "upstream/test-model" not in body
        assert str(seeded.model.id) not in body

    async def test_a_null_display_name_serialises_as_json_null(
        self,
        client: httpx.AsyncClient,
        seeded: Seeded,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        alias = await make_alias(session_factory, public=True)
        async with session_factory() as session:
            row = await session.get(ModelDef, alias.id)
            row.display_name = None
            row.description = None
            await session.commit()
        card = (await client.get("/v1/models/fast", headers=seeded.auth)).json()
        assert card["display_name"] is None
        assert card["description"] is None

    async def test_an_unavailable_alias_is_left_out_of_the_listing(
        self,
        client: httpx.AsyncClient,
        seeded: Seeded,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        await make_alias(session_factory, public=True)
        async with session_factory() as session:
            model = await session.get(ModelDef, seeded.model.id)
            model.is_active = False
            await session.commit()
        listing = await client.get("/v1/models")
        assert "fast" not in {card["id"] for card in listing.json()["data"]}

    async def test_an_inactive_alias_is_not_listed(
        self,
        client: httpx.AsyncClient,
        seeded: Seeded,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        await make_alias(session_factory, public=True, active=False)
        listing = await client.get("/v1/models", headers=seeded.auth)
        assert "fast" not in {card["id"] for card in listing.json()["data"]}

    async def test_many_aliases_cost_no_extra_queries(
        self,
        app: object,
        client: httpx.AsyncClient,
        seeded: Seeded,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        """One round trip for the targets however many there are.

        The failure this exists to catch is the obvious `selectinload` per
        alias, which is invisible with one alias and a p95 regression with
        fifty. Asserted here rather than left to `test_query_counts.py` because
        the shape of the number depends on the alias fixture too.
        """
        from test_query_counts import counted

        for index in range(5):
            await make_alias(session_factory, name=f"alias-{index}", public=True)

        async def catalogue() -> list[str]:
            response = await client.get("/v1/models", headers=seeded.auth)
            assert response.status_code == 200
            return [card["id"] for card in response.json()["data"]]

        # Warm anything one-off, as the pinned tests do.
        await catalogue()
        engine = app.state.engine  # type: ignore[attr-defined]
        with counted(engine) as statements:
            ids = await catalogue()
        selects = [s for s in statements if s.lstrip().upper().startswith("SELECT")]
        assert "alias-4" in ids
        assert len(selects) <= 4, f"{len(selects)} selects:\n" + "\n".join(selects)


# --------------------------------------------------------------------------
# every metered surface resolves an alias
# --------------------------------------------------------------------------


class TestEverySurface:
    #: (kind, path, body, echoes_a_model_name). The upstream id is
    #: `upstream/<model name>`, which is what the assertion compares against.
    #:
    #: The flag is because `/v1/images/generations` returns no `model` field at
    #: all — the OpenAI image schema has none, and adding one to make this table
    #: uniform would be a change to a surface's contract rather than to aliases.
    #: Listed so that the exception is visible here instead of being an `if` in
    #: the test body.
    SURFACES: ClassVar[list[tuple[ModelKind, str, dict, bool]]] = [
        (
            ModelKind.CHAT,
            "/v1/chat/completions",
            {"messages": [{"role": "user", "content": "hi"}]},
            True,
        ),
        (ModelKind.CHAT, "/v1/responses", {"input": "hi"}, True),
        (
            ModelKind.CHAT,
            "/v1/messages",
            {"max_tokens": 16, "messages": [{"role": "user", "content": "hi"}]},
            True,
        ),
        (ModelKind.EMBEDDING, "/v1/embeddings", {"input": "hi"}, True),
        (ModelKind.IMAGE, "/v1/images/generations", {"prompt": "a cat"}, False),
        (
            ModelKind.OCR,
            "/v1/ocr",
            {"document": {"type": "document_url", "document_url": "https://example.org/a.pdf"}},
            True,
        ),
    ]

    @pytest.mark.parametrize(
        ("kind", "path", "body", "echoes"), SURFACES, ids=[c[1] for c in SURFACES]
    )
    async def test_the_surface_routes_to_the_targets_upstream_model(
        self,
        kind: ModelKind,
        path: str,
        body: dict,
        echoes: bool,
        client: httpx.AsyncClient,
        seeded: Seeded,
        fake_upstream: FakeUpstream,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        """One test per surface, because each passes the resolved model on its
        own way and a shared mistake there is a shared fix.

        The assertion is that the upstream was asked for the **target's**
        upstream id and never the alias's. An alias has no upstream model, so
        the value that would have been sent is ``None`` — and the provider
        rejects that with an error nobody can act on, which is exactly the
        failure mode a resolver that half-works produces.
        """
        model_name = f"{kind.value}-model"
        async with session_factory() as session:
            model = ModelDef(
                name=model_name,
                upstream_model=f"upstream/{model_name}",
                provider_id=seeded.provider.id,
                kind=kind,
                context_window=8192,
            )
            session.add(model)
            await session.commit()
            model_id = model.id

        alias = await make_alias(session_factory, name=f"alias-{model_name}", public=True)
        # Repoint at this surface's own model rather than the seeded chat one.
        async with session_factory() as session:
            route = (
                await session.execute(
                    select(ModelRouteTarget).where(ModelRouteTarget.alias_id == alias.id)
                )
            ).scalar_one()
            route.target_model_id = model_id
            await session.commit()

        fake_upstream.set_json(COMPLETION)
        response = await client.post(path, json={**body, "model": alias.name}, headers=seeded.auth)
        assert response.status_code == 200, f"{path}: {response.text}"
        assert fake_upstream.last_body["model"] == f"upstream/{model_name}"
        # Where the surface echoes a model name at all, it is the name the
        # caller sent, never the target's: naming the target would leak a model
        # they may not be allowed to see.
        if echoes:
            assert response.json()["model"] == alias.name
