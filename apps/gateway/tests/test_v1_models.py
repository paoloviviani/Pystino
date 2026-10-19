"""What /v1/models lists, and what stays off it.

The list is an OpenAI-compatible client's picture of the deployment, which is
why a search tier does not ride in it by default (ADR 0071): a tier answers
/v1/search, and a client that read one here would present it as a chat model
— the confusion the surface must not create. `?include=search` is the opt-in,
and grants still bound everything either way.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any

import pytest
from conftest import Seeded, make_token
from conftest import bearer_auth as auth
from fastapi import FastAPI
from gateway.models import (
    Group,
    GroupModelAccess,
    ModelDef,
    ModelKind,
    ModelPrice,
    ModelRouteTarget,
    Provider,
    ProviderKind,
    UserModelAccess,
)
from helpers import completion_body
from joserfc.jwk import RSAKey
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from test_admin import as_user, make_admin

#: The billing header. Declared here rather than copied from test_bill_to so
#: the tests below read the wire directly: what they defend is that the
#: listing answers the same question the header does.
BILL_TO = "x-bill-to"


async def _second_group_and_model(
    session_factory: async_sessionmaker[AsyncSession],
    seeded: Seeded,
    app: FastAPI,
    *,
    group_name: str = "beta",
    model_name: str = "only-beta",
) -> Group:
    """A second group owning a chat model no other group is granted.

    Written directly, in the shape the admin save path leaves behind: a model
    behind the seeded provider (so a completion through it is otherwise
    ordinary), priced so metering has nothing to complain about, and granted
    to exactly one group. The group is also mapped in the identity policy
    (`group_import=manual` imports mapped names only), so a token claiming it
    becomes a member the way a real login would.
    """
    from gateway.models import OIDCPolicyConfig

    async with session_factory() as db:
        group = Group(name=group_name)
        db.add(group)
        await db.flush()
        model = ModelDef(
            name=model_name,
            upstream_model=f"upstream/{model_name}",
            provider_id=seeded.provider.id,
            context_window=8192,
        )
        db.add(model)
        await db.flush()
        db.add(
            ModelPrice(
                model_id=model.id,
                input_per_mtok=Decimal("1"),
                output_per_mtok=Decimal("2"),
                currency="EUR",
            )
        )
        db.add(GroupModelAccess(group_id=group.id, model_id=model.id))
        config = (await db.execute(select(OIDCPolicyConfig))).scalar_one()
        config.group_mappings = [*config.group_mappings, [group_name, group_name]]
        await db.commit()
        resolver = getattr(app.state, "oidc_policy", None)
        if resolver is not None:
            await resolver.refresh_once()
        return group


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
        # An exact set on purpose: this card is the whole public surface of a
        # model, and the test exists to catch anything *added* to it as well as
        # anything that leaks. `description` arrived with model aliases
        # (ADR 0101), which need their own text beside the target's
        # capabilities; it is null for a model nobody described, and OpenAI
        # clients ignore the field either way.
        assert set(card) == {
            "id",
            "object",
            "created",
            "owned_by",
            "context_window",
            "max_output_tokens",
            "max_input_tokens",
            "display_name",
            "description",
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
            # ADR 0096 (amended): the api_keys flag rides along — this test
            # is about model narrowing, not capabilities.
            group = Group(
                name="ungranted", description="no grants on purpose", api_keys_allowed=True
            )
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


class TestListingFollowsTheBillingGroup:
    """The list and the admission answer the same question, on the same scope.

    The owner's report: "as an admin, I still see models that I'm not entitled
    to use." It was never an admin thing — it hit anyone with more than one
    membership, because the listing was scoped to the *union* of the caller's
    groups while a request is admitted against the *billing group* only, so a
    model granted to group B answered 404 to anyone billing group A. The
    owner's decision: the list shows exactly what a request would accept,
    nothing more.

    Every test here bills through the ``x-bill-to`` header because that is
    how the chat's requests arrive; a user with two directory groups and no
    header has, by design, no default to fall back on.
    """

    def _headers(self, signing_key: RSAKey, *, bill_to: str) -> dict[str, str]:
        return {
            **auth(make_token(signing_key, groups=["research", "beta"])),
            "x-bill-to": bill_to,
        }

    @pytest.mark.asyncio
    async def test_not_billable_means_not_listed_and_not_usable(
        self,
        bearer_app: FastAPI,
        client: Any,
        seeded: Seeded,
        signing_key: RSAKey,
        fake_upstream: Any,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        """The owner's case: granted to group B, billing group A — off the
        list, refused by name, refused as a completion."""
        await _second_group_and_model(session_factory, seeded, bearer_app)
        headers = self._headers(signing_key, bill_to="research")
        response = await client.get("/v1/models", headers=headers)
        assert response.status_code == 200, response.text
        names = {entry["id"] for entry in response.json()["data"]}
        assert names == {"test-model"}, names
        assert (await client.get("/v1/models/only-beta", headers=headers)).status_code == 404
        completion = await client.post(
            "/v1/chat/completions",
            json={"model": "only-beta", "messages": [{"role": "user", "content": "hi"}]},
            headers=headers,
        )
        assert completion.status_code == 404, completion.text

    @pytest.mark.asyncio
    async def test_billable_means_listed_and_usable(
        self,
        bearer_app: FastAPI,
        client: Any,
        seeded: Seeded,
        signing_key: RSAKey,
        fake_upstream: Any,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        """The same person asking to bill the group that holds the grant gets
        the model on the list, by name, and through a completion."""
        await _second_group_and_model(session_factory, seeded, bearer_app)
        fake_upstream.set_json(completion_body())
        headers = self._headers(signing_key, bill_to="beta")
        response = await client.get("/v1/models", headers=headers)
        assert response.status_code == 200, response.text
        names = {entry["id"] for entry in response.json()["data"]}
        assert names == {"only-beta"}, names
        assert (await client.get("/v1/models/only-beta", headers=headers)).status_code == 200
        completion = await client.post(
            "/v1/chat/completions",
            json={"model": "only-beta", "messages": [{"role": "user", "content": "hi"}]},
            headers=headers,
        )
        assert completion.status_code == 200, completion.text

    @pytest.mark.asyncio
    async def test_a_key_lists_what_its_billing_group_admits(
        self,
        bearer_app: FastAPI,
        client: Any,
        seeded: Seeded,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        """A key never negotiates scope: it bills the group it was minted for
        (``billing_group_id`` pins it), whatever else its user belongs to.

        The owner's case in the key's shape: the person joins `beta`, so the
        *union* of their groups now holds `only-beta` — but this key bills
        `research` without sending any header (it may not: the gateway
        refuses `x-bill-to` from a key, so there is no way for a key to
        widen its scope either). The listing shows the key's billing group's
        models, the person's own grants and the public ones — the same set
        `resolve_model` would admit — and `only-beta` is off the list and
        refused by name.
        """
        from gateway.models import Membership

        await _second_group_and_model(session_factory, seeded, bearer_app)
        async with session_factory() as db:
            beta = (await db.execute(select(Group).where(Group.name == "beta"))).scalar_one()
            # A membership with no OIDC story on purpose: whatever put the
            # person in beta, a request through this key cannot bill it.
            db.add(Membership(user_id=seeded.user.id, group_id=beta.id))
            personal = ModelDef(
                name="just-mine",
                upstream_model="upstream/just-mine",
                provider_id=seeded.provider.id,
            )
            public = ModelDef(
                name="shared",
                upstream_model="upstream/shared",
                provider_id=seeded.provider.id,
                is_public=True,
            )
            db.add_all([personal, public])
            await db.flush()
            db.add(UserModelAccess(user_id=seeded.user.id, model_id=personal.id))
            await db.commit()

        response = await client.get("/v1/models", headers=seeded.auth)
        assert response.status_code == 200, response.text
        names = {entry["id"] for entry in response.json()["data"]}
        assert names == {"test-model", "just-mine", "shared"}, names
        # The list and the admission answer the same question for the key:
        # the model that only the other group holds is refused by name.
        completion = await client.post(
            "/v1/chat/completions",
            json={"model": "only-beta", "messages": [{"role": "user", "content": "hi"}]},
            headers=seeded.auth,
        )
        assert completion.status_code == 404, completion.text

    @pytest.mark.asyncio
    async def test_public_models_are_listed_regardless(
        self,
        bearer_app: FastAPI,
        client: Any,
        seeded: Seeded,
        signing_key: RSAKey,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        """A public model needs no grant and follows every billing group."""
        async with session_factory() as db:
            db.add(
                ModelDef(
                    name="shared",
                    upstream_model="upstream/shared",
                    provider_id=seeded.provider.id,
                    is_public=True,
                )
            )
            await db.commit()
        headers = self._headers(signing_key, bill_to="research")
        response = await client.get("/v1/models", headers=headers)
        assert response.status_code == 200, response.text
        names = {entry["id"] for entry in response.json()["data"]}
        assert "shared" in names, names
        # A card's detail still answers for it: listing and retrieval agree.
        assert (await client.get("/v1/models/shared", headers=headers)).status_code == 200

    @pytest.mark.asyncio
    async def test_personal_grants_are_listed_regardless(
        self,
        bearer_app: FastAPI,
        client: Any,
        seeded: Seeded,
        signing_key: RSAKey,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        """A grant made to the person, not to any group, follows the person."""
        from gateway.models import User

        headers = self._headers(
            signing_key, bill_to="research"
        )  # A first call provisions the caller the way a real first request does.
        first = await client.get("/v1/models", headers=headers)
        assert first.status_code == 200, first.text
        assert "just-mine" not in {entry["id"] for entry in first.json()["data"]}
        async with session_factory() as db:
            model = ModelDef(
                name="just-mine",
                upstream_model="upstream/just-mine",
                provider_id=seeded.provider.id,
            )
            db.add(model)
            await db.flush()
            user = (await db.execute(select(User).where(User.subject == "subject-1"))).scalar_one()
            db.add(UserModelAccess(user_id=user.id, model_id=model.id))
            await db.commit()
        response = await client.get("/v1/models", headers=headers)
        assert response.status_code == 200, response.text
        names = {entry["id"] for entry in response.json()["data"]}
        assert "just-mine" in names, names

    @pytest.mark.asyncio
    async def test_an_alias_is_listed_on_its_own_billable_grant(
        self,
        bearer_app: FastAPI,
        client: Any,
        seeded: Seeded,
        signing_key: RSAKey,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        """An alias is in or out on its *own* grant (ADR 0101), and that grant
        is read the same way the request path reads it: against the billing
        group the caller is asking to charge."""
        await _second_group_and_model(session_factory, seeded, bearer_app, group_name="beta")
        async with session_factory() as db:
            alias = ModelDef(
                name="fast",
                provider_id=None,
                upstream_model=None,
                is_alias=True,
                kind=ModelKind.CHAT,
                display_name="Fast",
                is_active=True,
            )
            db.add(alias)
            await db.flush()
            target = (
                await db.execute(select(ModelDef).where(ModelDef.name == "test-model"))
            ).scalar_one()
            db.add(ModelRouteTarget(alias_id=alias.id, target_model_id=target.id, position=0))
            beta = (await db.execute(select(Group).where(Group.name == "beta"))).scalar_one()
            db.add(GroupModelAccess(group_id=beta.id, model_id=alias.id))
            await db.commit()

        research = self._headers(signing_key, bill_to="research")
        response = await client.get("/v1/models", headers=research)
        assert response.status_code == 200, response.text
        assert "fast" not in {entry["id"] for entry in response.json()["data"]}
        assert (await client.get("/v1/models/fast", headers=research)).status_code == 404

        billing = self._headers(signing_key, bill_to="beta")
        response = await client.get("/v1/models", headers=billing)
        assert response.status_code == 200, response.text
        assert "fast" in {entry["id"] for entry in response.json()["data"]}
        assert (await client.get("/v1/models/fast", headers=billing)).status_code == 200
