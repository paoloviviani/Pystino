"""Inference providers: credentials, routing, and the access union.

Three things carry real consequences and are tested directly: a stored API key
must be unreadable in the database and never returned by the API; a request must
reach the provider its model points at, with that provider's credential; and
access must be the union of group and personal grants without becoming a way to
take access away.
"""

from __future__ import annotations

import uuid

import httpx
import pytest
from conftest import FakeUpstream, Seeded
from gateway.access import accessible_models
from gateway.config import UpstreamSettings
from gateway.models import GroupModelAccess, ModelDef, Provider, User, UserModelAccess
from gateway.providers import ProviderConfigurationError, ProviderRegistry
from gateway.secrets import (
    SecretBox,
    SecretDecryptionError,
    SecretsUnavailableError,
    hint_for,
)
from sqlalchemy import select
from sqlalchemy.dialects import postgresql
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from test_admin import as_user, make_admin

KEY = "test-encryption-key-not-for-production"


# --------------------------------------------------------------------------
# encryption
# --------------------------------------------------------------------------


class TestSecretBox:
    def test_a_secret_round_trips(self) -> None:
        box = SecretBox([KEY])
        assert box.decrypt(box.encrypt("sk-live-123")) == "sk-live-123"

    def test_the_ciphertext_does_not_contain_the_secret(self) -> None:
        """The whole point: a database dump must not yield the credential."""
        box = SecretBox([KEY])
        assert "sk-live-123" not in box.encrypt("sk-live-123")

    def test_the_same_secret_encrypts_differently_each_time(self) -> None:
        # A random IV per message. Without it, equal ciphertexts would reveal
        # that two providers share a key.
        box = SecretBox([KEY])
        assert box.encrypt("same") != box.encrypt("same")

    def test_a_stored_value_says_which_scheme_produced_it(self) -> None:
        assert SecretBox([KEY]).encrypt("x").startswith("v1:")

    def test_a_wrong_key_cannot_read_it(self) -> None:
        ciphertext = SecretBox([KEY]).encrypt("sk-live-123")
        with pytest.raises(SecretDecryptionError):
            SecretBox(["a-different-key"]).decrypt(ciphertext)

    def test_a_retired_key_still_decrypts_during_rotation(self) -> None:
        """The property that makes rotation possible rather than theoretical:
        prepend a new key, restart, and everything still opens."""
        old = SecretBox(["old-key"])
        ciphertext = old.encrypt("sk-live-123")

        rotating = SecretBox(["new-key", "old-key"])
        assert rotating.decrypt(ciphertext) == "sk-live-123"
        # And new writes use the new key, so the old one can eventually go.
        assert SecretBox(["new-key"]).decrypt(rotating.encrypt("fresh")) == "fresh"

    def test_rotate_re_encrypts_under_the_current_key(self) -> None:
        rotating = SecretBox(["new-key", "old-key"])
        moved = rotating.rotate(SecretBox(["old-key"]).encrypt("sk-live-123"))
        assert SecretBox(["new-key"]).decrypt(moved) == "sk-live-123"

    def test_with_no_key_configured_storing_is_refused(self) -> None:
        # Refused, not silently skipped: a provider whose key vanished would
        # fail later as a provider outage.
        box = SecretBox([])
        assert not box.enabled
        with pytest.raises(SecretsUnavailableError):
            box.encrypt("sk-live-123")

    def test_a_corrupt_value_is_refused_with_an_explanation(self) -> None:
        with pytest.raises(SecretDecryptionError, match="not in a format"):
            SecretBox([KEY]).decrypt("just-some-text")

    def test_the_decryption_error_says_how_to_recover(self) -> None:
        ciphertext = SecretBox(["old"]).encrypt("x")
        with pytest.raises(SecretDecryptionError, match="GATEWAY_SECRET_KEY"):
            SecretBox(["new"]).decrypt(ciphertext)


