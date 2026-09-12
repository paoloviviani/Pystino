"""``GET /v1/me``: who is calling, and may they administer this deployment.

Two properties are defended here, and both are about a *refusal* rather than a
feature — ground rule 3, because getting either wrong hands somebody an admin
panel they should not have.

1. ``is_admin`` and ``groups`` are read from effective memberships, never from
   the token's ``groups`` claim (ADR 0057). The test that pins it grants the
   admin group **by hand**, so the claim does not name it: an implementation
   deriving either from the directory's answer passes every other test in this
   file and fails that one.
2. An API key is never told that its owner is an administrator. The person's
   flag is true, the key's answer is false, and ``credential`` says why.

The claim shapes and the bearer fixtures come from test_bearer_auth.py, whose
header records that they were copied from a token this deployment's Keycloak
actually issued.
"""

from __future__ import annotations

from typing import Any

from conftest import Seeded, make_token
from conftest import bearer_auth as auth
from fastapi import FastAPI
from gateway.models import (
    Group,
    Membership,
    MembershipSource,
    User,
)
from joserfc.jwk import RSAKey
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker


async def _granted_by_hand(
    session_factory: async_sessionmaker[AsyncSession], seeded: Seeded, name: str
) -> Group:
    """A group an administrator put this user in, that no token mentions.

    ``source=MANUAL`` is the whole point: this is the membership a directory's
    answer cannot describe, and the one ADR 0057 records as having been got
    wrong three times.
    """
    async with session_factory() as db:
        group = Group(name=name, description="granted by an administrator")
        db.add(group)
        await db.flush()
        db.add(
            Membership(
                user_id=seeded.user.id, group_id=group.id, source=MembershipSource.MANUAL
            )
        )
        await db.commit()
        await db.refresh(group)
        return group


