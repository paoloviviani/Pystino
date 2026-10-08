"""OIDC access tokens as a ``/v1`` credential (ADR 0040).

This is authentication on the metered path, so the tests are about what must be
*refused* at least as much as what must work. The claim shapes come from a token
this deployment's Keycloak actually issued on 2026-08-28 — notably that an access
token carries **no ``aud`` claim at all** unless a mapper adds one, which is why
several of these would pass vacuously if written from the specification instead.

The fixtures live in conftest.py: the query-count suite pins this path too.
"""

from __future__ import annotations

import base64
import json
from typing import Any

import pytest
from conftest import (
    ABSENT,
    BEARER_AUDIENCE,
    BEARER_ISSUER,
    Seeded,
    make_token,
    seed_identity_provider,
)
from conftest import (
    bearer_auth as auth,
)
from fastapi import FastAPI
from gateway.config import OIDCSettings, Settings
from gateway.models import Group, Membership, OIDCPolicyConfig, UsageRecord, User
from gateway.types import utcnow
from helpers import completion_body
from joserfc.jwk import RSAKey
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker


class TestAccepted:
    @pytest.mark.asyncio
    async def test_a_valid_token_bills_the_users_group(
        self,
        bearer_app: FastAPI,
        client: Any,
        seeded: Seeded,
        signing_key: RSAKey,
        fake_upstream: Any,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        """The whole point: a chat turn arrives with no API key and is metered."""
        fake_upstream.set_json(completion_body())
        response = await client.post(
            "/v1/chat/completions",
            json={"model": "test-model", "messages": [{"role": "user", "content": "hi"}]},
            headers=auth(make_token(signing_key)),
        )
        assert response.status_code == 200, response.text

        async with session_factory() as db:
            record = (await db.execute(select(UsageRecord))).scalars().one()
        # Attribution is by user and group; the key column is simply empty. A
        # bearer request that recorded no user would be spend nobody can see.
        assert record.user_id == seeded.user.id
        assert record.group_id == seeded.group.id
        assert record.api_key_id is None

    @pytest.mark.asyncio
    async def test_an_unknown_subject_is_provisioned(
        self,
        bearer_app: FastAPI,
        client: Any,
        seeded: Seeded,
        signing_key: RSAKey,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        """A person who has never opened the console can still use the API.

        Without this, chat would be unusable until each user visited a screen
        they have no other reason to visit.
        """
        token = make_token(signing_key, sub="brand-new", email="new@example.org")
        response = await client.get("/v1/models", headers=auth(token))
        assert response.status_code == 200

        async with session_factory() as db:
            user = (await db.execute(select(User).where(User.subject == "brand-new"))).scalar_one()
            assert user.email == "new@example.org"
            # Provisioned into the group the token claims, and — being their only
            # group — it becomes the default they bill.
            assert user.default_billing_group_id == seeded.group.id
            # Not a login. `last_login_at` must still mean "signed in".
            assert user.last_login_at is None

    @pytest.mark.asyncio
    async def test_group_removal_in_the_directory_takes_effect(
        self,
        bearer_app: FastAPI,
        client: Any,
        seeded: Seeded,
        signing_key: RSAKey,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        """A token that no longer claims a group stops being able to bill it.

        The failure this prevents: revoking someone in the directory stops them
        signing into the console while leaving them able to spend through the
        API, which is the half that costs money.
        """
        response = await client.get("/v1/models", headers=auth(make_token(signing_key, groups=[])))
        assert response.status_code == 403

        async with session_factory() as db:
            user = (await db.execute(select(User).where(User.subject == "subject-1"))).scalar_one()
            assert user.default_billing_group_id is None

    @pytest.mark.asyncio
    async def test_an_api_key_still_works(
        self, bearer_app: FastAPI, client: Any, seeded: Seeded
    ) -> None:
        """Enabling tokens must not disturb the credential everything else uses."""
        response = await client.get("/v1/models", headers=seeded.auth)
        assert response.status_code == 200


class TestRefused:
    """Every refusal returns the same message a bad API key gets.

    Distinguishing "expired" from "wrong audience" from "no such user" hands a
    prober a map of the validator, so the tests assert the status and not a
    reason the response must not contain.
    """

    @pytest.mark.asyncio
    async def test_expired(
        self, bearer_app: FastAPI, client: Any, seeded: Seeded, signing_key: RSAKey
    ) -> None:
        # Well past the 60-second skew tolerance. An earlier draft of this test
        # used exactly 60 and passed against an expired token, because leeway
        # made it valid on the boundary — a test that asserts nothing.
        past = int(utcnow().timestamp()) - 3600
        token = make_token(signing_key, exp=past, iat=past - 300)
        response = await client.get("/v1/models", headers=auth(token))
        assert response.status_code == 401

    @pytest.mark.asyncio
    async def test_another_issuer(
        self, bearer_app: FastAPI, client: Any, seeded: Seeded, signing_key: RSAKey
    ) -> None:
        """`iss` is part of a user's identity, so a re-hosted realm must fail.

        Accepting it would provision everybody a second time under new rows with
        no memberships, which reads as data loss rather than a configuration
        error.
        """
        token = make_token(signing_key, iss="https://evil.test")
        response = await client.get("/v1/models", headers=auth(token))
        assert response.status_code == 401

    @pytest.mark.asyncio
    async def test_audience_absent(
        self, bearer_app: FastAPI, client: Any, seeded: Seeded, signing_key: RSAKey
    ) -> None:
        """The live default: Keycloak omits `aud` unless a mapper adds one."""
        token = make_token(signing_key, aud=ABSENT)
        response = await client.get("/v1/models", headers=auth(token))
        assert response.status_code == 401

    @pytest.mark.asyncio
    async def test_audience_is_another_service(
        self, bearer_app: FastAPI, client: Any, seeded: Seeded, signing_key: RSAKey
    ) -> None:
        token = make_token(signing_key, aud=["some-other-api"])
        response = await client.get("/v1/models", headers=auth(token))
        assert response.status_code == 401

    @pytest.mark.asyncio
    async def test_an_id_token_is_not_an_api_credential(
        self, bearer_app: FastAPI, client: Any, seeded: Seeded, signing_key: RSAKey
    ) -> None:
        """The hole this closes is specific and easy to miss.

        An ID token names the client as its audience. A deployment that sets
        `access_token_audience` to its own client_id would therefore accept the
        very token it hands to the browser at login.
        """
        token = make_token(signing_key, typ="ID")
        response = await client.get("/v1/models", headers=auth(token))
        assert response.status_code == 401

    @pytest.mark.asyncio
    async def test_signed_by_another_key(
        self, bearer_app: FastAPI, client: Any, seeded: Seeded
    ) -> None:
        other = RSAKey.generate_key(2048, parameters={"kid": "test-key-1"})
        response = await client.get("/v1/models", headers=auth(make_token(other)))
        assert response.status_code == 401

    @pytest.mark.asyncio
    async def test_unsigned_token(
        self, bearer_app: FastAPI, client: Any, seeded: Seeded, signing_key: RSAKey
    ) -> None:
        """`alg: none` is the oldest JWT attack and must not survive the switch."""
        _, payload, _ = make_token(signing_key).split(".")
        none_header = (
            base64.urlsafe_b64encode(json.dumps({"alg": "none"}).encode()).rstrip(b"=").decode()
        )
        response = await client.get("/v1/models", headers=auth(f"{none_header}.{payload}."))
        assert response.status_code == 401

    @pytest.mark.asyncio
    async def test_a_deactivated_user(
        self,
        bearer_app: FastAPI,
        client: Any,
        seeded: Seeded,
        signing_key: RSAKey,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        """Deactivating a user must stop them even while their token is valid."""
        async with session_factory() as db:
            user = (await db.execute(select(User).where(User.subject == "subject-1"))).scalar_one()
            user.is_active = False
            await db.commit()

        response = await client.get("/v1/models", headers=auth(make_token(signing_key)))
        assert response.status_code == 401


class TestGroupMappingsAreGlobalOnly:
    """ADR 0093 §3.4: the row's own `group_mappings` is no longer read on the
    bearer path — only `oidc_config`, the global policy, is. Closes R9.

    The row's own mapping is left at its default (empty) rather than set to a
    *different* value and compared: `seed_identity_provider` caches a stub
    client keyed by the row's `(id, updated_at)`, and writing to the row would
    bump `updated_at` and evict it, replacing it with a real `OIDCClient`
    built from the registry's own settings snapshot — a self-inflicted
    failure unrelated to what this test is asking. Leaving the row's mapping
    untouched and empty still distinguishes the two: an unmapped claim name
    would pass through as its own name if the row (or nothing) were consulted,
    and only becomes the global policy's local name if the global policy is
    what actually ran.

    Checked from the database after the call, not from the response body: a
    request that changes its own caller's default billing group and then
    resolves the billing group *in the same request* hits a pre-existing,
    unrelated staleness bug (reported, not fixed here — see the final
    report) — `User.default_billing_group` is `lazy="joined"`, loaded once
    per request, and reassigning `default_billing_group_id` in memory during
    reconciliation does not refresh it, so `resolve_billing_group` can still
    see the group the request just moved the caller out of. The membership
    row itself is written correctly either way, which is what this test is
    actually asking about.
    """

    @pytest.mark.asyncio
    async def test_the_global_mapping_is_applied(
        self,
        bearer_app: FastAPI,
        client: Any,
        seeded: Seeded,
        signing_key: RSAKey,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        async with session_factory() as db:
            db.add(OIDCPolicyConfig(group_mappings=[["idp-eng", "global-mapped"]]))
            # Existing, because group_import=manual (the default) creates no
            # group from a claim: the mapping is what is under test here.
            db.add(Group(name="global-mapped"))
            await db.commit()
        await bearer_app.state.oidc_policy.refresh_once()

        token = make_token(signing_key, groups=["idp-eng"])
        await client.get("/v1/me", headers=auth(token))

        async with session_factory() as db:
            user = (await db.execute(select(User).where(User.subject == "subject-1"))).scalar_one()
            memberships = (
                (await db.execute(select(Membership).where(Membership.user_id == user.id)))
                .scalars()
                .all()
            )
            names = {m.group.name for m in memberships}
        assert "global-mapped" in names
        assert "idp-eng" not in names, "the raw claim name would appear unmapped"


class TestAcceptedClients:
    """ADR 0093 §2: `azp` (or `client_id`) must be in `ACCEPTED_CLIENTS`, once
    that is set — the audience check alone only proves the token is *for*
    this gateway, never that it was asked for by a client an administrator
    actually trusts.

    `bearer_app` is deliberately not used here: it seeds a row for
    `BEARER_ISSUER` once, and seeding a second one for the same issuer with
    different settings hits the unique index — each test seeds its own,
    exactly the way `bearer_app` itself does.
    """

    async def _app(self, app: FastAPI, signing_key: RSAKey, *, accepted_clients: str) -> FastAPI:
        settings: Settings = app.state.settings
        settings.oidc = OIDCSettings(
            enabled=True,
            issuer=BEARER_ISSUER,
            client_id="llm-gateway",
            groups_claim="groups",
            access_token_audience=BEARER_AUDIENCE,
            accepted_clients=accepted_clients,
        )
        await seed_identity_provider(app, app.state.session_factory, signing_key)
        return app

    @pytest.mark.asyncio
    async def test_a_listed_azp_is_accepted(
        self, app: FastAPI, client: Any, seeded: Seeded, signing_key: RSAKey
    ) -> None:
        await self._app(app, signing_key, accepted_clients="llm-chat,opencode")
        response = await client.get("/v1/models", headers=auth(make_token(signing_key)))
        assert response.status_code == 200

    @pytest.mark.asyncio
    async def test_an_unlisted_azp_is_refused(
        self, app: FastAPI, client: Any, seeded: Seeded, signing_key: RSAKey
    ) -> None:
        await self._app(app, signing_key, accepted_clients="opencode")
        response = await client.get("/v1/models", headers=auth(make_token(signing_key)))
        assert response.status_code == 401

    @pytest.mark.asyncio
    async def test_client_id_is_read_when_azp_is_absent(
        self, app: FastAPI, client: Any, seeded: Seeded, signing_key: RSAKey
    ) -> None:
        await self._app(app, signing_key, accepted_clients="llm-chat")
        token = make_token(signing_key, azp=ABSENT, client_id="llm-chat")
        response = await client.get("/v1/models", headers=auth(token))
        assert response.status_code == 200

    @pytest.mark.asyncio
    async def test_neither_claim_still_passes_on_audience_alone(
        self, app: FastAPI, client: Any, seeded: Seeded, signing_key: RSAKey
    ) -> None:
        await self._app(app, signing_key, accepted_clients="llm-chat")
        token = make_token(signing_key, azp=ABSENT)
        response = await client.get("/v1/models", headers=auth(token))
        assert response.status_code == 200

    @pytest.mark.asyncio
    async def test_empty_accepted_clients_enforces_nothing(
        self, app: FastAPI, client: Any, seeded: Seeded, signing_key: RSAKey
    ) -> None:
        """The default (unset `ACCEPTED_CLIENTS`) is today's behaviour exactly:
        the audience check alone."""
        await self._app(app, signing_key, accepted_clients="")
        response = await client.get(
            "/v1/models", headers=auth(make_token(signing_key, azp="anything-at-all"))
        )
        assert response.status_code == 200


class TestNotConfigured:
    @pytest.mark.asyncio
    async def test_no_audience_means_no_tokens(
        self, app: FastAPI, client: Any, seeded: Seeded, signing_key: RSAKey
    ) -> None:
        """A deployment that has not opted in is unchanged.

        Note `bearer_app` is deliberately not requested here: this is the default
        configuration, where a perfectly valid token is simply not a credential.
        """
        settings: Settings = app.state.settings
        settings.oidc = OIDCSettings(
            enabled=True, issuer=BEARER_ISSUER, client_id="llm-gateway", access_token_audience=""
        )
        await seed_identity_provider(app, app.state.session_factory, signing_key)

        response = await client.get("/v1/models", headers=auth(make_token(signing_key)))
        assert response.status_code == 401