class TestHints:
    def test_a_long_key_shows_only_its_ends(self) -> None:
        assert hint_for("sk-abcdefghijklmnop") == "sk-a…mnop"

    def test_a_short_secret_is_masked_entirely(self) -> None:
        # "ab…cd" of a six-character key gives away most of it.
        assert hint_for("short1") == "••••••"

    def test_nothing_is_hinted_for_no_key(self) -> None:
        assert hint_for("") == ""


# --------------------------------------------------------------------------
# the registry
# --------------------------------------------------------------------------


def make_provider(**overrides: object) -> Provider:
    box = SecretBox([KEY])
    defaults: dict[str, object] = {
        "id": uuid.uuid4(),
        "name": "acme",
        "base_url": "https://acme.test/v1",
        "api_key_encrypted": box.encrypt("provider-key"),
        "api_key_hint": hint_for("provider-key"),
        "extra_headers": {},
        "is_active": True,
        "updated_at": __import__("datetime").datetime(
            2026, 8, 15, tzinfo=__import__("datetime").UTC
        ),
    }
    return Provider(**{**defaults, **overrides})


def registry(client: httpx.AsyncClient | None = None) -> ProviderRegistry:
    return ProviderRegistry(
        UpstreamSettings(),
        SecretBox([KEY]),
        client_factory=(lambda _settings: client) if client else None,
    )


class TestRegistry:
    async def test_the_provider_credential_is_sent_not_the_gateways(self) -> None:
        seen: list[httpx.Request] = []

        def handle(request: httpx.Request) -> httpx.Response:
            seen.append(request)
            return httpx.Response(200, json={"data": []})

        client = httpx.AsyncClient(transport=httpx.MockTransport(handle))
        upstream = await registry(client).upstream_for(make_provider())
        await upstream.list_models()

        assert seen[0].headers["authorization"] == "Bearer provider-key"
        assert str(seen[0].url).startswith("https://acme.test/v1")

    async def test_extra_headers_travel_with_the_provider(self) -> None:
        seen: list[httpx.Request] = []
        client = httpx.AsyncClient(
            transport=httpx.MockTransport(
                lambda request: (seen.append(request), httpx.Response(200, json={}))[1]
            )
        )
        provider = make_provider(extra_headers={"x-tenant": "links"})
        await (await registry(client).upstream_for(provider)).list_models()
        assert seen[0].headers["x-tenant"] == "links"

    async def test_a_provider_with_no_key_sends_no_authorization(self) -> None:
        """A local vLLM or Ollama has no credential, and inventing an empty
        bearer token would be rejected by some of them."""
        seen: list[httpx.Request] = []
        client = httpx.AsyncClient(
            transport=httpx.MockTransport(
                lambda request: (seen.append(request), httpx.Response(200, json={}))[1]
            )
        )
        provider = make_provider(api_key_encrypted="", api_key_hint="")
        await (await registry(client).upstream_for(provider)).list_models()
        assert "authorization" not in seen[0].headers

    async def test_the_client_is_reused_while_the_provider_is_unchanged(self) -> None:
        reg = registry()
        provider = make_provider()
        assert await reg.upstream_for(provider) is await reg.upstream_for(provider)

    async def test_editing_a_provider_rebuilds_its_client(self) -> None:
        """Otherwise a rotated key or a corrected base URL would need a restart."""
        import datetime

        reg = registry()
        provider = make_provider()
        first = await reg.upstream_for(provider)

        provider.base_url = "https://elsewhere.test/v1"
        provider.updated_at = datetime.datetime(2026, 8, 16, tzinfo=datetime.UTC)
        assert await reg.upstream_for(provider) is not first

    async def test_a_deactivated_provider_is_refused_with_a_reason(self) -> None:
        with pytest.raises(ProviderConfigurationError, match="deactivated"):
            await registry().upstream_for(make_provider(is_active=False))

    async def test_an_unreadable_credential_refuses_rather_than_sending_none(self) -> None:
        """Sending no credential would arrive as a 401 from the provider and read
        like an outage. This says what actually happened."""
        provider = make_provider(api_key_encrypted=SecretBox(["some-other-key"]).encrypt("x"))
        with pytest.raises(ProviderConfigurationError, match="GATEWAY_SECRET_KEY"):
            await registry().upstream_for(provider)