class TestIdentity:
    async def test_a_bearer_caller_learns_who_it_is(
        self,
        bearer_app: FastAPI,
        client: Any,
        seeded: Seeded,
        signing_key: RSAKey,
    ) -> None:
        """The whole reason the route exists: `/api/me` needs a cookie it lacks."""
        response = await client.get("/v1/me", headers=auth(make_token(signing_key)))
        assert response.status_code == 200, response.text
        body = response.json()

        assert body == {
            "object": "user",
            "id": str(seeded.user.id),
            "email": "member@example.org",
            "display_name": "Test Member",
            "is_admin": False,
            "credential": "access_token",
            "groups": ["research"],
            "default_billing_group": "research",
            "billing_group": "research",
        }

    async def test_it_reports_the_group_the_request_chose(
        self,
        bearer_app: FastAPI,
        client: Any,
        seeded: Seeded,
        signing_key: RSAKey,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        """`billing_group` is what this caller bills, `default` is the preference.

        They are different questions the moment ``x-bill-to`` is in play
        (ADR 0061), and a client that conflated them would report the wrong
        budget back to the person spending it.
        """
        await _granted_by_hand(session_factory, seeded, "finance")

        response = await client.get(
            "/v1/me",
            headers={**auth(make_token(signing_key)), "x-bill-to": "finance"},
        )
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["billing_group"] == "finance"
        assert body["default_billing_group"] == "research"

    async def test_a_disabled_group_is_still_a_membership(
        self,
        bearer_app: FastAPI,
        client: Any,
        seeded: Seeded,
        signing_key: RSAKey,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        """Deliberately unlike `/v1/billing/groups`, which filters it out.

        That listing answers "what may I bill", and a disabled group is refused
        by `resolve_billing_group`. This one answers "what do I hold": a
        disabled group still grants model access and still confers admin, so
        omitting it would make the list disagree with `/v1/models` and with the
        flag beside it.
        """
        group = await _granted_by_hand(session_factory, seeded, "dormant")
        async with session_factory() as db:
            row = await db.get(Group, group.id)
            assert row is not None
            row.is_active = False
            await db.commit()

        response = await client.get("/v1/me", headers=auth(make_token(signing_key)))
        assert response.status_code == 200, response.text
        assert response.json()["groups"] == ["dormant", "research"]


class TestAdminIsAFlagNotADerivation:
    """The flag is a console fact on the `/v1` surface too (ADR 0069).

    This class was `TestAdminComesFromMemberships`, and asserted the opposite:
    that membership of a named admin group *was* the flag, computed from
    effective membership so a hand grant survived a login. ADR 0069 removed
    the derivation instead of extending it — no claim, group or setting moves
    `is_admin`, and what the console set stays set.
    """

    async def test_a_console_granted_admin_is_reported_with_their_groups(
        self,
        bearer_app: FastAPI,
        client: Any,
        seeded: Seeded,
        signing_key: RSAKey,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        """The flag and the memberships are read from the same row, independently.

        The token names only "research"; the extra group was granted here and
        still shows in `groups` (effective membership, not the claim), while
        `is_admin` reads the flag the console set. The old test needed these
        to be *one* derivation; they never needed to be.
        """
        async with session_factory() as db:
            row = await db.get(User, seeded.user.id)
            assert row is not None
            row.is_admin = True
            await db.commit()

        before = await client.get("/v1/me", headers=auth(make_token(signing_key)))
        assert before.status_code == 200, before.text
        assert before.json()["is_admin"] is True, "the console's flag is the answer"

        await _granted_by_hand(session_factory, seeded, "platform-admins")

        # The claim is unchanged, and deliberately still only names the
        # directory's group.
        token = make_token(signing_key, groups=["research"])
        response = await client.get("/v1/me", headers=auth(token))
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["is_admin"] is True
        assert body["groups"] == ["platform-admins", "research"]

    async def test_a_claim_never_grants_or_revokes_admin(
        self,
        bearer_app: FastAPI,
        client: Any,
        seeded: Seeded,
        signing_key: RSAKey,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        """The directory naming an admin group says nothing about this deployment.

        Before ADR 0069 the same token below reported `is_admin: true`, then
        `false` when the group was withdrawn — the flag following the claim in
        both directions. The claim now moves nothing, which is also what makes
        the console's grant durable rather than a cache of the directory's.
        """
        granted = await client.get(
            "/v1/me",
            headers=auth(make_token(signing_key, groups=["research", "platform-admins"])),
        )
        assert granted.status_code == 200, granted.text
        assert granted.json()["is_admin"] is False, "no claim confers admin"

        async with session_factory() as db:
            row = await db.get(User, seeded.user.id)
            assert row is not None
            row.is_admin = True
            await db.commit()

        withdrawn = await client.get(
            "/v1/me", headers=auth(make_token(signing_key, groups=["research"]))
        )
        assert withdrawn.status_code == 200, withdrawn.text
        body = withdrawn.json()
        assert body["is_admin"] is True
        assert body["groups"] == ["research"]


class TestApiKeys:
    async def test_a_key_is_never_told_its_owner_is_an_admin(
        self,
        client: Any,
        seeded: Seeded,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        """The person is an administrator. The key is not, and says so.

        The failure this prevents: a leaked ``gwk_`` key opening an admin panel
        in any client that trusts the flag — an administrative decision a
        person is supposed to make by signing in, made instead by whoever found
        a program's credential. Every administrative route would refuse that
        key anyway, so a `true` here only ever promises access it cannot
        deliver.
        """
        async with session_factory() as db:
            user = await db.get(type(seeded.user), seeded.user.id)
            assert user is not None
            user.is_admin = True
            await db.commit()

        response = await client.get("/v1/me", headers=seeded.auth)
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["is_admin"] is False
        assert body["credential"] == "api_key", (
            "false must be distinguishable from 'this person is not an admin'"
        )

    async def test_a_key_still_learns_its_own_identity(
        self, client: Any, seeded: Seeded
    ) -> None:
        """Refusing the key outright was the alternative, and was not taken.

        Identity is not the privileged part — `/v1/billing/groups` already
        answers for a key — and a program that cannot ask whose key it holds
        gets that answer from a support ticket instead.
        """
        response = await client.get("/v1/me", headers=seeded.auth)
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["id"] == str(seeded.user.id)
        assert body["email"] == "member@example.org"
        assert body["groups"] == ["research"]
        assert body["billing_group"] == "research"


class TestCredentials:
    async def test_it_needs_a_credential(self, client: Any) -> None:
        response = await client.get("/v1/me")
        assert response.status_code == 401

    async def test_a_bad_key_is_refused(self, client: Any) -> None:
        """Same refusal a bad key gets anywhere else on `/v1`."""
        response = await client.get(
            "/v1/me", headers={"authorization": "Bearer gwk_not-a-real-key"}
        )
        assert response.status_code == 401
