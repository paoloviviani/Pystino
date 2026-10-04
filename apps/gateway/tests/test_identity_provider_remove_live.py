"""Removing a provider against a real PostgreSQL: the lock, not the checks.

SQLite has no row locking, so `FOR UPDATE` / `FOR SHARE` are silent no-ops
there and the unit suite cannot tell a race-safe removal from a racy one. Here
a sign-in holds its share lock on the provider row (as `oidc.sign_in` does)
while a removal starts on a separate connection; the removal must wait, then
see the person the sign-in attached and refuse, rather than count zero and
delete the row out from under an account.

Gated on ``TEST_POSTGRES_URL`` (``postgresql+asyncpg://...``), like
`test_erasure_skip_locked_live.py`, which has the throwaway-container recipe.
"""

from __future__ import annotations

import asyncio
import os
import uuid
from types import SimpleNamespace
from typing import Any

import pytest
from gateway.config import Settings
from gateway.db import create_engine, create_session_factory
from gateway.models import Base, IdentityProvider, User
from gateway.routers.admin import ConflictError, remove_identity_provider
from sqlalchemy import select

TEST_POSTGRES_URL = os.environ.get("TEST_POSTGRES_URL")

pytestmark = pytest.mark.skipif(
    not TEST_POSTGRES_URL,
    reason="set TEST_POSTGRES_URL (postgresql+asyncpg://...) to run this against a real PostgreSQL",
)

ISSUER = "https://old-idp.example.org"


async def test_a_sign_in_holding_the_row_makes_the_removal_wait_and_then_refuse() -> None:
    engine = create_engine(Settings(database_url=TEST_POSTGRES_URL))
    session_factory = create_session_factory(engine)
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    try:
        async with session_factory() as session:
            row = IdentityProvider(
                name="previous-20261004-ab12",
                issuer=ISSUER,
                client_id="pystino",
                client_secret_encrypted="x",
                scopes=["openid"],
                is_enabled=False,
            )
            session.add(row)
            await session.commit()
            provider_id = row.id

        admin: Any = SimpleNamespace(id=uuid.uuid4(), email="admin@example.org")

        async def remove() -> None:
            async with session_factory() as session:
                await remove_identity_provider(provider_id, admin, session)

        async with session_factory() as signing_in:
            # What `sign_in` does first: share-lock the provider row, enabled
            # or not, until this transaction commits.
            await signing_in.execute(
                select(IdentityProvider.is_enabled)
                .where(IdentityProvider.id == provider_id)
                .with_for_update(read=True)
            )
            removal = asyncio.create_task(remove())
            await asyncio.sleep(0.5)
            assert not removal.done(), "the removal did not wait for the sign-in's lock"

            signing_in.add(User(issuer=ISSUER, subject="late", email="late@example.org"))
            await signing_in.commit()

        with pytest.raises(ConflictError, match="1 person signed in"):
            await asyncio.wait_for(removal, timeout=10)

        async with session_factory() as session:
            assert await session.get(IdentityProvider, provider_id) is not None
    finally:
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.drop_all)
        await engine.dispose()