# --------------------------------------------------------------------------
# the admin API
# --------------------------------------------------------------------------


@pytest.fixture
async def admin_client(
    app: object,
    client: httpx.AsyncClient,
    seeded: Seeded,
    session_factory: async_sessionmaker[AsyncSession],
) -> httpx.AsyncClient:
    as_user(app, await make_admin(session_factory, seeded))
    return client


class TestProviderApi:
    async def test_a_provider_can_be_created(self, admin_client: httpx.AsyncClient) -> None:
        response = await admin_client.post(
            "/api/admin/providers",
            json={"name": "acme", "base_url": "https://acme.test/v1", "api_key": "sk-acme-123456"},
        )
        assert response.status_code == 201, response.text
        body = response.json()
        assert body["name"] == "acme"
        assert body["has_api_key"] is True
        assert body["model_count"] == 0

    async def test_the_api_never_returns_the_key(self, admin_client: httpx.AsyncClient) -> None:
        """Write-only, throughout. A hint is enough to tell two keys apart."""
        await admin_client.post(
            "/api/admin/providers",
            json={"name": "acme", "base_url": "https://acme.test/v1", "api_key": "sk-acme-123456"},
        )
        listing = (await admin_client.get("/api/admin/providers")).json()["items"]
        acme = next(entry for entry in listing if entry["name"] == "acme")

        assert "api_key" not in acme
        assert "sk-acme-123456" not in str(acme)
        assert acme["api_key_hint"] == "sk-a…3456"

    async def test_the_key_is_encrypted_in_the_database(
        self, admin_client: httpx.AsyncClient, session: AsyncSession
    ) -> None:
        await admin_client.post(
            "/api/admin/providers",
            json={"name": "acme", "base_url": "https://acme.test/v1", "api_key": "sk-acme-123456"},
        )
        stored = (
            await session.execute(select(Provider).where(Provider.name == "acme"))
        ).scalar_one()
        assert "sk-acme-123456" not in stored.api_key_encrypted
        assert stored.api_key_encrypted.startswith("v1:")

    async def test_a_provider_needs_no_key(self, admin_client: httpx.AsyncClient) -> None:
        response = await admin_client.post(
            "/api/admin/providers",
            json={"name": "ollama", "base_url": "http://ollama:11434/v1"},
        )
        assert response.status_code == 201
        assert response.json()["has_api_key"] is False

    async def test_the_cortecs_type_brings_its_own_endpoint(
        self, admin_client: httpx.AsyncClient
    ) -> None:
        """Choosing the type is choosing the endpoint.

        The URL is vendor knowledge and lives in the plugin (ADR 0032), so an
        operator configures Cortecs by typing a name and a key rather than
        re-typing a URL the gateway already knows — and cannot mistype it into
        a provider that looks right and bills nobody.
        """
        response = await admin_client.post(
            "/api/admin/providers",
            json={"name": "cortecs", "plugin": "cortecs", "api_key": "sk-cortecs-123456"},
        )
        assert response.status_code == 201, response.text
        assert response.json()["base_url"] == "https://api.cortecs.ai/v1"

    async def test_an_explicit_url_wins_over_the_plugin_default(
        self, admin_client: httpx.AsyncClient
    ) -> None:
        """A same-type proxy or gateway replica is a legitimate configuration."""
        response = await admin_client.post(
            "/api/admin/providers",
            json={
                "name": "cortecs-proxy",
                "plugin": "cortecs",
                "base_url": "https://cortecs.internal.test/v1",
            },
        )
        assert response.status_code == 201
        assert response.json()["base_url"] == "https://cortecs.internal.test/v1"

    async def test_a_type_without_a_default_still_requires_the_url(
        self, admin_client: httpx.AsyncClient
    ) -> None:
        """The generic type's endpoints range from a cloud API to a laptop's
        Ollama; there is nothing to default to, so the field stays required."""
        response = await admin_client.post("/api/admin/providers", json={"name": "acme"})
        assert response.status_code == 400
        assert "base URL" in response.json()["error"]["message"]

    async def test_a_duplicate_name_is_409(self, admin_client: httpx.AsyncClient) -> None:
        payload = {"name": "acme", "base_url": "https://acme.test/v1"}
        assert (await admin_client.post("/api/admin/providers", json=payload)).status_code == 201
        assert (await admin_client.post("/api/admin/providers", json=payload)).status_code == 409

    async def test_a_provider_can_be_renamed(self, admin_client: httpx.AsyncClient) -> None:
        """Because it gets mistyped, and there is no other way out.

        A provider serving any model refuses to be deleted, so without this the
        typo is permanent — and it is not private, it appears as `owned_by` on
        every /v1/models card that provider serves.
        """
        created = (
            await admin_client.post(
                "/api/admin/providers",
                json={"name": "cortecce", "base_url": "https://api.cortecs.ai/v1"},
            )
        ).json()
        response = await admin_client.patch(
            f"/api/admin/providers/{created['id']}", json={"name": "cortecs"}
        )
        assert response.status_code == 200
        assert response.json()["name"] == "cortecs"

    async def test_renaming_onto_an_existing_name_is_409(
        self, admin_client: httpx.AsyncClient
    ) -> None:
        """Names are unique, so a rename collides exactly as a create does — and
        must say so rather than surfacing the IntegrityError as a 500."""
        for name in ("first", "second"):
            await admin_client.post(
                "/api/admin/providers", json={"name": name, "base_url": f"https://{name}.test/v1"}
            )
        listing = (await admin_client.get("/api/admin/providers?limit=200")).json()["items"]
        second = next(p for p in listing if p["name"] == "second")

        response = await admin_client.patch(
            f"/api/admin/providers/{second['id']}", json={"name": "first"}
        )
        assert response.status_code == 409

    async def test_a_rename_does_not_disturb_the_credential(
        self, admin_client: httpx.AsyncClient
    ) -> None:
        """The three-way api_key convention applies to a rename too."""
        created = (
            await admin_client.post(
                "/api/admin/providers",
                json={"name": "typo", "base_url": "https://x.test/v1", "api_key": "sk-keep-me"},
            )
        ).json()
        renamed = await admin_client.patch(
            f"/api/admin/providers/{created['id']}", json={"name": "fixed"}
        )
        assert renamed.json()["name"] == "fixed"
        assert renamed.json()["has_api_key"] is True

    async def test_a_rename_to_an_impossible_name_is_refused(
        self, admin_client: httpx.AsyncClient
    ) -> None:
        """Same rule as create: a rename cannot produce a name that could not
        have been created in the first place."""
        created = (
            await admin_client.post(
                "/api/admin/providers",
                json={"name": "fine", "base_url": "https://x.test/v1"},
            )
        ).json()
        response = await admin_client.patch(
            f"/api/admin/providers/{created['id']}", json={"name": "not a valid name"}
        )
        # 400, not 422: this gateway normalises validation failures into its own
        # error envelope so that /v1 and /api answer the same shape.
        assert response.status_code == 400

    async def test_omitting_the_key_on_update_keeps_it(
        self, admin_client: httpx.AsyncClient
    ) -> None:
        """The three-way convention. Editing a base URL must not silently wipe
        the credential."""
        created = (
            await admin_client.post(
                "/api/admin/providers",
                json={
                    "name": "acme",
                    "base_url": "https://acme.test/v1",
                    "api_key": "sk-acme-123456",
                },
            )
        ).json()

        updated = await admin_client.patch(
            f"/api/admin/providers/{created['id']}",
            json={"base_url": "https://acme.test/v2"},
        )
        assert updated.json()["has_api_key"] is True
        assert updated.json()["api_key_hint"] == "sk-a…3456"

    async def test_an_empty_key_on_update_clears_it(self, admin_client: httpx.AsyncClient) -> None:
        created = (
            await admin_client.post(
                "/api/admin/providers",
                json={
                    "name": "acme",
                    "base_url": "https://acme.test/v1",
                    "api_key": "sk-acme-123456",
                },
            )
        ).json()

        updated = await admin_client.patch(
            f"/api/admin/providers/{created['id']}", json={"api_key": ""}
        )
        assert updated.json()["has_api_key"] is False
        assert updated.json()["api_key_hint"] == ""

    async def test_a_provider_in_use_cannot_be_deleted(
        self, admin_client: httpx.AsyncClient, seeded: Seeded
    ) -> None:
        """Cascading would leave historical spend attributed to a model nobody
        can explain."""
        response = await admin_client.delete(f"/api/admin/providers/{seeded.provider.id}")
        assert response.status_code == 409
        assert "model(s) still use this provider" in response.json()["error"]["message"]

    async def test_an_unused_provider_can_be_deleted(self, admin_client: httpx.AsyncClient) -> None:
        created = (
            await admin_client.post(
                "/api/admin/providers", json={"name": "unused", "base_url": "https://x.test/v1"}
            )
        ).json()
        assert (
            await admin_client.delete(f"/api/admin/providers/{created['id']}")
        ).status_code == 204

    async def test_the_model_count_is_the_blast_radius(
        self, admin_client: httpx.AsyncClient, seeded: Seeded
    ) -> None:
        listing = (await admin_client.get("/api/admin/providers")).json()["items"]
        fake = next(entry for entry in listing if entry["name"] == "fake")
        assert fake["model_count"] == 1

    async def test_a_non_admin_cannot_see_providers(
        self, app: object, client: httpx.AsyncClient, seeded: Seeded
    ) -> None:
        as_user(app, seeded.user)
        assert (await client.get("/api/admin/providers")).status_code == 403


