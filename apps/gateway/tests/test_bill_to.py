"""Choosing which group pays, per request (ADR 0061).

This is money, so the tests are about what must be *refused* as much as what
must work — ground rule 3. The property being defended is that ``x-bill-to``
adds no capability: it can only name a group the caller is a member of right
now, and a group they could already bill by changing their default. Every
refusal below is one of the ways that could stop being true.

The claim shapes and fixtures come from test_bearer_auth.py, whose header
records that they were taken from a token this deployment's Keycloak actually
issued rather than written from the specification.
"""

from __future__ import annotations

import uuid
from datetime import timedelta
from typing import Any

import pytest
from conftest import Seeded, make_token
from conftest import bearer_auth as auth
from fastapi import FastAPI
from gateway.models import (
    ApiKey,
    Group,
    GroupModelAccess,
    Membership,
    MembershipSource,
    UsageRecord,
)
from gateway.security import generate_api_key
from gateway.types import utcnow
from helpers import completion_body
from joserfc.jwk import RSAKey
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

BILL_TO = "x-bill-to"


async def _second_group(
    session_factory: async_sessionmaker[AsyncSession], seeded: Seeded, *, name: str = "finance"
) -> uuid.UUID:
    """A second group the seeded user belongs to, granted by hand.

    `source=MANUAL` on purpose: this is the case a token's `groups` claim can
    never describe, and the one ADR 0057 records as having been got wrong three
    times. A test using an OIDC-sourced membership here would pass against an
    implementation that read the claim.
    """
    async with session_factory() as db:
        group = Group(name=name, description="granted by an administrator")
        db.add(group)
        await db.flush()
        db.add(
            Membership(user_id=seeded.user.id, group_id=group.id, source=MembershipSource.MANUAL)
        )
        # The group must be able to use the model, or the refusal under test
        # would be indistinguishable from a model-access refusal.
        db.add(GroupModelAccess(group_id=group.id, model_id=seeded.model.id))
        await db.commit()
        return group.id


