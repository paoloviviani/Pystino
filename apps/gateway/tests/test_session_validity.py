"""A console session token minted before `sessions_valid_after` is refused.

ADR 0093 §9.1: the disable cascade's whole point is that it also ends
sessions someone already holds, not just ones they open after it runs. The
gateway's own JWT signature still verifies on an old token — nothing about
the token itself is wrong — so the refusal has to come from comparing it
against the row, in `load_user_for_management`, at no extra query cost
(the same row the route already loads).
"""

from __future__ import annotations

import uuid
from datetime import timedelta
from typing import Any

import httpx
import pytest
from conftest import Seeded
from gateway.models import User
from gateway.oidc import issue_session_token
from gateway.types import utcnow
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker


def session_cookie(user_id: uuid.UUID, app: Any) -> dict[str, str]:
    token = issue_session_token(
        user_id,
        secret=app.state.settings.session_secret.get_secret_value(),
        ttl_seconds=3600,
    )
    return {"cookie": f"gw_session={token}"}


class TestSessionsValidAfter:
    @pytest.mark.asyncio
    async def test_a_token_issued_before_sessions_valid_after_is_refused(
        self,
        app: Any,
        client: httpx.AsyncClient,
        seeded: Seeded,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        cookie = session_cookie(seeded.user.id, app)

        # The disable cascade runs *after* this session was minted — the
        # exact ordering a stale-session attack (or a merge, or a break-glass
        # recovery) relies on.
        async with session_factory() as db:
            row = await db.get(User, seeded.user.id)
            assert row is not None
            row.sessions_valid_after = utcnow() + timedelta(seconds=1)
            await db.commit()

        response = await client.get("/api/me/limits", headers=cookie)
        assert response.status_code == 401, response.text

    @pytest.mark.asyncio
    async def test_a_token_issued_after_sessions_valid_after_still_works(
        self,
        app: Any,
        client: httpx.AsyncClient,
        seeded: Seeded,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        async with session_factory() as db:
            row = await db.get(User, seeded.user.id)
            assert row is not None
            row.sessions_valid_after = utcnow() - timedelta(minutes=5)
            await db.commit()

        # Minted after the stamp above: a sign-in that happened *after* the
        # disable (or a re-enable, or break-glass) must not be caught by it.
        cookie = session_cookie(seeded.user.id, app)

        response = await client.get("/api/me/limits", headers=cookie)
        assert response.status_code == 200, response.text

    @pytest.mark.asyncio
    async def test_null_sessions_valid_after_never_refuses(
        self,
        app: Any,
        client: httpx.AsyncClient,
        seeded: Seeded,
    ) -> None:
        """The common case forever: nobody has ever disabled this account."""
        cookie = session_cookie(seeded.user.id, app)

        response = await client.get("/api/me/limits", headers=cookie)
        assert response.status_code == 200, response.text