class TestProviderTest:
    async def test_a_reachable_provider_reports_what_it_offers(
        self, admin_client: httpx.AsyncClient, seeded: Seeded, fake_upstream: FakeUpstream
    ) -> None:
        fake_upstream.set_json({"data": [{"id": "a"}, {"id": "b"}]})
        body = (await admin_client.post(f"/api/admin/providers/{seeded.provider.id}/test")).json()
        assert body["ok"] is True
        assert body["model_count"] == 2
        assert body["sample"] == ["a", "b"]

    async def test_an_authentication_failure_says_to_check_the_key(
        self, admin_client: httpx.AsyncClient, seeded: Seeded, fake_upstream: FakeUpstream
    ) -> None:
        fake_upstream.set_json({"error": "unauthorized"}, status=401)
        body = (await admin_client.post(f"/api/admin/providers/{seeded.provider.id}/test")).json()
        assert body["ok"] is False
        assert "check the API key" in body["detail"]

    async def test_a_404_suggests_the_version_path(
        self, admin_client: httpx.AsyncClient, seeded: Seeded, fake_upstream: FakeUpstream
    ) -> None:
        # The commonest configuration mistake: a base URL without /v1.
        fake_upstream.set_json({"error": "not found"}, status=404)
        body = (await admin_client.post(f"/api/admin/providers/{seeded.provider.id}/test")).json()
        assert body["ok"] is False
        assert "/v1" in body["detail"]

    async def test_a_failing_test_is_still_a_200(
        self, admin_client: httpx.AsyncClient, seeded: Seeded, fake_upstream: FakeUpstream
    ) -> None:
        """ "It did not work, and here is why" is the useful answer. Raising would
        make the console show a generic error instead of the detail."""
        fake_upstream.set_json({"error": "boom"}, status=500)
        response = await admin_client.post(f"/api/admin/providers/{seeded.provider.id}/test")
        assert response.status_code == 200
        assert response.json()["ok"] is False