class TestHonoured:
    @pytest.mark.asyncio
    async def test_the_header_bills_the_named_group_not_the_default(
        self,
        bearer_app: FastAPI,
        client: Any,
        seeded: Seeded,
        signing_key: RSAKey,
        fake_upstream: Any,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        """The feature: one user, two groups, and the request says which pays."""
        other = await _second_group(session_factory, seeded)
        fake_upstream.set_json(completion_body())

        response = await client.post(
            "/v1/chat/completions",
            json={"model": "test-model", "messages": [{"role": "user", "content": "hi"}]},
            headers={**auth(make_token(signing_key)), BILL_TO: "finance"},
        )
        assert response.status_code == 200, response.text

        async with session_factory() as db:
            record = (await db.execute(select(UsageRecord))).scalars().one()
        assert record.group_id == other
        # The default is untouched: sticky belongs to the client, and a header
        # that quietly rewrote a stored preference would make the next request
        # from a different client bill somewhere new.
        assert record.group_id != seeded.group.id

    @pytest.mark.asyncio
    async def test_a_manually_granted_group_is_billable(
        self,
        bearer_app: FastAPI,
        client: Any,
        seeded: Seeded,
        signing_key: RSAKey,
        fake_upstream: Any,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        """The group is absent from the token, and must still work (ADR 0057).

        An implementation resolving the header against the `groups` claim passes
        every other test here and fails this one, which is why it exists.
        """
        other = await _second_group(session_factory, seeded, name="hand-granted")
        fake_upstream.set_json(completion_body())

        # The token names only the directory's group, as a real one would.
        token = make_token(signing_key, groups=["research"])
        response = await client.post(
            "/v1/chat/completions",
            json={"model": "test-model", "messages": [{"role": "user", "content": "hi"}]},
            headers={**auth(token), BILL_TO: "hand-granted"},
        )
        assert response.status_code == 200, response.text

        async with session_factory() as db:
            record = (await db.execute(select(UsageRecord))).scalars().one()
        assert record.group_id == other

    @pytest.mark.asyncio
    async def test_absent_header_still_bills_the_default(
        self,
        bearer_app: FastAPI,
        client: Any,
        seeded: Seeded,
        signing_key: RSAKey,
        fake_upstream: Any,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        """The behaviour every existing caller has. Unchanged."""
        await _second_group(session_factory, seeded)
        fake_upstream.set_json(completion_body())

        response = await client.post(
            "/v1/chat/completions",
            json={"model": "test-model", "messages": [{"role": "user", "content": "hi"}]},
            headers=auth(make_token(signing_key)),
        )
        assert response.status_code == 200, response.text

        async with session_factory() as db:
            record = (await db.execute(select(UsageRecord))).scalars().one()
        assert record.group_id == seeded.group.id

    @pytest.mark.asyncio
    async def test_whitespace_only_is_treated_as_absent(
        self,
        bearer_app: FastAPI,
        client: Any,
        seeded: Seeded,
        signing_key: RSAKey,
        fake_upstream: Any,
    ) -> None:
        """A client that sends the header with nothing in it means nothing by it."""
        fake_upstream.set_json(completion_body())
        response = await client.post(
            "/v1/chat/completions",
            json={"model": "test-model", "messages": [{"role": "user", "content": "hi"}]},
            headers={**auth(make_token(signing_key)), BILL_TO: "   "},
        )
        assert response.status_code == 200, response.text


class TestRefused:
    @pytest.mark.asyncio
    async def test_a_group_the_user_does_not_belong_to_is_refused(
        self,
        bearer_app: FastAPI,
        client: Any,
        seeded: Seeded,
        signing_key: RSAKey,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        """The security property. Membership is re-checked on every request."""
        async with session_factory() as db:
            db.add(Group(name="someone-elses", description="not this user's"))
            await db.commit()

        response = await client.post(
            "/v1/chat/completions",
            json={"model": "test-model", "messages": [{"role": "user", "content": "hi"}]},
            headers={**auth(make_token(signing_key)), BILL_TO: "someone-elses"},
        )
        assert response.status_code == 403, response.text

    @pytest.mark.asyncio
    async def test_a_group_that_does_not_exist_is_refused_identically(
        self,
        bearer_app: FastAPI,
        client: Any,
        seeded: Seeded,
        signing_key: RSAKey,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        """And says the same thing, so the header cannot enumerate groups.

        A distinguishable "no such group" would turn a billing header into a
        directory of the deployment's group names for any authenticated user.
        """
        async with session_factory() as db:
            db.add(Group(name="someone-elses", description="not this user's"))
            await db.commit()

        real = await client.post(
            "/v1/chat/completions",
            json={"model": "test-model", "messages": [{"role": "user", "content": "hi"}]},
            headers={**auth(make_token(signing_key)), BILL_TO: "someone-elses"},
        )
        absent = await client.post(
            "/v1/chat/completions",
            json={"model": "test-model", "messages": [{"role": "user", "content": "hi"}]},
            headers={**auth(make_token(signing_key)), BILL_TO: "no-such-group-anywhere"},
        )
        assert real.status_code == absent.status_code == 403
        # Same shape and same wording bar the name the caller supplied.
        assert real.json()["error"]["type"] == absent.json()["error"]["type"]

    @pytest.mark.asyncio
    async def test_a_disabled_group_is_refused(
        self,
        bearer_app: FastAPI,
        client: Any,
        seeded: Seeded,
        signing_key: RSAKey,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        """Membership is not enough: `resolve_billing_group` also wants it active."""
        other = await _second_group(session_factory, seeded, name="dormant")
        async with session_factory() as db:
            group = await db.get(Group, other)
            assert group is not None
            group.is_active = False
            await db.commit()

        response = await client.post(
            "/v1/chat/completions",
            json={"model": "test-model", "messages": [{"role": "user", "content": "hi"}]},
            headers={**auth(make_token(signing_key)), BILL_TO: "dormant"},
        )
        assert response.status_code == 403, response.text

    @pytest.mark.asyncio
    async def test_an_api_key_caller_is_refused_rather_than_ignored(
        self,
        client: Any,
        seeded: Seeded,
        fake_upstream: Any,
    ) -> None:
        """A key carries its own answer, and a caller who asked is told.

        Ignoring the header was the alternative: the request would succeed and
        bill the key's group, and the difference would surface on an invoice.
        """
        fake_upstream.set_json(completion_body())
        response = await client.post(
            "/v1/chat/completions",
            json={"model": "test-model", "messages": [{"role": "user", "content": "hi"}]},
            headers={**seeded.auth, BILL_TO: "research"},
        )
        assert response.status_code == 403, response.text
        assert "x-bill-to" in response.json()["error"]["message"]

    @pytest.mark.asyncio
    async def test_a_minted_session_credential_bills_the_named_group(
        self,
        client: Any,
        seeded: Seeded,
        fake_upstream: Any,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        """The house IdP's own access tokens are keys in the table — and steer.

        The IdP keeps its access tokens opaque, so a session's credential is
        stored as an ``ApiKey`` row and reaches this endpoint through the same
        resolution an issued key takes. It is not an issued key, though: it is
        the proof of who is calling, minted at login and short-lived, and it
        bills the way the JWT it stands in for bills. An implementation that
        refused every row in the table would log this user out of web search
        and their chat turns the moment their settings named a group.
        """
        other = await _second_group(session_factory, seeded)
        minted = generate_api_key(environment_prefix="gwa")
        async with session_factory() as db:
            db.add(
                ApiKey(
                    user_id=seeded.user.id,
                    prefix=minted.prefix,
                    key_hash=minted.key_hash,
                    name="idp:cerea",
                    minted_by="idp-cerea",
                    expires_at=utcnow() + timedelta(minutes=15),
                )
            )
            await db.commit()
        fake_upstream.set_json(completion_body())

        response = await client.post(
            "/v1/chat/completions",
            json={"model": "test-model", "messages": [{"role": "user", "content": "hi"}]},
            headers={"authorization": f"Bearer {minted.secret}", BILL_TO: "finance"},
        )
        assert response.status_code == 200, response.text

        async with session_factory() as db:
            record = (await db.execute(select(UsageRecord))).scalars().one()
        assert record.group_id == other

    @pytest.mark.asyncio
    async def test_a_minted_session_credential_without_the_header_bills_the_default(
        self,
        client: Any,
        seeded: Seeded,
        fake_upstream: Any,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        """Steering is per request; without it, the default answers as always."""
        fake_upstream.set_json(completion_body())
        minted = generate_api_key(environment_prefix="gwa")
        async with session_factory() as db:
            db.add(
                ApiKey(
                    user_id=seeded.user.id,
                    prefix=minted.prefix,
                    key_hash=minted.key_hash,
                    name="idp:cerea",
                    minted_by="idp-cerea",
                    expires_at=utcnow() + timedelta(minutes=15),
                )
            )
            await db.commit()

        response = await client.post(
            "/v1/chat/completions",
            json={"model": "test-model", "messages": [{"role": "user", "content": "hi"}]},
            headers={"authorization": f"Bearer {minted.secret}"},
        )
        assert response.status_code == 200, response.text

        async with session_factory() as db:
            record = (await db.execute(select(UsageRecord))).scalars().one()
        assert record.group_id == seeded.group.id

    @pytest.mark.asyncio
    async def test_a_minted_session_credential_steering_outside_its_memberships_is_refused(
        self,
        client: Any,
        seeded: Seeded,
        fake_upstream: Any,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        """Steering adds no capability: membership is checked exactly as for JWTs."""
        minted = generate_api_key(environment_prefix="gwa")
        async with session_factory() as db:
            db.add(
                ApiKey(
                    user_id=seeded.user.id,
                    prefix=minted.prefix,
                    key_hash=minted.key_hash,
                    name="idp:cerea",
                    minted_by="idp-cerea",
                    expires_at=utcnow() + timedelta(minutes=15),
                )
            )
            await db.commit()
        fake_upstream.set_json(completion_body())

        response = await client.post(
            "/v1/chat/completions",
            json={"model": "test-model", "messages": [{"role": "user", "content": "hi"}]},
            headers={"authorization": f"Bearer {minted.secret}", BILL_TO: "not-a-group"},
        )
        assert response.status_code == 403, response.text

    @pytest.mark.asyncio
    async def test_nothing_is_billed_when_the_header_is_refused(
        self,
        bearer_app: FastAPI,
        client: Any,
        seeded: Seeded,
        signing_key: RSAKey,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        """The refusal happens in authentication, before any reservation.

        A refused request that had already written a ledger row would bill for a
        call that never went upstream.
        """
        response = await client.post(
            "/v1/chat/completions",
            json={"model": "test-model", "messages": [{"role": "user", "content": "hi"}]},
            headers={**auth(make_token(signing_key)), BILL_TO: "no-such-group"},
        )
        assert response.status_code == 403

        async with session_factory() as db:
            assert (await db.execute(select(UsageRecord))).scalars().all() == []


class TestDiscovery:
    @pytest.mark.asyncio
    async def test_a_bearer_caller_can_list_its_billable_groups(
        self,
        bearer_app: FastAPI,
        client: Any,
        seeded: Seeded,
        signing_key: RSAKey,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        """What the chat's dropdown reads. `/api/me` needs a cookie it lacks."""
        await _second_group(session_factory, seeded)

        response = await client.get("/v1/billing/groups", headers=auth(make_token(signing_key)))
        assert response.status_code == 200, response.text
        body = response.json()

        names = [g["name"] for g in body["data"]]
        assert names == ["finance", "research"], "sorted by name"
        assert body["billing_group"] == "research", "what this request billed"
        defaults = [g["name"] for g in body["data"] if g["is_default"]]
        assert defaults == ["research"]

    @pytest.mark.asyncio
    async def test_it_reports_the_group_the_header_chose(
        self,
        bearer_app: FastAPI,
        client: Any,
        seeded: Seeded,
        signing_key: RSAKey,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        """So a client can confirm the header took effect instead of assuming."""
        await _second_group(session_factory, seeded)
        response = await client.get(
            "/v1/billing/groups",
            headers={**auth(make_token(signing_key)), BILL_TO: "finance"},
        )
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["billing_group"] == "finance"
        # The default is still reported as the default. They are different
        # questions and a client showing a dropdown needs both.
        assert [g["name"] for g in body["data"] if g["is_default"]] == ["research"]

    @pytest.mark.asyncio
    async def test_a_disabled_group_is_not_offered(
        self,
        bearer_app: FastAPI,
        client: Any,
        seeded: Seeded,
        signing_key: RSAKey,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        """Listing a group whose use is refused would offer a choice that fails."""
        other = await _second_group(session_factory, seeded, name="dormant")
        async with session_factory() as db:
            group = await db.get(Group, other)
            assert group is not None
            group.is_active = False
            await db.commit()

        response = await client.get("/v1/billing/groups", headers=auth(make_token(signing_key)))
        assert response.status_code == 200, response.text
        assert "dormant" not in [g["name"] for g in response.json()["data"]]

    @pytest.mark.asyncio
    async def test_an_api_key_caller_may_also_list(
        self,
        client: Any,
        seeded: Seeded,
    ) -> None:
        """It is a `/v1` surface, not a bearer-only one.

        The header is bearer-only; knowing which groups you may bill is not
        privileged, and a key-holding program discovering it is harmless.
        """
        response = await client.get("/v1/billing/groups", headers=seeded.auth)
        assert response.status_code == 200, response.text
        assert response.json()["billing_group"] == "research"

    @pytest.mark.asyncio
    async def test_it_needs_a_credential(self, client: Any) -> None:
        response = await client.get("/v1/billing/groups")
        assert response.status_code == 401
