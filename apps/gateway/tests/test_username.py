"""The directory's own username, recorded and searchable.

From a report: *"I don't see chat@local in the console"*. The account was
there — listed as `chat@example.org`, its email — because `chat@local` was the
Keycloak **username**, and `sync_user_from_claims` read `preferred_username`
only as a *fallback* for the display name. Keycloak sends both, so it was
discarded on every login.

The test that matters most here is
`test_an_existing_account_acquires_its_username_on_the_next_request`. The naive
fix — record the claim when provisioning — fixes nothing for anybody who
already exists, because `sync_user_from_claims` returns early when the token
says nothing new. Every account on a running deployment is in exactly that
state, which is to say: the fix would have appeared to work and changed nothing
for the person who reported it.
"""

from __future__ import annotations

import httpx
import pytest
from conftest import ABSENT, BEARER_ISSUER, Seeded, bearer_auth, make_token
from fastapi import FastAPI
from gateway.deps import get_management_user
from gateway.models import User
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker


def as_admin(app: FastAPI, user: User) -> None:
    """Stand in for the session cookie, as `test_admin.py` does.

    The cookie path is covered end to end by a live script; repeating it here
    would be testing the identity provider rather than the listing.
    """
    user.is_admin = True
    app.dependency_overrides[get_management_user] = lambda: user


@pytest.mark.asyncio
async def test_a_new_directory_account_records_its_username(
    bearer_app: FastAPI,
    client: httpx.AsyncClient,
    signing_key: object,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    token = make_token(
        signing_key,
        sub="kc-1",
        email="chat@example.org",
        name="Chat Tester",
        preferred_username="chat@local",
    )
    response = await client.get("/v1/models", headers=bearer_auth(token))
    assert response.status_code == 200, response.text

    async with session_factory() as db:
        user = (
            await db.execute(select(User).where(User.subject == "kc-1"))
        ).scalars().one()
        # Both, separately. The display name is what to show; the username is
        # what the account was created as.
        assert user.display_name == "Chat Tester"
        assert user.username == "chat@local"
        assert user.email == "chat@example.org"


@pytest.mark.asyncio
async def test_an_existing_account_acquires_its_username_on_the_next_request(
    bearer_app: FastAPI,
    client: httpx.AsyncClient,
    signing_key: object,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """The test the naive fix fails.

    Every account on a running deployment already exists with no username, and
    `sync_user_from_claims` short-circuits when the token says nothing new — so
    recording the claim only at creation would have left the reporter's own
    account exactly as it was.
    """
    token = make_token(
        signing_key,
        sub="kc-2",
        email="someone@example.org",
        name="Someone",
        preferred_username="someone@local",
    )
    # First request creates the row; then take the username away, which is the
    # state of every account that predates the column.
    assert (await client.get("/v1/models", headers=bearer_auth(token))).status_code == 200
    async with session_factory() as db:
        user = (
            await db.execute(select(User).where(User.subject == "kc-2"))
        ).scalars().one()
        user.username = None
        await db.commit()

    # A second, otherwise unremarkable request — same groups, same email — has
    # to notice and fill it.
    assert (await client.get("/v1/models", headers=bearer_auth(token))).status_code == 200
    async with session_factory() as db:
        user = (
            await db.execute(select(User).where(User.subject == "kc-2"))
        ).scalars().one()
        assert user.username == "someone@local"


@pytest.mark.asyncio
async def test_a_directory_that_sends_no_name_still_gets_a_display_name(
    bearer_app: FastAPI,
    client: httpx.AsyncClient,
    signing_key: object,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    # The old fallback is kept: a directory sending only `preferred_username`
    # should still produce something to show a person.
    # ABSENT, not omitted: `make_token` supplies a default `name`, so leaving
    # it out of the overrides would still send one — which is how the first
    # version of this test asserted the fallback while never exercising it.
    token = make_token(
        signing_key,
        sub="kc-3",
        email="nameless@example.org",
        name=ABSENT,
        preferred_username="nameless@local",
    )
    assert (await client.get("/v1/models", headers=bearer_auth(token))).status_code == 200
    async with session_factory() as db:
        user = (
            await db.execute(select(User).where(User.subject == "kc-3"))
        ).scalars().one()
        assert user.display_name == "nameless@local"
        assert user.username == "nameless@local"


@pytest.mark.asyncio
async def test_a_username_change_in_the_directory_is_picked_up(
    bearer_app: FastAPI,
    client: httpx.AsyncClient,
    signing_key: object,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    # A username is mutable, which is exactly why it is not identity. Identity
    # stays `(issuer, subject)`, so a rename updates the row rather than
    # creating a second one.
    first = make_token(signing_key, sub="kc-4", preferred_username="before@local")
    assert (await client.get("/v1/models", headers=bearer_auth(first))).status_code == 200
    second = make_token(signing_key, sub="kc-4", preferred_username="after@local")
    assert (await client.get("/v1/models", headers=bearer_auth(second))).status_code == 200

    async with session_factory() as db:
        rows = (
            (await db.execute(select(User).where(User.issuer == BEARER_ISSUER)))
            .scalars()
            .all()
        )
        matching = [user for user in rows if user.subject == "kc-4"]
        assert len(matching) == 1, "a rename must not fork the account"
        assert matching[0].username == "after@local"


@pytest.mark.asyncio
async def test_the_admin_listing_finds_an_account_by_its_username(
    app: FastAPI,
    client: httpx.AsyncClient,
    seeded: Seeded,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """The reported failure, as a test.

    Searching the console for the name an account was created under has to find
    it — that is the whole point, and it is a different query from searching by
    email.
    """
    async with session_factory() as db:
        db.add(
            User(
                issuer="https://idp.test",
                subject="kc-5",
                email="findme@example.org",
                display_name="Find Me",
                username="findme@local",
            )
        )
        await db.commit()

    async with session_factory() as db:
        admin = (
            await db.execute(select(User).where(User.id == seeded.user.id))
        ).scalars().one()
        admin.is_admin = True
        await db.commit()
        await db.refresh(admin, attribute_names=["memberships"])
    as_admin(app, admin)

    found = await client.get("/api/admin/users?q=findme@local")
    assert found.status_code == 200, found.text
    emails = [row["email"] for row in found.json()["items"]]
    assert "findme@example.org" in emails
    usernames = [row["username"] for row in found.json()["items"]]
    assert "findme@local" in usernames