# --------------------------------------------------------------------------
# routing and access
# --------------------------------------------------------------------------


class TestRouting:
    async def test_a_request_reaches_the_model_s_provider(
        self, client: httpx.AsyncClient, seeded: Seeded, fake_upstream: FakeUpstream
    ) -> None:
        from helpers import completion_body
        from test_chat_completions import basic_request

        fake_upstream.set_json(completion_body())
        response = await client.post(
            "/v1/chat/completions", json=basic_request(), headers=seeded.auth
        )
        assert response.status_code == 200
        # The credential on the provider row, not anything from the environment.
        assert fake_upstream.headers[-1]["authorization"] == "Bearer upstream-key"

    async def test_a_model_on_a_deactivated_provider_disappears(
        self, client: httpx.AsyncClient, seeded: Seeded, session: AsyncSession
    ) -> None:
        """Deactivating a provider is how an endpoint is taken out of service.
        Leaving its models listed would advertise something every request fails."""
        provider = (
            await session.execute(select(Provider).where(Provider.id == seeded.provider.id))
        ).scalar_one()
        provider.is_active = False
        await session.commit()

        visible = (await client.get("/v1/models", headers=seeded.auth)).json()["data"]
        assert visible == []

    async def test_and_calling_it_fails_with_an_explanation(
        self, client: httpx.AsyncClient, seeded: Seeded, session: AsyncSession
    ) -> None:
        from test_chat_completions import basic_request

        provider = (
            await session.execute(select(Provider).where(Provider.id == seeded.provider.id))
        ).scalar_one()
        provider.is_active = False
        await session.commit()

        response = await client.post(
            "/v1/chat/completions", json=basic_request(), headers=seeded.auth
        )
        # 404, not 502: an inactive provider's models are not visible to this
        # caller at all, and "does not exist or you cannot use it" is the same
        # answer as for any other unreachable model.
        assert response.status_code == 404


class TestAccessQueryOnPostgres:
    """What the access predicate compiles to, checked against the real dialect.

    The suite runs on SQLite, and SQLite is more forgiving than PostgreSQL in
    exactly one way that bit here: it will happily `SELECT DISTINCT` over a
    JSON column, and PostgreSQL has no equality operator for `json` at all.
    The predicate used to outer-join the two grant tables and deduplicate with
    DISTINCT, and `ModelDef.provider` is `lazy="joined"`, so the provider's
    `extra_headers` landed in the select list and `/v1/models` answered 500 on
    PostgreSQL while every test passed.

    Compiling the statement needs no database, so the check is cheap and it
    fails for the right reason.
    """

    @staticmethod
    def _sql(**kwargs: object) -> str:
        stmt = accessible_models(**kwargs)  # type: ignore[arg-type]
        return str(stmt.compile(dialect=postgresql.dialect()))

    def test_the_predicate_does_not_deduplicate(self) -> None:
        sql = self._sql(user_id=uuid.uuid4(), group_ids=[uuid.uuid4()])
        assert "DISTINCT" not in sql.upper(), (
            "DISTINCT requires an equality operator for every selected column, "
            "and the eager-loaded provider brings a json one along"
        )

    def test_both_grant_kinds_are_exists_subqueries(self) -> None:
        """EXISTS cannot fan out, which is why no deduplication is needed."""
        sql = self._sql(user_id=uuid.uuid4(), group_ids=[uuid.uuid4()])
        assert sql.upper().count("EXISTS") == 2
        assert "JOIN group_model_access" not in sql
        assert "JOIN user_model_access" not in sql

    def test_a_caller_with_no_grants_still_compiles(self) -> None:
        sql = self._sql(user_id=None, group_ids=[])
        assert "DISTINCT" not in sql.upper()
        assert "IS NULL" in sql.upper()


class TestAccessUnion:
    async def test_a_personal_grant_is_enough_without_a_group_grant(
        self, client: httpx.AsyncClient, seeded: Seeded, session: AsyncSession
    ) -> None:
        model = ModelDef(
            name="special", upstream_model="vendor/special", provider_id=seeded.provider.id
        )
        session.add(model)
        await session.flush()
        session.add(UserModelAccess(user_id=seeded.user.id, model_id=model.id))
        await session.commit()

        visible = {
            m["id"] for m in (await client.get("/v1/models", headers=seeded.auth)).json()["data"]
        }
        assert "special" in visible

    async def test_a_group_grant_is_still_enough(
        self, client: httpx.AsyncClient, seeded: Seeded
    ) -> None:
        visible = {
            m["id"] for m in (await client.get("/v1/models", headers=seeded.auth)).json()["data"]
        }
        assert visible == {"test-model"}

    async def test_a_model_granted_both_ways_appears_once(
        self, client: httpx.AsyncClient, seeded: Seeded, session: AsyncSession
    ) -> None:
        """The reason this is one query with a distinct rather than two unioned."""
        session.add(UserModelAccess(user_id=seeded.user.id, model_id=seeded.model.id))
        await session.commit()

        data = (await client.get("/v1/models", headers=seeded.auth)).json()["data"]
        assert [m["id"] for m in data] == ["test-model"]

    async def test_a_personal_grant_does_not_leak_to_anyone_else(
        self, client: httpx.AsyncClient, seeded: Seeded, session: AsyncSession
    ) -> None:
        other = User(issuer="https://idp.test", subject="other", email="other@example.org")
        model = ModelDef(
            name="special", upstream_model="vendor/special", provider_id=seeded.provider.id
        )
        session.add_all([other, model])
        await session.flush()
        session.add(UserModelAccess(user_id=other.id, model_id=model.id))
        await session.commit()

        visible = {
            m["id"] for m in (await client.get("/v1/models", headers=seeded.auth)).json()["data"]
        }
        assert "special" not in visible

    async def test_a_personal_grant_lets_the_request_through(
        self,
        client: httpx.AsyncClient,
        seeded: Seeded,
        session: AsyncSession,
        fake_upstream: FakeUpstream,
    ) -> None:
        from helpers import completion_body
        from test_chat_completions import basic_request

        model = ModelDef(
            name="special", upstream_model="vendor/special", provider_id=seeded.provider.id
        )
        session.add(model)
        await session.flush()
        session.add(UserModelAccess(user_id=seeded.user.id, model_id=model.id))
        await session.commit()

        fake_upstream.set_json(completion_body())
        response = await client.post(
            "/v1/chat/completions",
            json={**basic_request(), "model": "special"},
            headers=seeded.auth,
        )
        assert response.status_code == 200

    async def test_revoking_the_group_grant_leaves_the_personal_one(
        self, client: httpx.AsyncClient, seeded: Seeded, session: AsyncSession
    ) -> None:
        """Union, not intersection: removing one source of access leaves the
        other working."""
        session.add(UserModelAccess(user_id=seeded.user.id, model_id=seeded.model.id))
        await session.commit()

        await session.execute(
            GroupModelAccess.__table__.delete().where(GroupModelAccess.model_id == seeded.model.id)
        )
        await session.commit()

        visible = {
            m["id"] for m in (await client.get("/v1/models", headers=seeded.auth)).json()["data"]
        }
        assert visible == {"test-model"}


class TestUserGrantApi:
    async def test_granting_and_revoking(
        self, admin_client: httpx.AsyncClient, seeded: Seeded
    ) -> None:
        path = f"/api/admin/users/{seeded.user.id}/models/{seeded.model.id}"
        assert (await admin_client.put(path)).status_code == 204
        # Idempotent: granting twice is not an error.
        assert (await admin_client.put(path)).status_code == 204

        listing = (await admin_client.get("/api/admin/models")).json()["items"]
        assert listing[0]["granted_to_users"] == [seeded.user.email]

        assert (await admin_client.delete(path)).status_code == 204
        listing = (await admin_client.get("/api/admin/models")).json()["items"]
        assert listing[0]["granted_to_users"] == []

    async def test_granting_to_an_unknown_user_is_404(
        self, admin_client: httpx.AsyncClient, seeded: Seeded
    ) -> None:
        response = await admin_client.put(
            f"/api/admin/users/{uuid.uuid4()}/models/{seeded.model.id}"
        )
        assert response.status_code == 404

    async def test_a_non_admin_cannot_grant_themselves_a_model(
        self, app: object, client: httpx.AsyncClient, seeded: Seeded, session: AsyncSession
    ) -> None:
        model = ModelDef(
            name="expensive", upstream_model="vendor/expensive", provider_id=seeded.provider.id
        )
        session.add(model)
        await session.commit()

        as_user(app, seeded.user)
        response = await client.put(f"/api/admin/users/{seeded.user.id}/models/{model.id}")
        assert response.status_code == 403
